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
import logging
import uuid

import yaml
from sqlalchemy.orm import Session

from ..database import K8sCluster

logger = logging.getLogger(__name__)

VALID_CLOUDS = ("aws", "azure", "gcp", "local")
VALID_MGMT_KINDS = ("portainer", "rancher", "argocd", "headlamp")

# config_service key that holds a cluster's kubeconfig; the row stores this
# string as kubeconfig_ref and config_service.get() resolves it.
_KUBECONFIG_KEY = "k8s_kubeconfig_{cluster_id}"


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
