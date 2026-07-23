"""Config-Management Ansible runs against Kubernetes clusters and cloud databases.

Unlike VM/host targets (which SSH/WinRM *to* an IP — handled by ``api.config_mgmt``'s
``_run_job``), these run a ``hosts: localhost, connection: local`` play that reaches
*out* to the cluster API (via a kubeconfig) or the DB endpoint (via login vars).

They ALWAYS execute on a **remote in-cloud transient runner** (ECS / ACI / Cloud
Run) placed in-subnet with line-of-sight to the private endpoint — never the local
sibling-Docker path, which can't reach RFC1918 endpoints and whose egress traverses
the corporate TLS-inspecting proxy. This is the same reasoning that gave
``k8s_runner_service`` its cloud path.

Dispatched by ``jobs_worker`` (``job_type=ansible_cloud_run``). The connection
material is resolved server-side at launch from the resource row + the encrypted
config store, delivered to the runner via an ephemeral env var, and scrubbed from
the job output. The job metadata carries only refs — never a resolved credential.
"""
import base64
import json
import logging

from sqlalchemy.orm import Session

from . import (cloud_database_service, config_service, job_service,
               k8s_runner_service, k8s_service, storage_service)

logger = logging.getLogger(__name__)

# DB engines the ansible-cloud image ships collections + client libs for.
ANSIBLE_DB_ENGINES = ("postgres", "mysql", "sqlserver")

# The cloud's native transient runner (the default when no per-cloud override).
_CLOUD_NATIVE_RUNNER = {"aws": "ecs", "azure": "aci", "gcp": "gcp"}

# Distinct ECS task family so these localhost runs don't share task-def revision
# history with the SSH VM runner (ansible-config-mgmt) or the k8s runner (k8s-runner).
_ECS_TASK_FAMILY = "ansible-cloud-run"


class AnsibleCloudRunError(Exception):
    """Raised for a mis-targeted/mis-configured run before or during dispatch."""


def _cfg(key: str) -> str:
    return config_service.get(key) or ""


def _scrub(text: str, values: list) -> str:
    """Redact resolved secret values from the run output before it's stored/shown —
    defense in depth (the localhost command never traces, but a play's own ``debug``
    could echo an injected var). Values shorter than 4 chars are skipped to avoid
    over-redaction. Mirrors ``api.config_mgmt._scrub_secrets``."""
    if not text or not values:
        return text
    for v in values:
        v = str(v)
        if len(v) >= 4:
            text = text.replace(v, "***")
    return text


def _kubeconfig_tokens(kubeconfig: str) -> list:
    """Bearer tokens embedded in a (token-prepped) kubeconfig, for the scrub set."""
    try:
        import yaml
        cfg = yaml.safe_load(kubeconfig) or {}
        toks = []
        for u in (cfg.get("users") or []):
            tok = (u.get("user") or {}).get("token")
            if tok:
                toks.append(tok)
        return toks
    except Exception:  # pragma: no cover — best-effort
        return []


def resolve_runner(cloud: str) -> str:
    """The transient in-cloud runner backend for a k8s/DB target in ``cloud``:
    ``ansible_runner_<cloud>`` override, else the cloud-native default. ``local`` is
    rejected — these resources are private, so the run must execute in-cloud (this
    also keeps the API/DB traffic clear of the corporate TLS-inspecting proxy)."""
    cloud = (cloud or "").strip().lower()
    default = _CLOUD_NATIVE_RUNNER.get(cloud)
    if not default:
        raise AnsibleCloudRunError(
            f"cloud {cloud!r} has no in-cloud Ansible runner (supported: aws/azure/gcp)")
    runner = (_cfg(f"ansible_runner_{cloud}") or default).strip().lower()
    if runner == "local":
        raise AnsibleCloudRunError(
            f"Kubernetes/database targets are private and must run on an in-cloud "
            f"runner — set ansible_runner_{cloud} to {default!r} (not 'local').")
    return runner


async def _dispatch_cloud_localhost_runner(
    *, runner: str, image: str, job_id: str,
    playbook_b64: str, conn_vars_b64: str, kubeconfig_b64: str,
    ps_env: dict | None = None,
) -> tuple:
    """Route to the configured transient cloud runner, reusing the k8s runner's infra
    resolution+validation (subnet / role / VPC connector — shared with the VM Ansible
    runner config) but overriding the image with ``ansible_cloud_image`` and the ECS
    task family. Returns ``(exit_code, output)``; a ``K8sRunnerError`` from the infra
    validation (missing subnet/role/…) propagates to the caller's ``set_failed``.

    ``ps_env`` (when present) is the PASSWORD_SAFE_* env for an in-playbook
    beyondtrust.secrets_safe lookup; it rides the runner's connection-material env
    channel (no cloud store)."""
    if runner == "ecs":
        from . import aws_service
        cfg = k8s_runner_service._resolve_ecs()
        return await aws_service.run_ecs_ansible_local_task(
            region=cfg["region"], cluster=cfg["cluster"], task_family=_ECS_TASK_FAMILY,
            image=image, cpu=cfg["cpu"], memory=cfg["memory"],
            subnet_id=cfg["subnet_id"], security_group_ids=cfg["security_group_ids"],
            execution_role_arn=cfg["execution_role_arn"],
            playbook_b64=playbook_b64, conn_vars_b64=conn_vars_b64,
            kubeconfig_b64=kubeconfig_b64, job_id=job_id, ps_env=ps_env,
        )
    if runner == "aci":
        from . import azure_service
        cfg = k8s_runner_service._resolve_aci()
        return await azure_service.run_aci_ansible_local_task(
            rg=cfg["rg"], location=cfg["location"], subnet_id=cfg["subnet_id"], image=image,
            playbook_b64=playbook_b64, conn_vars_b64=conn_vars_b64,
            kubeconfig_b64=kubeconfig_b64, job_id=job_id,
            acr_server=cfg["acr_server"], acr_username=cfg["acr_username"],
            acr_password=cfg["acr_password"], ps_env=ps_env,
        )
    if runner == "gcp":
        from . import gcp_service
        cfg = k8s_runner_service._resolve_gcp()
        return await gcp_service.run_cloud_run_ansible_local_task(
            project_id=cfg["project_id"], region=cfg["region"], image=image,
            playbook_b64=playbook_b64, conn_vars_b64=conn_vars_b64,
            kubeconfig_b64=kubeconfig_b64, job_id=job_id,
            vpc_connector=cfg["vpc_connector"],
            service_account=_cfg("gcp_ansible_runner_service_account"), ps_env=ps_env,
        )
    raise AnsibleCloudRunError(f"unknown runner {runner!r}")


