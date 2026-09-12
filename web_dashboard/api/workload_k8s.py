"""
Workload Lab → Kubernetes API — a Password-Safe-brokered ServiceAccount token.

  GET    /api/workload-k8s              — list the onboarded workload identities
  GET    /api/workload-k8s/options      — clusters, the profiles, and what is missing
  POST   /api/workload-k8s              — onboard one (a profile against a cluster)
  GET    /api/workload-k8s/{id}         — one identity
  GET    /api/workload-k8s/{id}/consumer — what a consumer needs in order to retrieve
  POST   /api/workload-k8s/{id}/rotate  — rotate the managed account
  DELETE /api/workload-k8s/{id}         — delete the ServiceAccount and the PS objects

**No new preview flag.** Settings owns two toggles for the Workload Lab, one per lab, and
this is a capability of the page rather than a third one — so the router is registered
behind ``k8s_management_enabled`` and every endpoint additionally calls
``_require_enabled``, which also requires ``password_safe_enabled``. Two flags, one of
which ``_feature_gate`` cannot express on its own.

``/consumer`` is the counterpart of the SPIRE router's ``/onboarding``, and it exists for
the same reason: what a consumer needs is a handful of exactly-spelled strings — the safe,
the managed system, the ``<namespace>/<serviceaccount>`` account name — and an operator
assembling them by reading a page is how a wrong one ends up in a pipeline. **It returns
no credential and never will.** Retrieval is the consumer's own Password Safe API call
with its own client id, which is the whole point: the audit trail has to record *which
build* retrieved, and a value proxied through this dashboard would record only that the
dashboard did.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import config_service, workload_k8s_service
from ..services.workload_k8s_service import WorkloadK8sError
from .auth import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workload-k8s", tags=["workload-k8s"])


def _require_enabled() -> None:
    """Both flags, because the tab needs a cluster to act on AND a vault to broker with.

    Spelled out here rather than folded into one derived flag: ``_DERIVED`` in
    feature_flags is all-preview, and adding a non-preview flag to it would stop
    ``workload_lab_enabled`` resolving as preview-only — which is what
    ``tests/test_permission_catalog.py`` keys off when deciding whether a page needs an
    RBAC scope of its own."""
    if not config_service.get_bool("k8s_management_enabled",
                                   settings.k8s_management_enabled):
        raise HTTPException(status_code=403,
                            detail="Kubernetes management is disabled")
    if not config_service.get_bool("password_safe_enabled",
                                   settings.password_safe_enabled):
        raise HTTPException(
            status_code=403,
            detail=("Password Safe is disabled — it is what brokers the token, so there "
                    "is nothing this tab can do without it"))


def _visible(row, user: User) -> bool:
    """Creator-scoped for non-admins, exactly like both sibling labs."""
    return bool(getattr(user, "is_admin", False)) or row.created_by == user.username


def _visible_or_404(db: Session, row_id: str, user: User):
    row = workload_k8s_service.get_row(db, row_id)
    if not row or row.status == "deleted" or not _visible(row, user):
        raise HTTPException(status_code=404, detail="workload identity not found")
    return row


def _shape(row) -> dict:
    """The row on the wire. **Ids and names only — never a token.**

    There is no credential here to omit, which is the design rather than an oversight:
    the bearer token lives in Password Safe and this dashboard never reads it. What the
    page shows is what the identity IS and what it may do.
    """
    return {
        "id": row.id, "name": row.name,
        "cluster_id": row.cluster_id, "cluster_name": row.cluster_name or "",
        "cloud": row.cloud or "",
        "profile": row.profile,
        # What the profile actually binds, resolved server-side so the page cannot
        # describe a binding the service does not create.
        "binds": workload_k8s_service.profile_summary(row.profile),
        "namespace": row.namespace, "service_account": row.service_account,
        "mode": row.mode, "ttl_seconds": row.ttl_seconds or 0,
        "status": row.status, "error_message": row.error_message,
        "stages": list(workload_k8s_service.STAGES),
        "stages_done": [s for s in (row.stages_done or "").split(",") if s],
        "stage_job_ids": workload_k8s_service.stage_jobs(row),
        # Password Safe coordinates. `ps_account_name` is `<ns>/<sa>` — a name, and the
        # one string a consumer has to get exactly right.
        "ps_system_id": row.ps_system_id or "",
        "ps_account_id": row.ps_account_id or "",
        "ps_account_name": row.ps_account_name or "",
        "ps_address": row.ps_address or "",
        # Whether a rotation has ever completed. Until it has, the managed account holds
        # the placeholder it was created with (a bearer token cannot be seeded — it is
        # far longer than the create API's 128-character cap), so the row looks onboarded
        # and would serve a credential that authenticates to nothing.
        "rotated": bool(row.rotated),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


class OnboardRequest(BaseModel):
    name: str
    cluster_id: str
    # deployer | reader. Validated in the service, which is also what renders the RBAC —
    # one place decides what a profile means.
    profile: str = "deployer"
    namespace: str = "default"
    service_account: str
    mode: str = "bound"
    # Bound mode only. 0 takes the configured default; the service clamps to the
    # TokenRequest API's own 600-second floor either way.
    ttl_seconds: int = 0
    # Auto-delete timer, in hours. A forgotten workload token keeps authenticating, so
    # the tab offers this at onboard time rather than only afterwards on Inventory.
    expires_in_hours: Optional[int] = None
    # The cloud's OWN name for the cluster, and the two per-cloud parts that go with it.
    # Needed for a REGISTERED cluster and only then: one the dashboard provisioned has a
    # deploy job whose Terraform variables carry all three, and blank means "derive them".
    # A registered cluster has no such job — and `K8sCluster` stores neither a resource
    # group nor a GKE location — so without these an EKS/AKS/GKE address cannot be built
    # at all and the onboard is refused with a message naming what is missing.
    cluster_name: str = ""
    resource_group: str = ""   # AKS only
    location: str = ""         # GKE only, and its ZONE for a zonal cluster


# ── read ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_identities(db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    rows = workload_k8s_service.list_rows(db)
    return {"identities": [_shape(r) for r in rows if _visible(r, user)]}


@router.get("/options")
def onboard_options(db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "read"))):
    """What the onboard form needs, plus an honest list of what is not configured.

    ``missing`` is the point of this route, as it is on both sibling labs: every item on
    it produces a failure that surfaces inside Password Safe hours later — at the first
    rotation, where this dashboard sees only a generic failure — rather than at the click.

    A cluster with no stored kubeconfig is listed with ``usable: false`` rather than
    hidden: an operator who registered it is entitled to see why it cannot be chosen,
    and "it is not in the list" is the least useful way to say so.

    Read off ``K8sCluster`` rather than through ``k8s_service.list_clusters``, which is
    the seam this would otherwise reuse. That serializer deliberately omits the
    kubeconfig and exposes no flag about it, so it cannot answer the one question this
    form has to ask — whether anything here can reach the API server at all. Deriving
    ``usable`` from it would have meant a field that is always true.
    """
    _require_enabled()
    from ..database import K8sCluster
    from ..services import ps_api_service

    clusters = []
    for c in (db.query(K8sCluster)
              .order_by(K8sCluster.created_at.desc()).all()):
        clusters.append({
            "id": c.id, "name": c.name, "cloud": c.cloud,
            "region": c.region or "", "status": c.status,
            "usable": bool(c.kubeconfig_ref),
            # Whether the dashboard provisioned it. The form uses this to decide whether to
            # ASK for the cloud's own cluster name: a provisioned cluster's deploy job
            # carries it, so asking would invite a wrong answer over a correct derived one.
            "source": c.source or "",
        })

    missing = []
    if not clusters:
        missing.append("no Kubernetes cluster is registered — register or provision one "
                       "on the Kubernetes page first")
    if not ps_api_service.configured():
        missing.append("Password Safe is not configured (pscli_api_url, pscli_client_id, "
                       "pscli_client_secret, pscli_api_account_name)")
    if not config_service.get("k8s_ps_token_platform",
                              "Kubernetes Service Account Token"):
        missing.append("k8s_ps_token_platform is blank — it names the imported "
                       ".psplugin platform the managed system is created on")
    # The functional account is per-cloud and there is no single key to check, so this
    # reports the one fact that is checkable without knowing which cluster is chosen.
    if not any(config_service.get(k) for k in
               ("k8s_ps_functional_account_aws", "k8s_ps_functional_account_azure",
                "k8s_ps_functional_account_gcp", "k8s_ps_functional_account_local")):
        missing.append("no Password Safe functional account is configured for any cloud "
                       "(k8s_ps_functional_account_{aws,azure,gcp,local}) — the managed "
                       "system inherits its platform, and without one nothing can rotate")

    return {
        "clusters": clusters,
        "profiles": [{"key": p, "binds": workload_k8s_service.profile_summary(p)}
                     for p in workload_k8s_service.VALID_PROFILES],
        "modes": list(workload_k8s_service.VALID_MODES),
        "defaults": {"profile": "deployer", "namespace": "default", "mode": "bound",
                     "ttl_seconds": 600},
        "missing": missing,
    }


@router.get("/{row_id}")
def get_identity(row_id: str, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    return _shape(_visible_or_404(db, row_id, user))


@router.get("/{row_id}/consumer")
def consumer_details(row_id: str, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("cloud_function", "read"))):
    """The exact strings a consumer play needs. **No credential, by design.**

    The consumer retrieves with its OWN Password Safe client id, which is what puts
    *which build* in the audit trail. Proxying the value through here would put only
    "the dashboard" there and would turn this endpoint into a second, unaudited way to
    read the token — the precise property this whole mechanism exists to remove.
    """
    _require_enabled()
    row = _visible_or_404(db, row_id, user)
    safe = config_service.get("k8s_ps_workgroup") or config_service.get(
        "passwordsafe_workgroup") or ""
    return {
        "id": row.id,
        "managed_system": f"workload-{row.profile}-{row.cluster_name}-"
                          f"{row.service_account}",
        # `<namespace>/<serviceaccount>`, and the one string that has to match exactly:
        # `secret_list` is `<system>/<account>`, so a wrong half is a retrieval that
        # fails with "not found" and reads like a permissions problem.
        "account_name": row.ps_account_name or "",
        "workgroup": safe,
        "namespace": row.namespace,
        "profile": row.profile,
        "binds": workload_k8s_service.profile_summary(row.profile),
        "playbook": ("examples/playbooks/k8s/ci-deploy-with-ps-token.yml"
                     if row.profile == "deployer"
                     else "examples/playbooks/k8s/ci-read-with-ps-token.yml"),
        "ready": bool(row.ps_account_id and row.rotated),
        "note": ("retrieval is the consumer's own Password Safe call with its own client "
                 "id — that is what records which build read the token. Nothing here "
                 "returns the token itself."),
    }


# ── write ─────────────────────────────────────────────────────────────────────

@router.post("")
def onboard_identity(req: OnboardRequest, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("cloud_function", "write"))):
    """Onboard one workload identity against a registered cluster.

    The profile is the decision being made here, not the token: it selects the RBAC, and
    the RBAC is what makes a vaulted token worth demonstrating. See the service module's
    docstring for why neither profile is ``cluster-admin``.
    """
    _require_enabled()
    expires_at = None
    if req.expires_in_hours and req.expires_in_hours > 0:
        expires_at = datetime.utcnow() + timedelta(hours=int(req.expires_in_hours))
    try:
        return workload_k8s_service.onboard(
            db, name=req.name, cluster_id=req.cluster_id, profile=req.profile,
            namespace=req.namespace, service_account=req.service_account,
            mode=req.mode, ttl_seconds=req.ttl_seconds, created_by=user.username,
            expires_at=expires_at, cluster_name=req.cluster_name,
            resource_group=req.resource_group, location=req.location)
    except WorkloadK8sError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{row_id}/rotate")
def rotate_identity(row_id: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    """Rotate the managed account.

    **This does not revoke.** A token already retrieved keeps working until its TTL
    expires, so this is hygiene; deleting the ServiceAccount is containment. The response
    says so rather than leaving an operator to infer that rotating locked somebody out.
    """
    _require_enabled()
    _visible_or_404(db, row_id, user)
    try:
        return workload_k8s_service.start_rotate(
            db, row_id=row_id, created_by=user.username)
    except WorkloadK8sError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{row_id}")
def delete_identity(row_id: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    """Delete the ServiceAccount, its binding, and the Password Safe objects.

    Deleting the ServiceAccount is the one hard kill switch this mechanism has: every
    token Password Safe ever issued is bound to that account's uid and dies with it. The
    namespace is deliberately left behind — onboarding created it only if it was missing,
    and removing it would take every unrelated workload in it too.
    """
    _require_enabled()
    _visible_or_404(db, row_id, user)
    try:
        return workload_k8s_service.start_decommission(
            db, row_id=row_id, created_by=user.username)
    except WorkloadK8sError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
