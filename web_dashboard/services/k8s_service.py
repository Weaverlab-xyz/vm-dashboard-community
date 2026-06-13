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

from ..database import K8sCluster

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


def create_cluster(db: Session, **kwargs) -> dict:
    """Provision a new cluster via Terraform (a ``terraform/k8s_cluster/*``
    module). Not in Phase 1 — provision the cluster out-of-band and call
    ``register_cluster``."""
    raise K8sError(
        "cluster provisioning (the terraform/k8s_cluster module) lands in a later "
        "sub-phase; register an existing cluster with register_cluster() instead"
    )


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


async def _apply_manifest_via_runner(kubeconfig: str, manifest_ref: str) -> str:
    """Apply a manifest into a cluster with a **transient kubectl container**
    (mirrors ansible_local_service's local-docker runner over the mounted
    docker.sock). ``manifest_ref`` is a URL (``kubectl apply -f <url>``) or
    inline YAML. The kubeconfig + manifest live in a tmpdir mounted into the
    one-shot container, which holds cluster-admin only for the apply."""
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
