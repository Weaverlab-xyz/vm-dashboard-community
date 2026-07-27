"""
Oracle Cloud Infrastructure (OCI) API endpoints — the fourth cloud provider.

Mirrors the AWS / Azure / GCP router patterns:
  - config helpers read from config_service (DB) first, fall back to settings
  - background tasks create a Job record and update progress
  - cache_service used for expensive OCI API calls
  - deploy orchestration order matches the other clouds: VM → PRA Shell Jump →
    Entitle registration → Password Safe onboarding (each non-fatal)

Free-tier guardrail: the deploy form defaults to Always-Free compute; a selection
outside the envelope (services/oci_freetier.py) is rejected with HTTP 400 unless
the request carries acknowledge_charges=true (warn-and-confirm).
"""
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import Job, User, get_db
from ..models.oci import (
    OCIDeployRequest,
    OCIDeployResponse,
    OCIImageListResponse,
    OCIInstanceListResponse,
    OCINetworkOptions,
    OCISSHKeyDetail,
)
from ..services import (
    cache_service, cloud_stats, deploy_batch, job_service, oci_freetier, oci_service,
    workgroup_service,
)
from .auth import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oci", tags=["oci"])


# ── Config helpers ────────────────────────────────────────────────────────────

def _oci_cfg(key: str, fallback: str = "") -> str:
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _compartment() -> str:
    return _oci_cfg("oci_compartment_ocid") or _oci_cfg("oci_tenancy_ocid")


def _region() -> str:
    return _oci_cfg("oci_region") or "us-ashburn-1"


def _configured() -> bool:
    return bool(_oci_cfg("oci_tenancy_ocid") and _oci_cfg("oci_user_ocid")
                and _oci_cfg("oci_private_key"))




def _validate_workgroup(db: Session, user: User, workgroup: str) -> str:
    wg = workgroup_service.get(db, workgroup)
    if not wg:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup '{workgroup}'")
    canonical = wg.name
    if not user.is_admin and canonical not in [w.lower() for w in user.workgroups_list]:
        raise HTTPException(status_code=403, detail=f"You do not have access to workgroup '{canonical}'")
    return canonical


def _accessible_workgroups(user: User) -> Optional[List[str]]:
    if user.is_admin:
        return None
    return [w.lower() for w in user.workgroups_list]


# ── Free-tier usage from this dashboard's own deploys ─────────────────────────

def _existing_freetier_usage(db: Session, exclude_job_id: str = "") -> dict:
    """Sum the free-tier footprint of OCI VMs this dashboard has committed to, so the
    guardrail can flag 'this would exceed the free count/budget'. Best-effort (reads
    job metadata; not account-wide usage).

    Counts in-flight rows as well as completed ones, using the same status set as the
    name-collision pre-flight. Scoped to `completed` alone, a second batch submitted
    while the first was still running would see none of the first batch's VMs and wave
    a double overage straight through."""
    from ..services.inventory_service import _NAME_HOLDING_STATUSES
    amd = 0
    a1_ocpus = 0.0
    a1_mem = 0.0
    jobs = (db.query(Job)
            .filter(Job.job_type == "oci_deploy",
                    Job.status.in_(_NAME_HOLDING_STATUSES)).all())
    for j in jobs:
        if j.id == exclude_job_id:
            continue
        meta = j.metadata_dict
        if meta.get("destroyed"):
            continue
        shape = meta.get("shape") or ""
        if shape == oci_freetier.FREE_AMD_SHAPE:
            amd += 1
        elif shape == oci_freetier.FREE_A1_SHAPE:
            a1_ocpus += float(meta.get("ocpus") or 0)
            a1_mem += float(meta.get("memory_gb") or 0)
    return {"existing_amd_count": amd, "existing_a1_ocpus": a1_ocpus, "existing_a1_memory_gb": a1_mem}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/images", response_model=OCIImageListResponse)
async def list_images(
    current_user: User = Depends(require_permission("oci", "read")),
):
    """List platform (Oracle-provided) + custom images in the configured compartment."""
    if not _configured():
        raise HTTPException(status_code=400, detail="OCI not configured — run the setup wizard.")
    compartment = _compartment()
    cache_key = cache_service.key_global("oci_images")
    cached = await cache_service.get(cache_key)
    if cached:
        return cached["data"]
    try:
        images = await oci_service.list_images(compartment)
    except oci_service.OCIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result = OCIImageListResponse(images=images, compartment_ocid=compartment)
    await cache_service.set(cache_key, result.model_dump(), ttl=300)
    return result