async def run(db: Session, *, job_id: str, meta: dict) -> None:
    """Execute one ``ansible_cloud_run`` job: resolve the target's connection material,
    launch the localhost play on the in-cloud runner, scrub + finalize. Owns its own
    ``set_completed``/``set_failed`` (worker contract); never raises."""
    job_service.set_running(db, job_id)
    scrub_values: list = []
    try:
        target_kind = meta.get("target_kind")
        target_id = meta.get("target_id") or ""
        cloud = (meta.get("cloud") or "").strip().lower()
        asset = meta.get("asset") or ""
        asset_backend = meta.get("asset_backend") or ""
        extra_vars = meta.get("extra_vars") or {}
        secret_var_refs = meta.get("secret_vars") or {}

        job_service.update_progress(db, job_id, 5, f"Fetching asset '{asset}'…")
        try:
            if asset_backend:
                raw = await storage_service.fetch_asset_in(asset_backend, asset)
                playbook_b64 = base64.b64encode(raw).decode()
            else:
                playbook_b64 = await storage_service.fetch_asset_b64(asset)
        except storage_service.StorageError as e:
            job_service.set_failed(db, job_id, f"Asset storage error: {e}")
            return

        # Operator-picked Secrets-Management named vars (permission-gated at the
        # endpoint). Resolved server-side and merged into the vars file; every value
        # is scrubbed from output. They ride the same ephemeral env as the connection
        # material, so no cloud-store residency rule applies.
        resolved_secret_vars: dict = {}
        if secret_var_refs:
            from . import ansible_secrets
            resolved_secret_vars = ansible_secrets.resolve_secret_vars(
                secret_var_refs, get=config_service.get,
                resolve_reference=config_service.resolve_reference,
                is_reference=config_service.is_reference)
            scrub_values.extend(v for v in resolved_secret_vars.values() if v)

        kubeconfig_b64 = ""
        vars_file: dict = dict(extra_vars)
        vars_file.update(resolved_secret_vars)

        if target_kind == "database":
            conn = cloud_database_service.ansible_connection_vars(db, target_id)
            engine = conn.get("db_engine")
            if engine not in ANSIBLE_DB_ENGINES:
                job_service.set_failed(
                    db, job_id,
                    f"engine {engine!r} is not supported for Ansible runs "
                    f"(supported: {', '.join(ANSIBLE_DB_ENGINES)}).")
                return
            # Auto-injected connection vars win over any operator-supplied override.
            vars_file.update(conn)
            scrub_values.append(conn.get("db_login_password"))
        elif target_kind == "k8s":
            kubeconfig = k8s_service._runner_kubeconfig(
                k8s_service.resolve_kubeconfig(db, target_id))
            kubeconfig_b64 = base64.b64encode(kubeconfig.encode()).decode()
            scrub_values.extend(_kubeconfig_tokens(kubeconfig))
        else:
            job_service.set_failed(db, job_id, f"unknown target_kind {target_kind!r}")
            return

        conn_vars_b64 = ""
        if vars_file:
            conn_vars_b64 = base64.b64encode(json.dumps(vars_file).encode()).decode()

        runner = resolve_runner(cloud)
        image = _cfg("ansible_cloud_image") or "chrweav/ansible-cloud:latest"

        # Auto-inject the configured Password Safe OAuth creds as PASSWORD_SAFE_* env so
        # an in-playbook beyondtrust.secrets_safe lookup works with no per-run setup. Rides
        # the runner's connection-material env channel (no cloud store). {} when BeyondTrust
        # is disabled / unconfigured. Scrub the client secret from output.
        from . import password_safe_runner as _psr
        ps_env = _psr.runner_env()
        _ps_secret = ps_env.get(_psr.SECRET_KEY)
        if _ps_secret and _ps_secret not in scrub_values:
            scrub_values.append(_ps_secret)

        job_service.update_progress(
            db, job_id, 20, f"Launching {runner.upper()} runner ({target_kind})…")
        exit_code, output = await _dispatch_cloud_localhost_runner(
            runner=runner, image=image, job_id=job_id,
            playbook_b64=playbook_b64, conn_vars_b64=conn_vars_b64,
            kubeconfig_b64=kubeconfig_b64, ps_env=ps_env or None,
        )
        output = _scrub(output, scrub_values)
        if exit_code == 0:
            job_service.set_completed(db, job_id, {"output": output, "returncode": exit_code})
        else:
            job_service.set_failed(db, job_id, f"ansible-playbook exited {exit_code}:\n{output}")
    except Exception as e:
        logger.exception("ansible_cloud_run job %s failed: %s", job_id, e)
        job_service.set_failed(db, job_id, _scrub(str(e), scrub_values))
