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

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.k8s import (
    BrokerAccessRequest,
    ClusterProvisionRequest,
    ClusterRegisterRequest,
    EntitleAgentRequest,
    EntitleClusterRegisterRequest,
    EntraGroupRequest,
    K8sProvisionOptions,
    ManagementRequest,
    SecretDeliveryRequest,
)
from ..services import k8s_service, job_service, cache_service, pra_api_service
from ..services.aws_service import AWSError
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
        "vpc_cidr": payload.vpc_cidr,
        "authorized_cidrs": payload.authorized_cidrs,
        "zone": payload.zone,
    }.items() if v is not None}

    # Pre-action policy gate (inert unless enabled + this action is gated).
    from ..services import admission_service
    admission_service.enforce(
        "k8s:provision",
        request={"region": payload.region, "instance_type": payload.node_instance_type,
                 "name": payload.name, "node_count": payload.node_count},
        actor=current_user, db=db,
    )
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


@router.get("/clusters/provision-options", response_model=K8sProvisionOptions)
async def provision_options(
    cloud: str = "aws",
    region: str = "",
    current_user: User = Depends(require_admin),
):
    """Served pickers for the provision modal (region-scoped). Curated per-cloud
    static lists (regions / node sizes / k8s versions, configured value always
    included) + (AWS only) live VPC subnets for the EKS subnet override and the
    configured sandbox subnet ids to pre-select. AWS subnet discovery is cached
    (10 min); static lists are assembled per request.

    Declared BEFORE GET /clusters/{cluster_id} so that path param doesn't capture
    "provision-options"."""
    cloud_l = (cloud or "aws").strip().lower()

    async def _fetch():
        return await k8s_service.provision_options(cloud_l, region)

    try:
        if cloud_l == "aws":
            key = cache_service.key_param("k8s_provision_opts", cloud=cloud_l, region=(region or "").strip())
            opts, cached_at = await cache_service.get_or_refresh(
                key, cache_service.TTL["k8s_provision_opts"], _fetch)
            return K8sProvisionOptions(**opts, cached_at=cached_at)
        opts = await _fetch()   # azure / gcp — pure static assembly, no cache
        return K8sProvisionOptions(**opts, cached_at=None)
    except K8sError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/clusters/pra-options")
async def pra_options(
    current_user: User = Depends(require_admin),
):
    """PRA pickers for the per-cluster tunnel modal — Jump Groups, Jumpoints and
    Vault account groups (best-effort, cluster-agnostic). ``configured`` is false
    when PRA OAuth isn't set, so the UI shows a note instead of empty dropdowns.
    Declared before ``/clusters/{cluster_id}`` so the literal isn't captured by the
    path param (same ordering rule as ``/clusters/provision-options``)."""
    pickers = await pra_api_service.list_pickers()
    return {"configured": pra_api_service.configured(), **pickers}


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

    for _dereg in (k8s_service.deregister_pra_tunnel, k8s_service.deregister_api_tunnel,
                   k8s_service.unbind_entra_group):
        try:
            await _dereg(db, cluster_id)
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


@router.post("/clusters/{cluster_id}/tunnel", status_code=202)
async def register_tunnel(
    cluster_id: str,
    payload: BrokerAccessRequest = BrokerAccessRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Provision the cluster's sra ``tunnel_type=k8s`` jump (Phase 3b) — enqueues a
    ``k8s_tunnel`` job the worker runs (async: the vault-inject path mints an SA token
    via the cluster runner, minutes on a Cloud Run runner — too long for the request).
    Idempotent. Optional jump-group / jumpoint-name / PRA-credential / vault overrides
    fall back to config. Open the returned job for status/logs."""
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_tunnel", created_by=current_user.username,
        metadata={
            "cluster_id": cluster_id, "action": "register",
            "jump_group": payload.jump_group, "jumpoint_name": payload.jumpoint_name,
            "pra_credential_ref": payload.pra_credential_ref,
            "vault_inject": payload.vault_inject,
            "vault_account_group_id": payload.vault_account_group_id,
        },
    )
    return {"ok": True, "status": "provisioning", "cluster_id": cluster_id,
            "action": "register", "job_id": job.id}


@router.delete("/clusters/{cluster_id}/tunnel", status_code=202)
async def remove_tunnel(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Destroy the cluster's PRA tunnel jump + clear its state (Phase 3b) — enqueues a
    ``k8s_tunnel`` (action=remove) job the worker runs (the vault path revokes the
    in-cluster SA via the runner). Open the returned job for status/logs."""
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_tunnel", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "action": "remove"},
    )
    return {"ok": True, "status": "removing", "cluster_id": cluster_id,
            "action": "remove", "job_id": job.id}


