"""Kubernetes cluster lifecycle — Phase 1 (register + record).

Phase 1 of docs/saas-kubernetes-management-plan.md. A **thin lifecycle**
service over the ``k8s_clusters`` table: register a reachable cluster from its
kubeconfig (the dev-testable path — the cluster is provisioned out-of-band),
store the kubeconfig as a **secrets-backend reference** (never in the row), and
list/get/delete. No kubectl wrapping and no live cluster calls — the
management-plane launch (Phase 2; Portainer-k8s first, then Rancher), brokered
access + native PRA ``tunnel_type=k8s`` jump (Phase 3), and in-cluster Password
Safe delivery (Phase 4) build on this.

Cloud-provisioned clusters (a ``terraform/k8s_cluster/*`` module) are a later
sub-phase; ``create_cluster`` raises until then — register an existing cluster.
"""
import asyncio
import base64
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urlparse

import yaml
from sqlalchemy.orm import Session

from ..database import Job, K8sCluster

logger = logging.getLogger(__name__)

VALID_CLOUDS = ("aws", "azure", "gcp", "local")
VALID_MGMT_KINDS = ("portainer", "rancher", "argocd", "headlamp")

# config_service key that holds a cluster's kubeconfig; the row stores this
# string as kubeconfig_ref and config_service.get() resolves it.
_KUBECONFIG_KEY = "k8s_kubeconfig_{cluster_id}"

# Phase 2 — management-plane launch. The apply runs in a transient kubectl
# container (mirrors ansible_local_service's local-docker runner) so the app
# process never holds cluster-admin. Phase 2's first plane is Portainer-k8s:
# deploy the Portainer Agent into the cluster, then register it in the Portainer
# server the dashboard already brokers (portainer_service.add_agent_endpoint).
_KUBECTL_IMAGE = "bitnami/kubectl:latest"
_PORTAINER_AGENT_MANIFEST_URL = "https://downloads.portainer.io/ce-lts/portainer-agent-k8s-nodeport.yaml"
_PORTAINER_AGENT_NODEPORT = 30778

# Phase 4 (Feature D) — in-cluster Password Safe secret delivery. The dashboard
# installs BeyondTrust's own integration rather than proxying secrets itself.
# v1 ships the External Secrets Operator path: ESO (Helm) + a BeyondTrust
# ClusterSecretStore that syncs Password Safe → native K8s Secrets. Helm runs in
# a transient container (same throwaway-runner pattern as the kubectl apply).
_HELM_IMAGE = "alpine/helm:latest"
_ESO_HELM_REPO_NAME = "external-secrets"
_ESO_HELM_REPO_URL = "https://charts.external-secrets.io"
_ESO_HELM_CHART = "external-secrets/external-secrets"


class K8sError(Exception):
    pass


def _parse_api_server(kubeconfig: str) -> str:
    """The current-context cluster's API server URL from a kubeconfig (or "")."""
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        current = cfg.get("current-context")
        clusters = {c["name"]: c.get("cluster", {}) for c in cfg.get("clusters", [])}
        if current:
            for ctx in cfg.get("contexts", []):
                if ctx.get("name") == current:
                    cl = ctx.get("context", {}).get("cluster")
                    return clusters.get(cl, {}).get("server", "") or ""
        if cfg.get("clusters"):
            return cfg["clusters"][0].get("cluster", {}).get("server", "") or ""
    except Exception as exc:
        logger.warning("kubeconfig parse failed: %s", exc)
    return ""


def _serialize(r: K8sCluster) -> dict:
    return {
        "id":                    r.id,
        "cloud":                 r.cloud,
        "name":                  r.name,
        "status":                r.status,
        "source":                r.source,
        "region":                r.region,
        "api_server":            r.api_server,
        "mgmt_kind":             r.mgmt_kind,
        "mgmt_endpoint":         r.mgmt_endpoint,
        "pra_jump_id":           r.pra_jump_id,
        "jump_group":            r.jump_group,
        "jumpoint_name":         r.jumpoint_name,
        "pra_credential_ref":    r.pra_credential_ref,
        "secrets_delivery_kind": r.secrets_delivery_kind,
        "created_by":            r.created_by,
        "created_at":            r.created_at.isoformat() if r.created_at else "",
    }


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_clusters(db: Session) -> list[dict]:
    rows = db.query(K8sCluster).order_by(K8sCluster.created_at.desc()).all()
    return [_serialize(r) for r in rows]


def get_cluster(db: Session, cluster_id: str) -> dict:
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    return _serialize(row)


