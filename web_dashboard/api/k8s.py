"""Kubernetes management API — Phase 1.

Gated on ``k8s_management_enabled`` (feature-gate dependency). Phase 1
registers/lists managed clusters via ``k8s_service`` and stores the kubeconfig
as a backend reference — no provisioning, no kubectl, no brokering yet. See
docs/saas-kubernetes-management-plan.md.

  GET    /api/k8s/__phase1__          — health check (router-mounted probe)
  GET    /api/k8s/clusters            — list managed clusters
  POST   /api/k8s/clusters            — register an existing cluster (kubeconfig)
  GET    /api/k8s/clusters/{id}       — one cluster
  DELETE /api/k8s/clusters/{id}       — deregister a cluster
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.k8s import ClusterRegisterRequest
from ..services import k8s_service
from ..services.k8s_service import K8sError
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/k8s", tags=["kubernetes"])


@router.get("/__phase1__")
def phase1_status() -> dict:
    """Health check — confirms the router is mounted (k8s_management_enabled is on)."""
    return {
        "phase": 1,
        "ok": True,
        "note": (
            "Kubernetes management Phase 1 — register/list managed clusters + "
            "kubeconfig-as-reference. Phase 2 launches a management plane "
            "(Portainer-k8s first, then Rancher), Phase 3 brokers access (native "
            "PRA tunnel_type=k8s + Entitle-Rancher JIT), Phase 4 installs "
            "in-cluster Password Safe secret delivery."
        ),
    }


@router.get("/clusters")
async def list_clusters(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Every managed cluster (newest first)."""
    return {"clusters": k8s_service.list_clusters(db)}


@router.post("/clusters", status_code=201)
async def register_cluster(
    payload: ClusterRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Register an existing reachable cluster from its kubeconfig. The kubeconfig
    is stored as a secrets-backend reference, never in the row."""
    try:
        return k8s_service.register_cluster(
            db, name=payload.name, cloud=payload.cloud, kubeconfig=payload.kubeconfig,
            created_by=current_user.username, mgmt_kind=payload.mgmt_kind,
        )
    except K8sError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/clusters/{cluster_id}")
async def get_cluster(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """One cluster's record."""
    try:
        return k8s_service.get_cluster(db, cluster_id)
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/clusters/{cluster_id}")
async def delete_cluster(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Deregister a cluster + clear its stored kubeconfig (Phase 1 does not tear
    down a cloud-provisioned cluster)."""
    try:
        return k8s_service.delete_cluster(db, cluster_id)
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
