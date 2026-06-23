"""Kubernetes management API — Phases 1–2.

Gated on ``k8s_management_enabled`` (feature-gate dependency). Phase 1
registers/lists managed clusters via ``k8s_service`` and stores the kubeconfig
as a backend reference; Phase 2 launches a management plane (Portainer-k8s) into
a registered cluster. See docs/saas-kubernetes-management-plan.md.

  GET    /api/k8s/__phase1__               — health check (router-mounted probe)
  GET    /api/k8s/clusters                 — list managed clusters
  POST   /api/k8s/clusters                 — register an existing cluster (kubeconfig)
  POST   /api/k8s/clusters/provision       — provision a new cluster with Terraform (§1.1a)
  GET    /api/k8s/clusters/{id}            — one cluster
  DELETE /api/k8s/clusters/{id}            — deregister (registered) / decommission+destroy (provisioned)
  POST   /api/k8s/clusters/{id}/management — launch a management plane (Phase 2)
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.k8s import (
    BrokerAccessRequest,
    ClusterProvisionRequest,
    ClusterRegisterRequest,
    EntitleAgentRequest,
    EntitleClusterRegisterRequest,
    ManagementRequest,
    SecretDeliveryRequest,
)
from ..services import k8s_service, job_service
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


@router.post("/clusters/provision", status_code=202)
async def provision_cluster(
    payload: ClusterProvisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Provision a new cluster with Terraform (§1.1a). Async — records a
    ``provisioning`` row + schedules the apply, which stores the generated
    kubeconfig and flips the cluster to ``registered``. Returns 202; poll the
    cluster status (provisioning → registered / failed). Implemented for aws (EKS),
    azure (AKS), and gcp (GKE); an unwired cloud returns 501."""
    opts = {k: v for k, v in {
        "k8s_version": payload.k8s_version,
        "node_instance_type": payload.node_instance_type,
        "node_count": payload.node_count,
        "subnet_ids": payload.subnet_ids,
        "authorized_cidrs": payload.authorized_cidrs,
        "zone": payload.zone,
    }.items() if v is not None}
    try:
        result = k8s_service.create_cluster(
            db, cloud=payload.cloud, name=payload.name, region=payload.region,
            created_by=current_user.username, **opts,
        )
    except K8sError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    # Persist the Terraform vars on the job; the dedicated job runner (a separate
    # process, immune to gunicorn worker recycling) claims the pending job and runs
    # the apply. cloud + cluster_id are already in the job metadata (create_cluster).
    job_service.update_metadata(db, result["job_id"], {"tf_variables": result["tf_variables"]})
    return {"ok": True, "cluster_id": result["cluster_id"], "job_id": result["job_id"]}


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
    """Delete a cluster. A **provisioned** cluster (``source=provisioned``) is torn
    down asynchronously — PRA tunnel, then ``terraform destroy``, then the record
    (poll status decommissioning → gone / failed). A **registered** cluster is
    deregistered synchronously (best-effort PRA tunnel cleanup first so a deregister
    doesn't orphan a Jump Item, then drop the record + kubeconfig); it does not tear
    down the underlying cluster."""
    try:
        info = k8s_service.get_cluster(db, cluster_id)
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if info.get("source") == "provisioned":
        # start_decommission flips the row to decommissioning + creates the pending
        # k8s_decommission job; the job runner claims it and drives the teardown.
        result = k8s_service.start_decommission(db, cluster_id, created_by=current_user.username)
        return {"status": "decommissioning", **result}

    try:
        await k8s_service.deregister_pra_tunnel(db, cluster_id)
    except Exception as e:
        logger.warning("tunnel cleanup during delete of %s failed: %s", cluster_id, e)
    try:
        return k8s_service.delete_cluster(db, cluster_id)
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/clusters/{cluster_id}/console")
async def cluster_console(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """A link to the cluster's management console (Phase 3a). For Portainer-k8s,
    the brokered Portainer endpoint view; for Rancher/Argo, the management URL."""
    try:
        return k8s_service.console_url(db, cluster_id)
    except K8sError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/clusters/{cluster_id}/access")
async def broker_access(
    cluster_id: str,
    payload: BrokerAccessRequest = BrokerAccessRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Broker access (Phase 3b). When PRA is configured, ensures the sra
    ``tunnel_type=k8s`` jump exists and returns a tunnel descriptor (connect via
    the PRA representative console — no public ingress); otherwise returns the
    Phase-3a ingress link. For a Rancher plane with Entitle enabled it also opens
    a time-boxed RBAC grant. Optional per-cluster overrides (jump group, jumpoint
    name, PRA credential) fall back to config."""
    try:
        return await k8s_service.open_console(
            db, cluster_id, current_user.username,
            jump_group=payload.jump_group,
            jumpoint_name=payload.jumpoint_name,
            pra_credential_ref=payload.pra_credential_ref,
            vault_inject=payload.vault_inject,
            vault_account_group_id=payload.vault_account_group_id,
        )
    except K8sError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/clusters/{cluster_id}/tunnel", status_code=201)
async def register_tunnel(
    cluster_id: str,
    payload: BrokerAccessRequest = BrokerAccessRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Provision the cluster's sra ``tunnel_type=k8s`` jump (Phase 3b). Idempotent.
    Optional jump-group / jumpoint-name / PRA-credential overrides fall back to config."""
    try:
        return await k8s_service.register_pra_tunnel(
            db, cluster_id,
            jump_group=payload.jump_group,
            jumpoint_name=payload.jumpoint_name,
            pra_credential_ref=payload.pra_credential_ref,
            vault_inject=payload.vault_inject,
            vault_account_group_id=payload.vault_account_group_id,
        )
    except K8sError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/clusters/{cluster_id}/tunnel")
async def remove_tunnel(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Destroy the cluster's PRA tunnel jump and clear its state (Phase 3b)."""
    try:
        return await k8s_service.deregister_pra_tunnel(db, cluster_id)
    except K8sError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/clusters/{cluster_id}/management", status_code=202)
async def launch_management(
    cluster_id: str,
    payload: ManagementRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Launch a management plane into the cluster (Phase 2). Async — enqueues a
    ``k8s_management`` job the dedicated worker runs (applies the Portainer Agent via
    a transient kubectl container, then registers it in the brokered Portainer
    server). Returns 202 + job_id; poll the cluster status (deploying → managed /
    failed), or open the job to see the error if it fails."""
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_management", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "mgmt_kind": payload.mgmt_kind},
    )
    return {"ok": True, "status": "deploying", "cluster_id": cluster_id,
            "mgmt_kind": payload.mgmt_kind, "job_id": job.id}


@router.post("/clusters/{cluster_id}/secret-delivery", status_code=202)
async def setup_secret_delivery(
    cluster_id: str,
    payload: SecretDeliveryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Install (or remove) in-cluster Password Safe secret delivery (Phase 4 /
    Feature D). ``kind=eso`` Helm-installs the External Secrets Operator + a
    BeyondTrust ClusterSecretStore (Password Safe → K8s Secrets); ``kind=none``
    removes it. Async — enqueues a ``k8s_secret_delivery`` job the worker runs; poll
    the cluster's ``secrets_delivery_kind`` (installing → eso / failed), or open the
    job for the error."""
    if payload.kind not in k8s_service.VALID_DELIVERY_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind {payload.kind!r} (expected one of {', '.join(k8s_service.VALID_DELIVERY_KINDS)})",
        )
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_secret_delivery", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "kind": payload.kind},
    )
    return {"ok": True, "status": "installing", "cluster_id": cluster_id,
            "kind": payload.kind, "job_id": job.id}


@router.post("/clusters/{cluster_id}/entitle-agent", status_code=202)
async def setup_entitle_agent(
    cluster_id: str,
    payload: EntitleAgentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Install (or remove) the **Entitle agent** in a managed cluster — the
    agent-cluster bootstrap. ``action=install`` resolves the agent token server-side
    from ``entitle_agent_token_ref``, applies the ``ENTITLE_TOKEN`` Secret, and
    Helm-installs the chart referencing it; ``action=remove`` uninstalls it. Async —
    enqueues a ``k8s_entitle_agent`` job the worker runs; open the job for status/error."""
    if payload.action not in k8s_service.VALID_ENTITLE_AGENT_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action {payload.action!r} (expected one of {', '.join(k8s_service.VALID_ENTITLE_AGENT_ACTIONS)})",
        )
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_entitle_agent", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "action": payload.action},
    )
    return {"ok": True, "status": "installing" if payload.action == "install" else "removing",
            "cluster_id": cluster_id, "action": payload.action, "job_id": job.id}


@router.post("/clusters/{cluster_id}/entitle-register", status_code=202)
async def register_cluster_in_entitle(
    cluster_id: str,
    payload: EntitleClusterRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Register (or deregister) the cluster as a generic Entitle **Kubernetes**
    integration (EKS/AKS/GKE) so users request JIT cluster RBAC in Entitle. Uses the
    agent's In-Cluster access when the agent is installed here, else mints a
    least-privilege ServiceAccount for External Access. Async — enqueues a
    ``k8s_entitle_register`` job; open the job for status/error."""
    if payload.action not in k8s_service.VALID_ENTITLE_CLUSTER_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action {payload.action!r} (expected one of {', '.join(k8s_service.VALID_ENTITLE_CLUSTER_ACTIONS)})",
        )
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_entitle_register", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "action": payload.action},
    )
    return {"ok": True, "status": "registering" if payload.action == "register" else "deregistering",
            "cluster_id": cluster_id, "action": payload.action, "job_id": job.id}