def resolve_kubeconfig(db: Session, cluster_id: str) -> str:
    """The cluster's kubeconfig, resolved from its reference. For Phase 2+
    (apply a management plane); kept out of the row + list/get responses."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not row.kubeconfig_ref:
        raise K8sError(f"cluster {cluster_id} has no stored kubeconfig")
    from . import config_service
    kubeconfig = config_service.get(row.kubeconfig_ref)
    if not kubeconfig:
        raise K8sError(f"kubeconfig for {cluster_id} could not be resolved")
    return kubeconfig


# ── Writes ────────────────────────────────────────────────────────────────────

def register_cluster(db: Session, *, name: str, cloud: str, kubeconfig: str,
                     created_by: str, mgmt_kind: str = None) -> dict:
    """Record an existing reachable cluster. Parses the API server from the
    kubeconfig, stores the kubeconfig as a secrets-backend reference, and
    inserts a ``registered`` row. The kubeconfig is never written to the row."""
    name = (name or "").strip()
    if not name:
        raise K8sError("cluster name is required")
    if cloud not in VALID_CLOUDS:
        raise K8sError(f"unknown cloud {cloud!r} (expected one of {', '.join(VALID_CLOUDS)})")
    if mgmt_kind and mgmt_kind not in VALID_MGMT_KINDS:
        raise K8sError(f"unknown mgmt_kind {mgmt_kind!r} (expected one of {', '.join(VALID_MGMT_KINDS)})")
    if not (kubeconfig or "").strip():
        raise K8sError("kubeconfig is required")
    if db.query(K8sCluster).filter(K8sCluster.name == name).first():
        raise K8sError(f"a cluster named {name!r} is already registered")

    api_server = _parse_api_server(kubeconfig)
    if not api_server:
        raise K8sError("could not parse an API server from the kubeconfig")

    cluster_id = str(uuid.uuid4())
    ref = _KUBECONFIG_KEY.format(cluster_id=cluster_id)
    from . import config_service
    config_service.set(ref, kubeconfig)

    row = K8sCluster(
        id=cluster_id, cloud=cloud, name=name, status="registered",
        api_server=api_server, kubeconfig_ref=ref, mgmt_kind=mgmt_kind,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    logger.info("Registered k8s cluster %s (%s, api=%s)", name, cloud, api_server)
    return _serialize(row)


# ── §1.1a: cluster provisioning (Terraform) ───────────────────────────────────
# A per-cloud terraform/k8s_cluster/<dir> module driven by the terraform.py
# subprocess wrapper with a per-job_id deploy dir — the exact shape
# cloud_database_service proved. AWS EKS first; GCP/Azure fan out later. The
# generated kubeconfig is stored via the same secrets-backend path
# register_cluster uses, and the row flips to ``registered`` so every downstream
# flow (manage / broker / secrets / delete) applies unchanged.

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CLUSTER_TEMPLATE_DIRS = {
    "aws": os.path.join(_REPO_ROOT, "terraform", "k8s_cluster", "aws_eks"),
}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")
_PROVISION_IMPLEMENTED = ("aws",)


def _deploy_dir(job_id: str) -> str:
    return os.path.join(_DEPLOYMENTS_DIR, job_id)


def _cluster_template_dir(cloud: str) -> str:
    return _CLUSTER_TEMPLATE_DIRS[cloud]


def _eks_name(name: str) -> str:
    """A valid EKS cluster name from the dashboard cluster name: keep alnum + '-',
    ensure it starts alphanumeric, cap length."""
    slug = "".join(c if (c.isalnum() or c == "-") else "-" for c in (name or "")).strip("-")
    if not slug or not slug[0].isalnum():
        slug = "k8s-" + slug
    return slug[:100]


def create_cluster(db: Session, *, cloud: str, name: str, region: str,
                   created_by: str, **opts) -> dict:
    """Provision a new cluster with Terraform (the ``terraform/k8s_cluster/<cloud>``
    module), then store its kubeconfig and flip to ``registered`` (§1.1a).

    Synchronous record-keeping only — validate, insert a ``provisioning`` row + a
    ``k8s_provision`` Job, and return the Terraform variables the apply will use.
    Does **not** run Terraform — the API schedules :func:`run_provision_apply` as a
    background task. Mirrors ``cloud_database_service.provision``."""
    name = (name or "").strip()
    if not name:
        raise K8sError("cluster name is required")
    if cloud not in VALID_CLOUDS:
        raise K8sError(f"unknown cloud {cloud!r} (expected one of {', '.join(VALID_CLOUDS)})")
    if cloud not in _PROVISION_IMPLEMENTED:
        raise NotImplementedError(
            f"cluster provisioning for {cloud!r} is not wired yet — §1.1a implements aws (EKS) first"
        )
    if not (region or "").strip():
        raise K8sError("region is required")
    if db.query(K8sCluster).filter(K8sCluster.name == name).first():
        raise K8sError(f"a cluster named {name!r} is already registered")

    cluster_id = str(uuid.uuid4())
    row = K8sCluster(
        id=cluster_id, cloud=cloud, name=name, status="provisioning",
        source="provisioned", region=region, created_by=created_by,
    )
    db.add(row)
    db.commit()

    from . import job_service
    job = job_service.create_job(
        db, job_type="k8s_provision", created_by=created_by,
        metadata={"cluster_id": cluster_id, "cloud": cloud, "name": name, "region": region},
    )
    row.deploy_job_id = job.id
    db.commit()

    tf_variables = _build_cluster_tf_variables(
        cloud=cloud, cluster_id=cluster_id, name=name, region=region, opts=opts)
    logger.info("k8s provision record cluster_id=%s cloud=%s name=%s job_id=%s",
                cluster_id, cloud, name, job.id)
    return {"ok": True, "cluster_id": cluster_id, "job_id": job.id, "tf_variables": tf_variables}


def _build_cluster_tf_variables(*, cloud: str, cluster_id: str, name: str,
                                region: str, opts: dict) -> dict:
    """The Terraform ``-var`` set for the cluster module. §1.1a: aws (EKS).

    Subnet ids default to the two private k8s subnets the sandbox emits
    (``aws_k8s_subnet_a_id`` / ``aws_k8s_subnet_b_id``); k8s version + node size
    fall back to config then the module defaults."""
    if cloud == "aws":
        subnets = opts.get("subnet_ids") or [
            s for s in (_cfg("aws_k8s_subnet_a_id"), _cfg("aws_k8s_subnet_b_id")) if s
        ]
        tf = {
            "region": region,
            "cluster_name": _eks_name(f"k8s-{name}"),
            "subnet_ids": subnets,
            "tags": {"managed-by": "vm-dashboard", "k8s-cluster-id": cluster_id},
        }
        version = opts.get("k8s_version") or _cfg("aws_eks_k8s_version")
        if version:
            tf["k8s_version"] = version
        node_type = opts.get("node_instance_type") or _cfg("aws_eks_node_instance_type")
        if node_type:
            tf["node_instance_type"] = node_type
        if opts.get("node_count"):
            tf["node_desired"] = int(opts["node_count"])
        return tf
    raise NotImplementedError(f"{cloud} cluster Terraform variables not implemented")


def _assemble_eks_kubeconfig(*, cluster_name: str, endpoint: str, ca_b64: str,
                             region: str) -> str:
    """An exec-based kubeconfig for an EKS cluster: API server + inline CA + an
    ``aws eks get-token`` exec block. The CA is inline so the PRA tunnel's
    ``_parse_ca_cert`` works; auth is via the exec plugin. The transient
    kubectl/helm runner can't run the ``aws`` plugin, so ``_runner_kubeconfig``
    swaps in a server-minted bearer token (``aws_service.eks_get_token``) for
    runner use."""
    cfg = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": cluster_name,
            "cluster": {"server": endpoint, "certificate-authority-data": ca_b64},
        }],
        "contexts": [{
            "name": cluster_name,
            "context": {"cluster": cluster_name, "user": cluster_name},
        }],
        "current-context": cluster_name,
        "users": [{
            "name": cluster_name,
            "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": "aws",
                "args": ["eks", "get-token", "--cluster-name", cluster_name,
                         "--region", region],
            }},
        }],
    }
    return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)


async def run_provision_apply(db: Session, *, cluster_id: str, job_id: str,
                              cloud: str, tf_variables: dict) -> None:
    """**§1.1a** background task: ``terraform apply`` the cluster module, assemble a
    kubeconfig from its outputs, store it as a secrets-backend reference (the same
    path :func:`register_cluster` uses), and flip the row to ``registered`` — after
    which the Phase 2-4 flows treat it like any registered cluster. Marks the row +
    job failed on apply error. Mirrors ``cloud_database_service.run_provision_apply``."""
    from . import config_service, job_service, terraform, terraform_provider_env
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if not row:
        logger.warning("k8s provision: row %s vanished", cluster_id)
        return
    job_service.set_running(db, job_id)
    try:
        outputs = await terraform.apply(
            _deploy_dir(job_id), tf_variables,
            template_dir=_cluster_template_dir(cloud),
            env=terraform_provider_env.provider_env(cloud),
        )
        endpoint = str(outputs.get("endpoint") or "")
        ca_b64 = str(outputs.get("ca_certificate") or "")
        eks_name = str(outputs.get("cluster_name") or tf_variables.get("cluster_name") or row.name)
        if not (endpoint and ca_b64):
            raise K8sError("cluster apply did not return endpoint + ca_certificate outputs")

        kubeconfig = _assemble_eks_kubeconfig(
            cluster_name=eks_name, endpoint=endpoint, ca_b64=ca_b64, region=row.region or "")
        ref = _KUBECONFIG_KEY.format(cluster_id=cluster_id)
        config_service.set(ref, kubeconfig)
        row.kubeconfig_ref = ref
        row.api_server = endpoint
        row.status = "registered"
        db.commit()
        job_service.set_completed(db, job_id)
        logger.info("k8s provision complete cluster_id=%s eks=%s endpoint=%s",
                    cluster_id, eks_name, endpoint)
    except Exception as exc:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("k8s provision failed cluster_id=%s: %s", cluster_id, exc)


def start_decommission(db: Session, cluster_id: str, created_by: str = "") -> dict:
    """Record intent to decommission a **provisioned** cluster + schedule teardown:
    flip to ``decommissioning`` and create a ``k8s_decommission`` Job. The teardown
    (PRA tunnel → ``terraform destroy`` → drop the record) runs in
    :func:`run_decommission` as a background task — it's many minutes long and must
    not block the request. Mirrors ``cloud_database_service.start_decommission``."""
    from . import job_service
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if row.status == "decommissioning":
        existing = (db.query(Job)
                      .filter(Job.job_type == "k8s_decommission")
                      .order_by(Job.created_at.desc()).all())
        job = next((j for j in existing if (j.metadata_dict or {}).get("cluster_id") == cluster_id), None)
        if job:
            return {"ok": True, "cluster_id": cluster_id, "job_id": job.id}
    row.status = "decommissioning"
    db.commit()
    job = job_service.create_job(
        db, job_type="k8s_decommission", created_by=created_by or row.created_by or "system",
        metadata={"cluster_id": cluster_id, "cloud": row.cloud, "name": row.name},
    )
    return {"ok": True, "cluster_id": cluster_id, "job_id": job.id}


async def run_decommission(db: Session, *, cluster_id: str, job_id: str) -> None:
    """Background teardown for a **provisioned** cluster: best-effort remove the PRA
    tunnel, then ``terraform destroy`` the cluster, then drop the record + stored
    kubeconfig. Errors are ACCUMULATED → the row/job end ``failed`` (an orphaned
    cluster stays visible) rather than a false ``decommissioned``. Mirrors
    ``cloud_database_service.run_decommission``."""
    from . import config_service, job_service, terraform, terraform_provider_env
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if not row:
        job_service.set_failed(db, job_id, f"cluster {cluster_id} not found")
        return
    job_service.set_running(db, job_id)
    errors: list = []

    # 1. PRA tunnel (best-effort; clears pra_jump_id / pra_tunnel_state on the row).
    job_service.update_progress(db, job_id, 15, "Removing PRA tunnel…")
    try:
        await deregister_pra_tunnel(db, cluster_id)
    except Exception as exc:
        errors.append(f"PRA tunnel removal: {exc}")
        logger.warning("k8s decommission: tunnel removal for %s failed: %s", cluster_id, exc)

    # 2. terraform destroy (the long step). State lives in the active storage
    #    backend, so destroy recovers a deploy dir lost to a container recreate —
    #    pass template_dir so the module is rebuilt from it + the remote state.
    job_service.update_progress(db, job_id, 40, "Destroying the cluster…")
    if row.deploy_job_id:
        try:
            # terraform destroy evaluates the module config, so it needs the same
            # -var set apply used (else "No value for required variable"). The values
            # don't change what's destroyed (resources come from state), but the
            # provider's region must be correct — reconstruct from the row.
            destroy_vars = _build_cluster_tf_variables(
                cloud=row.cloud, cluster_id=row.id, name=row.name,
                region=row.region or "", opts={})
            await terraform.destroy(
                _deploy_dir(row.deploy_job_id),
                env=terraform_provider_env.provider_env(row.cloud),
                template_dir=_cluster_template_dir(row.cloud),
                variables=destroy_vars,
            )
            logger.info("k8s cluster destroyed cluster_id=%s cloud=%s", cluster_id, row.cloud)
        except Exception as exc:
            errors.append(f"cluster destroy: {exc}")
            logger.warning("k8s destroy for %s failed: %s", cluster_id, exc)
    else:
        errors.append("no provisioning job recorded — the cluster may need manual "
                      "teardown in the cloud console")

    if errors:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, "; ".join(errors))
        logger.error("k8s decommission cluster_id=%s ended with errors: %s", cluster_id, errors)
        return

    # 3. Drop the record + stored kubeconfig (same as the register-delete path).
    name = row.name
    if row.kubeconfig_ref:
        try:
            config_service.set(row.kubeconfig_ref, "")
        except Exception as exc:
            logger.warning("k8s decommission: clearing kubeconfig for %s failed: %s", cluster_id, exc)
    db.delete(row)
    db.commit()
    job_service.set_completed(db, job_id, {"cluster_id": cluster_id, "deregistered": name})
    logger.info("k8s decommissioned cluster_id=%s", cluster_id)


def delete_cluster(db: Session, cluster_id: str) -> dict:
    """Drop the cluster record and its stored kubeconfig. Phase 1 deregisters;
    it does not tear down a cloud-provisioned cluster (no Terraform yet)."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if row.kubeconfig_ref:
        from . import config_service
        try:
            config_service.set(row.kubeconfig_ref, "")
        except Exception as exc:
            logger.warning("clearing kubeconfig for %s failed: %s", cluster_id, exc)
    name = row.name
    db.delete(row)
    db.commit()
    logger.info("Deregistered k8s cluster %s", name)
    return {"ok": True, "deregistered": name}


