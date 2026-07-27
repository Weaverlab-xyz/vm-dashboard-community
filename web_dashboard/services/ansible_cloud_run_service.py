"""Config-Management Ansible runs against Kubernetes clusters and cloud databases.

Unlike VM/host targets (which SSH/WinRM *to* an IP — handled by ``api.config_mgmt``'s
``_run_job``), these run a ``hosts: localhost, connection: local`` play that reaches
*out* to the cluster API (via a kubeconfig) or the DB endpoint (via login vars).

The runner is chosen by the target's cloud, and the choice is about **line-of-sight
to the endpoint**:

* Cloud-hosted resources (aws / azure / gcp) ALWAYS execute on a **remote in-cloud
  transient runner** (ECS / ACI / Cloud Run) placed in-subnet with reach to the
  private endpoint — never the local sibling-Docker path, which can't reach those
  RFC1918 endpoints and whose egress traverses the corporate TLS-inspecting proxy.
  This is the same reasoning that gave ``k8s_runner_service`` its cloud path.
* A Kubernetes cluster registered with ``cloud="local"`` (an on-prem cluster — see
  ``examples/playbooks/k3s/`` for building one) inverts that: it sits on the
  corporate LAN, where an in-cloud task has no route at all, so it runs in the local
  sibling container. Same image, same localhost play, same scrubbing.

Dispatched by ``jobs_worker`` (``job_type=ansible_cloud_run``). The connection
material is resolved server-side at launch from the resource row + the encrypted
config store, delivered to the runner via an ephemeral env var, and scrubbed from
the job output. The job metadata carries only refs — never a resolved credential.
"""
import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile

from sqlalchemy.orm import Session

from . import (cloud_database_service, config_service, job_service,
               k8s_runner_service, k8s_service, storage_service)
from .ansible_localhost_cmd import build_local_docker_argv

logger = logging.getLogger(__name__)

# DB engines the ansible-cloud image ships collections + client libs for.
ANSIBLE_DB_ENGINES = ("postgres", "mysql", "sqlserver")

# The cloud's native transient runner (the default when no per-cloud override).
_CLOUD_NATIVE_RUNNER = {"aws": "ecs", "azure": "aci", "gcp": "gcp"}

# Which resources may be a Config-Management target, by cloud. Single source of truth
# for both the picker listing (/managed-targets) and the run gate — they sat as two
# separate literal tuples and silently disagreeing would half-wire the feature.
#
# "local" is a Kubernetes cluster registered from a kubeconfig (an on-prem cluster —
# examples/playbooks/k3s/ builds one); it runs on the local runner. Databases stay
# cloud-only: a CloudDatabase is always provisioned into a cloud and never carries
# cloud="local", so there is nothing for a local runner to reach.
K8S_TARGET_CLOUDS = ("aws", "azure", "gcp", "local")
DB_TARGET_CLOUDS = ("aws", "azure", "gcp")

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
    """The runner backend for a k8s/DB target in ``cloud``: ``ansible_runner_<cloud>``
    override, else the cloud-native default.

    Two rules that look contradictory but aren't — they cover opposite topologies:

    * ``cloud="local"`` (an on-prem cluster registered from a kubeconfig) MUST run on
      the local runner. The cluster is on the corporate LAN, where a transient ECS /
      ACI / Cloud Run task has no route; the dashboard host is the only thing with
      line-of-sight.
    * ``cloud`` in aws/azure/gcp must NOT run on the local runner. Those control
      planes / DB endpoints are private to their VPC, so the run has to originate
      in-cloud (which also keeps the traffic clear of a corporate TLS-inspecting
      proxy). An ``ansible_runner_<cloud>: local`` override stays an error.
    """
    cloud = (cloud or "").strip().lower()
    if cloud == "local":
        return "local"
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