@router.post("/clusters/{cluster_id}/api-tunnel", status_code=202)
async def register_api_tunnel(
    cluster_id: str,
    payload: BrokerAccessRequest = BrokerAccessRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Provision a generic ``tunnel_type=tcp`` PRA jump straight to the cluster's
    API server (pinned local port) — enqueues a ``k8s_api_tunnel`` job. Unlike the
    ``tunnel_type=k8s`` tunnel, this forwards raw TCP, so kubectl authenticates
    end-to-end with the downloadable cloud-login kubeconfig and can ``--as``
    impersonate Entitle grants. Optional jump-group / jumpoint / PRA-credential
    overrides fall back to config (vault fields on the body are ignored — this
    tunnel injects no credential). Open the returned job for status/logs."""
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_api_tunnel", created_by=current_user.username,
        metadata={
            "cluster_id": cluster_id, "action": "register",
            "jump_group": payload.jump_group, "jumpoint_name": payload.jumpoint_name,
            "pra_credential_ref": payload.pra_credential_ref,
        },
    )
    return {"ok": True, "status": "provisioning", "cluster_id": cluster_id,
            "action": "register", "job_id": job.id}


@router.delete("/clusters/{cluster_id}/api-tunnel", status_code=202)
async def remove_api_tunnel(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Destroy the cluster's API TCP tunnel jump + clear its state — enqueues a
    ``k8s_api_tunnel`` (action=remove) job. Open the returned job for status/logs."""
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_api_tunnel", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "action": "remove"},
    )
    return {"ok": True, "status": "removing", "cluster_id": cluster_id,
            "action": "remove", "job_id": job.id}


@router.get("/clusters/{cluster_id}/api-tunnel-kubeconfig")
async def api_tunnel_kubeconfig(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Download a kubeconfig for the cluster's API TCP tunnel: the stored kubeconfig
    repointed at ``https://127.0.0.1:<local_port>`` with ``tls-server-name`` set to
    the real API host, keeping the CA and the cloud-native exec-plugin auth. Token-
    free — carries no injected credential. Connect the tunnel on that local port,
    point ``KUBECONFIG`` at this file, and kubectl authenticates as your own cloud
    identity (and can ``--as`` impersonate Entitle grants)."""
    try:
        content = k8s_service.build_api_tunnel_kubeconfig(db, cluster_id)
        info = k8s_service.get_cluster(db, cluster_id)
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    filename = f"{info.get('name') or cluster_id}-api-tunnel.kubeconfig"
    return Response(
        content=content,
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/clusters/{cluster_id}/entra-group", status_code=202)
async def bind_entra_group(
    cluster_id: str,
    payload: EntraGroupRequest = EntraGroupRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Bind an Entra (AAD) group to a ClusterRole on the cluster — enqueues a
    ``k8s_group_binding`` job. Members of the group get the role when they sign in as
    themselves (their AAD token's group OID matches a `Group` RBAC subject), so
    Entitle's Entra-ID integration can JIT-grant real-identity cluster access with no
    impersonation. ``group_id``/``role`` fall back to config (entra_rbac_group_id /
    entra_rbac_group_role, default cluster-admin). Open the returned job for status."""
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_group_binding", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "action": "bind",
                  "group_id": payload.group_id, "role": payload.role},
    )
    return {"ok": True, "status": "binding", "cluster_id": cluster_id,
            "action": "bind", "job_id": job.id}


@router.delete("/clusters/{cluster_id}/entra-group", status_code=202)
async def unbind_entra_group(
    cluster_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Remove the cluster's Entra-group ClusterRoleBinding — enqueues a
    ``k8s_group_binding`` (action=unbind) job. Open the returned job for status."""
    try:
        k8s_service.get_cluster(db, cluster_id)   # 404 if unknown
    except K8sError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = job_service.create_job(
        db, job_type="k8s_group_binding", created_by=current_user.username,
        metadata={"cluster_id": cluster_id, "action": "unbind"},
    )
    return {"ok": True, "status": "unbinding", "cluster_id": cluster_id,
            "action": "unbind", "job_id": job.id}


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