# ── Phase 2: management-plane launch ───────────────────────────────────────────

def _api_host(api_server: str) -> str:
    """The hostname/IP from a cluster API URL (where the agent NodePort is
    reachable). Best-effort — the live test confirms the agent's actual
    reachable address for the operator's network."""
    try:
        return urlparse(api_server).hostname or ""
    except Exception:
        return ""


def _run_sync(cmd: list) -> str:
    """Run a command, returning stdout; raise K8sError with stderr on failure.
    asyncio's subprocess support is unreliable under uvicorn's SelectorEventLoop
    on Windows, so callers wrap this in asyncio.to_thread (same as the rest of
    the codebase)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise K8sError(f"command failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _runner_kubeconfig(kubeconfig: str) -> str:
    """Prepare a kubeconfig for a transient kubectl/helm runner. A provisioned EKS
    cluster's kubeconfig authenticates with an ``aws eks get-token`` **exec** block
    (see :func:`_assemble_eks_kubeconfig`), but the throwaway container has no
    ``aws`` CLI or AWS creds — so for that case mint a short-lived bearer token
    server-side (``aws_service.eks_get_token``) and swap the exec block for a static
    ``token``. Any other kubeconfig (registered clusters using token / client-cert /
    non-aws exec auth) is returned **unchanged**. Best-effort: on any parse/mint
    error, return the original so non-EKS paths are unaffected."""
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        users = cfg.get("users") or []
        # The current-context's user (fall back to the only/first user).
        user_name = None
        current = cfg.get("current-context")
        if current:
            for ctx in cfg.get("contexts", []):
                if ctx.get("name") == current:
                    user_name = ctx.get("context", {}).get("user")
                    break
        entry = next((u for u in users if u.get("name") == user_name), None) or (users[0] if users else None)
        if not entry:
            return kubeconfig
        exec_blk = (entry.get("user") or {}).get("exec") or {}
        args = exec_blk.get("args") or []
        if exec_blk.get("command") != "aws" or "get-token" not in args:
            return kubeconfig  # not an `aws eks get-token` kubeconfig — leave as-is

        def _arg(flag: str) -> str:
            return args[args.index(flag) + 1] if (flag in args and args.index(flag) + 1 < len(args)) else ""
        cluster_name = _arg("--cluster-name")
        region = _arg("--region")
        if not cluster_name:
            return kubeconfig
        from . import aws_service
        entry["user"] = {"token": aws_service.eks_get_token(cluster_name, region)}
        return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        logger.warning("runner kubeconfig token-prep failed (using exec kubeconfig as-is): %s", exc)
        return kubeconfig


async def _apply_manifest_via_runner(kubeconfig: str, manifest_ref: str) -> str:
    """Apply a manifest into a cluster with a **transient kubectl container**
    (mirrors ansible_local_service's local-docker runner over the mounted
    docker.sock). ``manifest_ref`` is a URL (``kubectl apply -f <url>``) or
    inline YAML. The kubeconfig + manifest live in a tmpdir mounted into the
    one-shot container, which holds cluster-admin only for the apply."""
    kubeconfig = _runner_kubeconfig(kubeconfig)
    tmpdir = tempfile.mkdtemp(prefix="k8s_apply_")
    try:
        with open(os.path.join(tmpdir, "kubeconfig"), "w") as fh:
            fh.write(kubeconfig)
        if manifest_ref.startswith(("http://", "https://")):
            apply_target = manifest_ref
        else:
            with open(os.path.join(tmpdir, "manifest.yaml"), "w") as fh:
                fh.write(manifest_ref)
            apply_target = "/work/manifest.yaml"
        shell_cmd = f"kubectl --kubeconfig /work/kubeconfig apply -f {shlex.quote(apply_target)}"
        cmd = ["docker", "run", "--rm", "-v", f"{tmpdir}:/work", _KUBECTL_IMAGE, "sh", "-c", shell_cmd]
        logger.info("k8s apply: image=%s target=%s", _KUBECTL_IMAGE, apply_target)
        return await asyncio.to_thread(_run_sync, cmd)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _delete_manifest_via_runner(kubeconfig: str, manifest: str) -> str:
    """``kubectl delete -f`` an inline manifest (best-effort teardown)."""
    kubeconfig = _runner_kubeconfig(kubeconfig)
    tmpdir = tempfile.mkdtemp(prefix="k8s_del_")
    try:
        with open(os.path.join(tmpdir, "kubeconfig"), "w") as fh:
            fh.write(kubeconfig)
        with open(os.path.join(tmpdir, "manifest.yaml"), "w") as fh:
            fh.write(manifest)
        shell_cmd = "kubectl --kubeconfig /work/kubeconfig delete --ignore-not-found -f /work/manifest.yaml"
        cmd = ["docker", "run", "--rm", "-v", f"{tmpdir}:/work", _KUBECTL_IMAGE, "sh", "-c", shell_cmd]
        return await asyncio.to_thread(_run_sync, cmd)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _helm_via_runner(kubeconfig: str, helm_args: list, add_eso_repo: bool = True) -> str:
    """Run a helm command against the cluster in a transient helm container
    (KUBECONFIG mounted, same throwaway pattern as the kubectl runner)."""
    kubeconfig = _runner_kubeconfig(kubeconfig)
    tmpdir = tempfile.mkdtemp(prefix="k8s_helm_")
    try:
        with open(os.path.join(tmpdir, "kubeconfig"), "w") as fh:
            fh.write(kubeconfig)
        parts = []
        if add_eso_repo:
            parts.append(f"helm repo add {_ESO_HELM_REPO_NAME} {_ESO_HELM_REPO_URL}")
            parts.append("helm repo update")
        parts.append("helm " + " ".join(shlex.quote(a) for a in helm_args))
        shell_cmd = " && ".join(parts)
        cmd = ["docker", "run", "--rm", "--entrypoint", "/bin/sh",
               "-e", "KUBECONFIG=/work/kubeconfig", "-v", f"{tmpdir}:/work",
               _HELM_IMAGE, "-c", shell_cmd]
        logger.info("k8s helm: %s", " ".join(helm_args[:3]))
        return await asyncio.to_thread(_run_sync, cmd)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _yaml_quote(value: str) -> str:
    """Double-quoted YAML scalar, safe for arbitrary strings."""
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _eso_credentials_secret_manifest(namespace: str, secret_name: str,
                                     client_id: str, client_secret: str) -> str:
    """Namespace + the K8s Secret the ESO BeyondTrust ClusterSecretStore reads its
    Password Safe OAuth client id/secret from (keys ClientId / ClientSecret)."""
    return (
        "apiVersion: v1\n"
        "kind: Namespace\n"
        "metadata:\n"
        f"  name: {namespace}\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {secret_name}\n"
        f"  namespace: {namespace}\n"
        "type: Opaque\n"
        "stringData:\n"
        f"  ClientId: {_yaml_quote(client_id)}\n"
        f"  ClientSecret: {_yaml_quote(client_secret)}\n"
    )


def _eso_clustersecretstore_manifest(name: str, api_url: str, retrieval_type: str,
                                     secret_namespace: str, secret_name: str,
                                     bt_api_version: str = "3.1") -> str:
    """A BeyondTrust ClusterSecretStore (external-secrets.io/v1) that syncs Password
    Safe → K8s Secrets. Schema confirmed against the ESO BeyondTrust provider docs;
    OAuth client id/secret come from the ``secret_name`` Secret (ClusterSecretStore
    is cluster-scoped, so the secretRef carries an explicit namespace)."""
    return (
        "apiVersion: external-secrets.io/v1\n"
        "kind: ClusterSecretStore\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  provider:\n"
        "    beyondtrust:\n"
        "      server:\n"
        f"        apiUrl: {_yaml_quote(api_url)}\n"
        f"        retrievalType: {retrieval_type}\n"
        "        verifyCA: true\n"
        "        clientTimeOutSeconds: 45\n"
        f"        apiVersion: {_yaml_quote(bt_api_version)}\n"
        "      auth:\n"
        "        clientId:\n"
        "          secretRef:\n"
        f"            name: {secret_name}\n"
        f"            namespace: {secret_namespace}\n"
        "            key: ClientId\n"
        "        clientSecret:\n"
        "          secretRef:\n"
        f"            name: {secret_name}\n"
        f"            namespace: {secret_namespace}\n"
        "            key: ClientSecret\n"
    )


def _eso_bt_api_url() -> str:
    """The BeyondTrust public API URL for the ESO provider. Explicit override
    (eso_bt_api_url) wins; otherwise derive from the Password Safe URL."""
    override = _cfg("eso_bt_api_url")
    if override:
        return override
    base = (_cfg("pscli_api_url") or "").rstrip("/")
    return f"{base}/BeyondTrust/api/public/v3/" if base else ""


VALID_DELIVERY_KINDS = ("eso", "none")


async def setup_secret_delivery(cluster_id: str, kind: str) -> None:
    """**Phase 4 (Feature D)** — install BeyondTrust's in-cluster Password Safe
    secret delivery into a managed cluster. Background task. v1 supports the
    External Secrets Operator path:

      * ``kind=eso``  → Helm-install ESO, write the BeyondTrust OAuth credentials
        Secret, and apply a BeyondTrust ClusterSecretStore (Password Safe →
        native K8s Secrets). Records ``secrets_delivery_kind=eso``.
      * ``kind=none`` → remove the ClusterSecretStore + credentials and uninstall
        ESO (best-effort); clears ``secrets_delivery_kind``.

    The dashboard installs + configures only — ESO owns the sync; secrets are
    never proxied through the dashboard. (Secrets-Agent is a later kind.)"""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is None:
            return
        kubeconfig = resolve_kubeconfig(db, cluster_id)
        namespace = _cfg("eso_namespace", "external-secrets")
        secret_name = _cfg("eso_bt_credentials_secret", "beyondtrust-credentials")
        css_name = _cfg("eso_bt_clustersecretstore", "beyondtrust-store")

        if kind == "none":
            try:
                await _delete_manifest_via_runner(kubeconfig, _eso_clustersecretstore_manifest(
                    css_name, _eso_bt_api_url() or "https://x/", _cfg("eso_bt_retrieval_type", "SECRET"),
                    namespace, secret_name))
                await _helm_via_runner(kubeconfig, ["uninstall", "external-secrets", "-n", namespace],
                                       add_eso_repo=False)
            except Exception as exc:
                logger.warning("ESO teardown for %s partially failed: %s", cluster_id, exc)
            row.secrets_delivery_kind = None
            db.commit()
            return

        # kind == "eso"
        from . import config_service
        client_id = config_service.get("pscli_client_id")
        client_secret = config_service.get("pscli_client_secret")
        api_url = _eso_bt_api_url()
        if not (client_id and client_secret and api_url):
            raise K8sError(
                "ESO secret delivery needs the Password Safe API URL + OAuth client "
                "(pscli_api_url, pscli_client_id, pscli_client_secret) configured"
            )
        row.secrets_delivery_kind = "installing"
        db.commit()
        try:
            ver = _cfg("eso_helm_version")
            helm_args = ["upgrade", "--install", "external-secrets", _ESO_HELM_CHART,
                         "--namespace", namespace, "--create-namespace", "--wait",
                         "--set", "installCRDs=true"]
            if ver:
                helm_args += ["--version", ver]
            await _helm_via_runner(kubeconfig, helm_args)
            await _apply_manifest_via_runner(kubeconfig, _eso_credentials_secret_manifest(
                namespace, secret_name, client_id, client_secret))
            await _apply_manifest_via_runner(kubeconfig, _eso_clustersecretstore_manifest(
                css_name, api_url, _cfg("eso_bt_retrieval_type", "SECRET"),
                namespace, secret_name, _cfg("eso_bt_api_version", "3.1")))
            row.secrets_delivery_kind = "eso"
            db.commit()
            logger.info("ESO + Password Safe secret delivery installed on cluster %s", row.name)
        except Exception as exc:
            row.secrets_delivery_kind = "failed"
            db.commit()
            logger.warning("ESO secret delivery install failed cluster=%s: %s", cluster_id, exc)
    finally:
        db.close()


async def launch_management_plane(cluster_id: str, mgmt_kind: str = "portainer") -> None:
    """**Phase 2** — launch a management plane into a registered cluster, then
    register it in the brokered Portainer server. Scheduled as a background task
    by the API (the apply is slow).

    Phase 2's first plane is **Portainer-k8s** (operator's chosen model: agent +
    brokered server): apply the Portainer Agent into the cluster, then
    ``portainer_service.add_agent_endpoint`` registers it as an endpoint in the
    Portainer server the dashboard already brokers. Other ``mgmt_kind`` values
    are accepted by Phase 1 registration but not yet launched here."""
    from ..database import SessionLocal
    from . import portainer_service
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is None:
            return
        if mgmt_kind != "portainer":
            row.status = "failed"
            db.commit()
            logger.warning("management-plane launch: only 'portainer' is wired in Phase 2 (got %r)", mgmt_kind)
            return
        row.status = "deploying"
        db.commit()
        try:
            kubeconfig = resolve_kubeconfig(db, cluster_id)
            await _apply_manifest_via_runner(kubeconfig, _PORTAINER_AGENT_MANIFEST_URL)
            host = _api_host(row.api_server)
            endpoint = await portainer_service.add_agent_endpoint(
                name=row.name, ip=host, port=_PORTAINER_AGENT_NODEPORT)
            row.mgmt_kind = "portainer"
            row.mgmt_endpoint = str(endpoint.get("Id") or endpoint.get("Name") or host)
            row.status = "managed"
            db.commit()
            logger.info("Cluster %s management plane up (portainer endpoint %s)", row.name, row.mgmt_endpoint)
        except Exception as exc:
            row.status = "failed"
            db.commit()
            logger.warning("management-plane launch failed cluster=%s: %s", cluster_id, exc)
    finally:
        db.close()


# ── Phase 3: brokered access ───────────────────────────────────────────────────

def console_url(db: Session, cluster_id: str) -> dict:
    """A link to the cluster's management console (Phase 3a). For **Portainer-k8s**
    the cluster was registered as a Portainer endpoint at launch (Phase 2), so the
    console is that endpoint's view on the Portainer server the dashboard already
    brokers — built from the configured server URL + the endpoint id stored in
    ``mgmt_endpoint``. For a plane whose ``mgmt_endpoint`` is already a URL
    (Rancher / Argo ingress), return it directly. (The native PRA
    ``tunnel_type=k8s`` jump + a true short-lived brokered session are Phase 3b.)"""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not row.mgmt_kind or not row.mgmt_endpoint:
        raise K8sError("no management plane launched yet — launch one first")
    ep = row.mgmt_endpoint
    if ep.startswith(("http://", "https://")):
        return {"url": ep, "kind": row.mgmt_kind}
    if row.mgmt_kind == "portainer":
        from ..config import settings
        from . import config_service
        base = (config_service.get("portainer_url") or getattr(settings, "portainer_url", "")).rstrip("/")
        if not base:
            raise K8sError("Portainer server URL is not configured (set portainer_url)")
        return {"url": f"{base}/#!/{ep}/kubernetes/dashboard", "kind": "portainer", "endpoint_id": ep}
    raise K8sError(f"no console URL builder for mgmt_kind={row.mgmt_kind!r}")


# ── Phase 3b: native PRA tunnel_type=k8s jump (beyondtrust/sra) + Entitle JIT ──
#
# Community provisions the PRA tunnel with the beyondtrust/sra Terraform provider
# (terraform_pra_service) — never btapi — matching the managed-database tunnel.
# The tunnel routes through a configurable Jumpoint (the "separate jumpoint"),
# looked up by NAME. The in-cluster Jumpoint pod (the prod plan's primary host)
# is out of scope here.

def _cfg(key: str, default: str = "") -> str:
    from . import config_service
    from ..config import settings
    val = config_service.get(key)
    if val:
        return val
    return str(getattr(settings, key, "") or default)


def _parse_ca_cert(kubeconfig: str) -> str:
    """The current-context cluster's CA certificate (PEM) from a kubeconfig.
    Decodes ``certificate-authority-data``; "" when the kubeconfig uses an
    on-disk CA path or insecure-skip-tls (the k8s tunnel requires the inline cert)."""
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        clusters = {c["name"]: c.get("cluster", {}) for c in cfg.get("clusters", [])}
        target = None
        current = cfg.get("current-context")
        if current:
            for ctx in cfg.get("contexts", []):
                if ctx.get("name") == current:
                    target = clusters.get(ctx.get("context", {}).get("cluster"))
                    break
        if target is None and cfg.get("clusters"):
            target = cfg["clusters"][0].get("cluster", {})
        data = (target or {}).get("certificate-authority-data")
        if data:
            return base64.b64decode(data).decode("utf-8", "replace")
    except Exception as exc:
        logger.warning("kubeconfig CA parse failed: %s", exc)
    return ""


def _pra_configured() -> bool:
    """True when the sra provider creds + a Jumpoint name for the k8s tunnel are set."""
    return bool(_cfg("bt_api_host") and _cfg("bt_client_id")
                and (_cfg("bt_jumpoint_name")))


def _api_host_from_url(api_url: str) -> str:
    try:
        return urlparse(api_url).hostname or api_url
    except Exception:
        return api_url


def _apply_overrides(row, *, jump_group=None, jumpoint_name=None, pra_credential_ref=None) -> None:
    """Persist provided per-cluster overrides (blank clears, None leaves unchanged)."""
    if jump_group is not None:
        row.jump_group = jump_group.strip() or None
    if jumpoint_name is not None:
        row.jumpoint_name = jumpoint_name.strip() or None
    if pra_credential_ref is not None:
        row.pra_credential_ref = pra_credential_ref.strip() or None


async def register_pra_tunnel(db: Session, cluster_id: str, *, jump_group: str = None,
                              jumpoint_name: str = None, pra_credential_ref: str = None) -> dict:
    """Provision the cluster's ``tunnel_type=k8s`` sra protocol-tunnel jump and
    record its id + Terraform state on the row. Idempotent: returns the existing
    ``pra_jump_id`` without recreating when one is already set.

    Per-cluster overrides (persisted; config is the fallback): ``jump_group`` (else
    ``bt_jump_group_name``), ``jumpoint_name`` (else ``bt_jumpoint_name`` — the
    Jumpoint the tunnel routes through), ``pra_credential_ref`` (a secret ref
    resolved to a bt_client_secret override for the apply)."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not _pra_configured():
        raise K8sError(
            "PRA is not configured — set bt_api_host, bt_client_id/secret, and a "
            "Jumpoint name (bt_jumpoint_name, the Jumpoint the k8s tunnel routes through)"
        )
    _apply_overrides(row, jump_group=jump_group, jumpoint_name=jumpoint_name,
                     pra_credential_ref=pra_credential_ref)
    if row.pra_jump_id:
        db.commit()
        return {"pra_jump_id": row.pra_jump_id, "already_registered": True}

    kubeconfig = resolve_kubeconfig(db, cluster_id)
    api_url = row.api_server or _parse_api_server(kubeconfig)
    if not api_url:
        raise K8sError("cluster API URL is unknown — cannot register a k8s tunnel")
    ca = _parse_ca_cert(kubeconfig)
    if not ca:
        raise K8sError(
            "could not extract a CA certificate from the kubeconfig — the k8s "
            "tunnel requires inline ca_certificates (certificate-authority-data)"
        )

    from . import config_service, terraform_pra_service as pra
    cred_ref = row.pra_credential_ref
    client_secret = config_service.resolve_reference(cred_ref) if cred_ref else ""
    result = await pra.provision_k8s_tunnel(
        name=f"k8s-{row.name}",
        hostname=_api_host_from_url(api_url),
        api_url=api_url,
        ca_certificates=ca,
        jump_group_name=row.jump_group or _cfg("bt_jump_group_name"),
        jumpoint_name=row.jumpoint_name or _cfg("bt_jumpoint_name"),
        client_secret=client_secret,
    )
    row.pra_jump_id = str(result.get("tunnel_jump_id") or "")
    row.pra_tunnel_state = result.get("tf_state_json")
    db.commit()
    logger.info("Registered k8s PRA tunnel for cluster %s (jump id %s)", row.name, row.pra_jump_id)
    return {"pra_jump_id": row.pra_jump_id, "jump_group_name": result.get("jump_group_name")}