@router.get("/network-options", response_model=OCINetworkOptions)
async def network_options(
    bust: bool = Query(False),
    current_user: User = Depends(require_permission("oci", "read")),
):
    """Availability domains, shapes (free-tier flagged), subnets, and the free-tier
    catalog for the configured compartment/VCN."""
    if not _configured():
        raise HTTPException(status_code=400, detail="OCI not configured — run the setup wizard.")
    cache_key = cache_service.key_global("oci_network_opts")
    if not bust:
        cached = await cache_service.get(cache_key)
        if cached:
            return cached["data"]
    try:
        opts = await oci_service.get_network_options(_compartment(), _oci_cfg("oci_vcn_ocid"))
    except oci_service.OCIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    from datetime import datetime, timezone
    opts["cached_at"] = datetime.now(timezone.utc).isoformat()
    result = OCINetworkOptions(**opts)
    await cache_service.set(cache_key, result.model_dump(), ttl=300)
    return result


async def _build_oci_instances(db, compartment: str) -> list:
    """Collect live OCIDs from completed, non-destroyed oci_deploy jobs, describe
    them, and cache the full (unfiltered) list. Shared by /instances + /dashboard-stats."""
    deploy_jobs = (db.query(Job)
                   .filter(Job.job_type == "oci_deploy", Job.status == "completed")
                   .order_by(Job.created_at.desc()).all())
    ocids: list[str] = []
    job_meta: dict = {}
    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("destroyed"):
            continue
        ocid = meta.get("instance_ocid")
        if not ocid:
            continue
        ocids.append(ocid)
        job_meta[ocid] = {
            "job_id": job.id,
            "deployed_by": job.created_by,
            "workgroup": (job.workgroup or meta.get("workgroup") or "").lower() or None,
        }
    instances = await oci_service.describe_instances(compartment, ocids)
    for inst in instances:
        meta = job_meta.get(inst["ocid"], {})
        inst["job_id"] = meta.get("job_id")
        inst["deployed_by"] = meta.get("deployed_by")
        inst["workgroup"] = meta.get("workgroup") or inst.get("workgroup")
    full = OCIInstanceListResponse(instances=instances, compartment_ocid=compartment, region=_region())
    await cache_service.set(cache_service.key_global("oci_instances"), full.model_dump(), ttl=60)
    return instances


@router.get("/instances", response_model=OCIInstanceListResponse)
async def list_instances(
    bust: bool = Query(False),
    workgroup: Optional[str] = None,
    current_user: User = Depends(require_permission("oci", "read")),
    db: Session = Depends(get_db),
):
    """List OCI compute instances deployed via this dashboard (job records + live state).
    Non-admins see only instances in their workgroups."""
    if not _configured():
        raise HTTPException(status_code=400, detail="OCI not configured — run the setup wizard.")
    accessible = _accessible_workgroups(current_user)
    if workgroup is not None and accessible is not None and workgroup.lower() not in accessible:
        raise HTTPException(status_code=403, detail=f"No access to workgroup '{workgroup.lower()}'")

    cache_key = cache_service.key_global("oci_instances")
    if not bust:
        cached = await cache_service.get(cache_key)
        if cached:
            payload = cached.get("data") or {}
            inst_list = payload.get("instances")
            if inst_list is not None:
                payload = {**payload, "instances": _filter_instances(inst_list, workgroup, accessible)}
            return payload
    try:
        instances = await _build_oci_instances(db, _compartment())
    except oci_service.OCIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return OCIInstanceListResponse(
        instances=_filter_instances(instances, workgroup, accessible),
        compartment_ocid=_compartment(), region=_region())


def _filter_instances(inst_list, workgroup, accessible):
    out = []
    for inst in inst_list:
        inst_wg = (inst.get("workgroup") or "").lower() or None
        if workgroup is not None and inst_wg != workgroup.lower():
            continue
        if accessible is not None and (inst_wg is None or inst_wg not in accessible):
            continue
        out.append(inst)
    return out