def _run_local_docker_sync(cmd: list) -> tuple:
    """Run the local `docker run` and return ``(exit_code, combined_output)``.

    Note the tuple order: it matches the cloud runner task fns, which is the REVERSE
    of ansible_local_service._run_sync's ``(output, rc)``. Mirroring the cloud shape
    here keeps _dispatch_cloud_localhost_runner's four branches interchangeable."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        raise AnsibleCloudRunError(
            "The local Ansible runner needs the `docker` CLI on the dashboard host, "
            "and it was not found. On-prem Kubernetes targets run in a sibling "
            "container because only this host has a route to the cluster — a "
            "dashboard deployed in a cloud (ECS / ACI / Cloud Run) cannot run them.")
    lines: list = []
    if proc.stdout:
        for line in iter(proc.stdout.readline, ""):
            lines.append(line.rstrip())
    proc.wait()
    return proc.returncode or 0, "\n".join(lines)


async def _run_local_ansible_localhost(
    *, image: str, playbook_b64: str, conn_vars_b64: str, kubeconfig_b64: str,
    ps_env: dict | None = None,
) -> tuple:
    """Localhost Ansible play in a sibling container on the dashboard host — the
    on-prem Kubernetes path (``cloud="local"``).

    Every env value is written to a 0600 ``--env-file`` in a per-run temp directory
    rather than passed with ``-e``, so the kubeconfig's client key never lands in the
    host's process list. The directory is removed as soon as the container exits."""
    env: dict = {"PLAYBOOK_B64": playbook_b64}
    if conn_vars_b64:
        env["CONN_VARS_B64"] = conn_vars_b64
    if kubeconfig_b64:
        env["KUBECONFIG_B64"] = kubeconfig_b64
    env.update(ps_env or {})

    with tempfile.TemporaryDirectory(prefix="ansible_cloud_run_") as tmpdir:
        env_path = os.path.join(tmpdir, "run_env")
        # Docker env-file lines are literal KEY=VALUE with no shell interpolation,
        # so base64 blobs and a client secret are carried verbatim.
        with open(env_path, "w", newline="\n") as f:
            for k, v in env.items():
                f.write(f"{k}={v}\n")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass  # Windows NTFS — the file lives in the per-run tmpdir either way

        cmd = build_local_docker_argv(
            image=image, env_file_path=env_path,
            with_conn_vars=bool(conn_vars_b64), with_kubeconfig=bool(kubeconfig_b64))
        logger.info("ansible-cloud-run local: image=%s kubeconfig=%s conn_vars=%s",
                    image, bool(kubeconfig_b64), bool(conn_vars_b64))
        return await asyncio.to_thread(_run_local_docker_sync, cmd)


def check_target(kind: str, cloud: str, asset_backend: str, asset: str = "") -> str | None:
    """Validate a k8s/database Config-Management target against its asset storage.

    Returns the error detail for a 400, or ``None`` when the run may proceed. Lives
    here rather than inline in the endpoint because the two conditions interact and
    the interaction is the easy thing to get wrong:

    * which clouds are targetable depends on the target KIND (a database has no
      on-prem case), and
    * whether local filesystem assets are readable depends on WHERE THE RUNNER RUNS,
      which is itself derived from the cloud. The in-cloud runners can't see this
      host's disk; the local runner is this host, so for an on-prem cluster the
      storage restriction simply doesn't apply.

    Reads config (via resolve_runner) but changes nothing, so both branches are
    unit-testable without standing up the app.
    """
    cloud = (cloud or "").strip().lower()
    allowed = K8S_TARGET_CLOUDS if kind == "k8s" else DB_TARGET_CLOUDS
    if cloud not in allowed:
        return (f"cloud {cloud!r} has no Ansible runner for {kind} targets "
                f"(supported: {'/'.join(allowed)}).")
    if asset_backend == "local":
        try:
            runs_here = resolve_runner(cloud) == "local"
        except AnsibleCloudRunError as e:
            # A misconfigured ansible_runner_<cloud>. Report it as a validation error
            # now rather than letting the caller enqueue a job that dies in the worker.
            return str(e)
        if not runs_here:
            return (f"Asset {asset!r} lives on local filesystem storage, which the "
                    f"in-cloud runner cannot reach. Move it to a cloud backend (S3 / "
                    f"Azure Blob / GCS) on the Storage page, then re-run.")
    return None


async def _dispatch_cloud_localhost_runner(
    *, runner: str, image: str, job_id: str,
    playbook_b64: str, conn_vars_b64: str, kubeconfig_b64: str,
    ps_env: dict | None = None,
) -> tuple:
    """Route to the configured runner, reusing the k8s runner's infra resolution +
    validation (subnet / role / VPC connector — shared with the VM Ansible runner
    config) but overriding the image with ``ansible_cloud_image`` and the ECS task
    family. Returns ``(exit_code, output)``; a ``K8sRunnerError`` from the infra
    validation (missing subnet/role/…) propagates to the caller's ``set_failed``.

    ``ps_env`` (when present) is the auto-injected credential env — PASSWORD_SAFE_*
    and/or PORTAINER_* — for an in-playbook
    beyondtrust.secrets_safe lookup; it rides the runner's connection-material env
    channel (no cloud store)."""
    if runner == "local":
        # On-prem cluster: no cloud infra to resolve, so this branch takes none of
        # the subnet/role config the three below require.
        return await _run_local_ansible_localhost(
            image=image, playbook_b64=playbook_b64, conn_vars_b64=conn_vars_b64,
            kubeconfig_b64=kubeconfig_b64, ps_env=ps_env,
        )
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

        # Same treatment for the Portainer connection (PORTAINER_* env), merged into
        # the same runner env channel. {} when Portainer is disabled / unconfigured.
        from . import portainer_runner as _ptr
        _pt_env = _ptr.runner_env()
        _pt_secret = _pt_env.get(_ptr.SECRET_KEY)
        if _pt_secret and _pt_secret not in scrub_values:
            scrub_values.append(_pt_secret)
        ps_env = {**ps_env, **_pt_env}

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