async def deregister_pra_tunnel(db: Session, cluster_id: str) -> dict:
    """Destroy the cluster's k8s tunnel jump from its stored Terraform state and
    clear ``pra_jump_id``/``pra_tunnel_state`` (best-effort)."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not row.pra_jump_id:
        return {"ok": True, "removed": False}
    if row.pra_tunnel_state:
        from . import terraform_pra_service as pra
        try:
            await pra.remove_k8s_tunnel(row.pra_tunnel_state)
        except Exception as exc:
            logger.warning("removing k8s tunnel for %s failed: %s", cluster_id, exc)
    row.pra_jump_id = None
    row.pra_tunnel_state = None
    db.commit()
    return {"ok": True, "removed": True}


async def _entitle_rancher_grant(cluster_id: str, username: str) -> dict:
    """Open a time-boxed Entitle request for Rancher RBAC (the JIT *authorization*
    layer; the PRA tunnel is the *connection*). Only meaningful for
    ``mgmt_kind=rancher`` — Entitle's only management-plane connector."""
    from . import config_service, entitle_service
    from ..config import settings
    bundle = _cfg("k8s_rancher_entitle_bundle")
    if not bundle:
        raise K8sError(
            "k8s_rancher_entitle_bundle is not configured (the Entitle bundle/role "
            "id that grants the requested Rancher RBAC)"
        )
    email = (config_service.get("entitle_machine_identity_email")
             or getattr(settings, "entitle_machine_identity_email", "") or username)
    duration = int(_cfg("k8s_entitle_duration_minutes", "60") or 60)
    payload_hash = hashlib.sha256(f"{cluster_id}:{username}".encode()).hexdigest()
    rid = await entitle_service.submit_machine_request(
        operation="k8s_rancher_console",
        target={"bundle_id": bundle},
        duration_minutes=duration,
        payload_hash=payload_hash,
        behalf_of_email=email,
        justification=f"Rancher console access for {username} (cluster {cluster_id})",
    )
    payload = await entitle_service.get_machine_request(rid)
    category, reason = entitle_service.classify_machine_status(payload)
    return {"request_id": rid, "status": category, "reason": reason}