@router.get("/dashboard-stats")
async def oci_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("oci", "read")),
):
    """One-call counts for the OCI dashboard tiles (instances total+running,
    custom images total). A null section → the tile shows unavailable."""
    out = {"instances": None, "images": None}
    if not _configured():
        return out
    try:
        instances = await _build_oci_instances(db, _compartment())
        out["instances"] = cloud_stats.summarize_instances(
            instances, _accessible_workgroups(current_user), "lifecycle_state")
    except oci_service.OCIError:
        pass
    try:
        cached = await cache_service.get(cache_service.key_global("oci_images"))
        imgs = (cached.get("data") or {}).get("images") if cached else await oci_service.list_images(_compartment())
        custom = [i for i in (imgs or []) if i.get("source") == "custom"]
        out["images"] = {"total": len(custom)}
    except oci_service.OCIError:
        pass
    return out


@router.get("/secrets/ssh-key", response_model=OCISSHKeyDetail)
async def get_configured_ssh_key(
    current_user: User = Depends(require_permission("oci", "read")),
):
    """Preview of the SSH public key from the configured OCI Vault secret."""
    secret = _oci_cfg("oci_ssh_key_secret")
    if not secret:
        raise HTTPException(status_code=404, detail="SSH key secret not configured — add oci_ssh_key_secret.")
    try:
        pub = await oci_service.get_ssh_public_key(secret)
    except oci_service.OCIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return OCISSHKeyDetail(secret_name=secret, public_key_preview=pub[:80])


async def _fan_out_batch(
    payload: OCIDeployRequest, db: Session, current_user: User, *, compartment: str,
) -> OCIDeployResponse:
    """Fan a ``count > 1`` deploy out into one ``oci_bulk_deploy`` parent plus N
    ``queued`` ``oci_deploy`` children sharing a batch_id.

    A separate module-level function, NOT an ``if`` inside ``deploy_instance``:
    ``test_worker_dispatch``'s children-are-unclaimable rule walks the AST per
    function, so a ``children``-carrying create_job alongside the single deploy's
    ``pending`` one reads as a violation regardless of the runtime branch.

    The free-tier gate is *not* repeated here — ``deploy_instance`` runs it once for
    the whole batch before dispatching, because "would these N together exceed the
    envelope" is inherently a batch question.
    """
    names = deploy_batch.expand_names(payload.instance_name, payload.count, "oci")
    deploy_batch.reject_name_collisions(db, "oci_deploy", names)
    await deploy_batch.enforce_admission(
        "oci:compute:deploy",
        requests=deploy_batch.batch_request_docs(
            {"region": _region(), "instance_type": payload.shape,
             "image": payload.image_ocid},
            names),
        actor=current_user, db=db,
    )

    batch_id = uuid.uuid4().hex[:12]
    children = []
    for name in names:
        child_req = payload.model_copy(update={"instance_name": name, "count": 1})
        job = job_service.create_job(
            db,
            job_type="oci_deploy",
            created_by=current_user.username,
            workgroup=payload.workgroup,
            # `queued`, not `pending`: the runner claims on status='pending', so a
            # pending child would be deployed a second time alongside its parent.
            status="queued",
            batch_id=batch_id,
            metadata={
                "compartment_ocid": compartment,
                "instance_name":    name,
                # shape/ocpus/memory_gb are load-bearing on the child, not decoration:
                # _existing_freetier_usage reads exactly these three off deploy rows.
                "shape":            payload.shape,
                "ocpus":            payload.ocpus,
                "memory_gb":        payload.memory_gb,
                "image_ocid":       payload.image_ocid,
                "image_name":       payload.image_name,
                "region":           _region(),
                "workgroup":        payload.workgroup,
                "bulk":             True,
                "req":              child_req.model_dump(),
            },
        )
        job_service.set_cloud_resource_id(db, job.id, name)
        job_service.log_audit(
            db, current_user.username, "oci_deploy",
            details={"instance_name": name, "shape": payload.shape,
                     "workgroup": payload.workgroup, "bulk": True},
        )
        children.append({"job_id": job.id, "instance_name": name,
                         "req": child_req.model_dump()})

    parent = job_service.create_job(
        db,
        job_type="oci_bulk_deploy",
        created_by=current_user.username,
        workgroup=payload.workgroup,
        batch_id=batch_id,
        metadata={
            "compartment_ocid": compartment,
            "region":           _region(),
            "workgroup":        payload.workgroup,
            "children":         children,
        },
    )
    return OCIDeployResponse(
        job_id=parent.id,
        status="pending",
        message=f"Deploying {len(names)} instances ({names[0]} … {names[-1]})…",
        count=len(names),
        batch_id=batch_id,
        job_ids=[c["job_id"] for c in children],
        names=names,
    )