async def open_console(db: Session, cluster_id: str, username: str = "system", *,
                       jump_group: str = None, jumpoint_name: str = None,
                       pra_credential_ref: str = None) -> dict:
    """**Phase 3b** — broker access. Layers the PRA tunnel (connection) + the
    Entitle-Rancher JIT (authorization) over the Phase-3a console link:

      * **PRA configured** → ensure the sra ``tunnel_type=k8s`` jump exists and
        return a ``pra_tunnel`` descriptor (connect via the PRA representative
        console — no public ingress).
      * **otherwise** → the Phase-3a brokered ingress (``console_url``).
      * **mgmt_kind=rancher + entitle_enabled** → also open a time-boxed Entitle
        grant and report its status.

    The optional jump_group / jumpoint_name / pra_credential_ref are per-cluster
    overrides (config defaults are the fallback)."""
    from . import config_service
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    _apply_overrides(row, jump_group=jump_group, jumpoint_name=jumpoint_name,
                     pra_credential_ref=pra_credential_ref)
    db.commit()
    out: dict = {"cluster_id": cluster_id, "name": row.name, "mgmt_kind": row.mgmt_kind}

    if row.mgmt_kind == "rancher" and config_service.get_bool("entitle_enabled", True):
        out["entitle"] = await _entitle_rancher_grant(cluster_id, username)

    if _pra_configured():
        if not row.pra_jump_id:
            await register_pra_tunnel(db, cluster_id)
            db.refresh(row)
        out["access"] = "pra_tunnel"
        out["pra"] = {
            "tunnel_jump_id": row.pra_jump_id,
            "jump_group": row.jump_group or _cfg("bt_jump_group_name"),
            "jumpoint": row.jumpoint_name or _cfg("bt_jumpoint_name"),
            "note": "Connect through the BeyondTrust PRA representative console — no public ingress.",
        }
    else:
        out["access"] = "ingress"
        out.update(console_url(db, cluster_id))
    return out