@router.post("/deploy", response_model=OCIDeployResponse)
async def deploy_instance(
    payload: OCIDeployRequest,
    current_user: User = Depends(require_permission("oci", "write")),
    db: Session = Depends(get_db),
):
    """Deploy one or more OCI compute instances from an image. Runs in background;
    returns a job id immediately. Enforces the free-tier warn-and-confirm gate over
    the whole request, so ``count`` is factored into the envelope."""
    if not _configured():
        raise HTTPException(status_code=400, detail="OCI not configured — run the setup wizard.")

    compartment = _compartment()
    workgroup = _validate_workgroup(db, current_user, payload.workgroup)
    payload.workgroup = workgroup

    # ── Free-tier guardrail (warn + confirm) ──────────────────────────────────
    # Evaluated once for the whole request, count included: three Always-Free micros
    # is two more than the envelope allows even though each one on its own is free.
    # oci_freetier.evaluate has always taken instance_count; nothing passed it until
    # counts existed.
    if _oci_cfg("oci_freetier_enforce", "1") not in ("0", "false", "False", ""):
        usage = _existing_freetier_usage(db)
        within, warnings = oci_freetier.evaluate(
            shape=payload.shape, ocpus=payload.ocpus, memory_gb=payload.memory_gb,
            boot_volume_gb=payload.boot_volume_gb, instance_count=payload.count, **usage,
        )
        if not within and not payload.acknowledge_charges:
            # `code` is preserved onto the Error by the frontend API helper
            # (app.js); the deploy modal keys off it to reveal the acknowledgment.
            raise HTTPException(status_code=400, detail={
                "code": "free_tier_exceeded",
                "message": "This selection is outside the OCI Always-Free tier and may incur charges: "
                           + " ".join(warnings),
                "warnings": warnings,
            })

    if payload.count > 1:
        return await _fan_out_batch(payload, db, current_user, compartment=compartment)

    # Pre-action policy gate (inert unless enabled + this action is gated).
    from ..services import admission_service
    admission_service.enforce(
        "oci:compute:deploy",
        request={"region": _region(), "instance_type": payload.shape,
                 "image": payload.image_ocid, "name": payload.instance_name,
                 "count": 1, "batch": False},
        actor=current_user, db=db,
    )

    job = job_service.create_job(
        db,
        job_type="oci_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "compartment_ocid": compartment,
            "instance_name":    payload.instance_name,
            "shape":            payload.shape,
            "ocpus":            payload.ocpus,
            "memory_gb":        payload.memory_gb,
            "image_ocid":       payload.image_ocid,
            "image_name":       payload.image_name,
            "region":           _region(),
            "workgroup":        workgroup,
            # Full request so the runner can rebuild the deploy call. Only secret
            # *references* live on it, resolved at deploy time — same shape as the
            # Packer build jobs (services/packer_build_service.run_build).
            "req":              payload.model_dump(),
        },
    )
    job_service.set_cloud_resource_id(db, job.id, payload.instance_name)
    job_service.log_audit(
        db, current_user.username, "oci_deploy",
        details={"instance_name": payload.instance_name, "shape": payload.shape, "workgroup": workgroup},
    )
    return OCIDeployResponse(job_id=job.id, status="pending", message=f"Deploying {payload.instance_name}…")




@router.delete("/instances/{instance_ocid:path}")
async def destroy_instance(
    instance_ocid: str,
    current_user: User = Depends(require_permission("oci", "delete")),
    db: Session = Depends(get_db),
):
    """Terminate an OCI compute instance. Runs in background."""
    if not _configured():
        raise HTTPException(status_code=400, detail="OCI not configured — run the setup wizard.")

    deploy_job = None
    for j in (db.query(Job)
              .filter(Job.job_type == "oci_deploy", Job.status == "completed").all()):
        meta = j.metadata_dict
        if meta.get("instance_ocid") == instance_ocid and not meta.get("destroyed"):
            deploy_job = j
            break

    job = job_service.create_job(
        db, job_type="oci_destroy", created_by=current_user.username,
        metadata={"instance_ocid": instance_ocid, "deploy_job_id": deploy_job.id if deploy_job else None},
    )
    job_service.log_audit(db, current_user.username, "oci_destroy", details={"instance_ocid": instance_ocid})
    return {"job_id": job.id, "status": "pending", "message": "Terminating instance…"}


