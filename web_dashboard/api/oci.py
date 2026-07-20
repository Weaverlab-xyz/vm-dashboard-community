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
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
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
from ..services import cache_service, cloud_stats, job_service, oci_freetier, oci_service, workgroup_service
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


def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


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
    """Sum the free-tier footprint of live (completed, non-destroyed) OCI VMs this
    dashboard deployed, so the guardrail can flag 'this would exceed the free
    count/budget'. Best-effort (reads job metadata; not account-wide usage)."""
    amd = 0
    a1_ocpus = 0.0
    a1_mem = 0.0
    jobs = (db.query(Job)
            .filter(Job.job_type == "oci_deploy", Job.status == "completed").all())
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


@router.post("/deploy", response_model=OCIDeployResponse)
async def deploy_instance(
    payload: OCIDeployRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_permission("oci", "write")),
    db: Session = Depends(get_db),
):
    """Deploy an OCI compute instance from an image. Runs in background; returns a
    job id immediately. Enforces the free-tier warn-and-confirm gate."""
    if not _configured():
        raise HTTPException(status_code=400, detail="OCI not configured — run the setup wizard.")

    compartment = _compartment()
    workgroup = _validate_workgroup(db, current_user, payload.workgroup)
    payload.workgroup = workgroup

    # ── Free-tier guardrail (warn + confirm) ──────────────────────────────────
    if _oci_cfg("oci_freetier_enforce", "1") not in ("0", "false", "False", ""):
        usage = _existing_freetier_usage(db)
        within, warnings = oci_freetier.evaluate(
            shape=payload.shape, ocpus=payload.ocpus, memory_gb=payload.memory_gb,
            boot_volume_gb=payload.boot_volume_gb, **usage,
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

    # Pre-action policy gate (inert unless enabled + this action is gated).
    from ..services import admission_service
    admission_service.enforce(
        "oci:compute:deploy",
        request={"region": _region(), "instance_type": payload.shape,
                 "image": payload.image_ocid, "name": payload.instance_name},
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
        },
    )
    job_service.set_cloud_resource_id(db, job.id, payload.instance_name)
    job_service.log_audit(
        db, current_user.username, "oci_deploy",
        details={"instance_name": payload.instance_name, "shape": payload.shape, "workgroup": workgroup},
    )
    background_tasks.add_task(_run_deploy, job.id, payload, compartment)
    return OCIDeployResponse(job_id=job.id, status="pending", message=f"Deploying {payload.instance_name}…")


async def _run_deploy(job_id: str, payload: OCIDeployRequest, compartment: str) -> None:
    from ..services import config_service as _cfg_svc
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)

        # Resolve the SSH public key (per-launch override wins over the default).
        secret = (payload.ssh_key_secret_override or _cfg_svc.get("oci_ssh_key_secret") or "")
        ssh_public_key = ""
        if secret:
            job_service.update_progress(db, job_id, 15, "Retrieving SSH public key from OCI Vault…")
            try:
                ssh_public_key = await oci_service.get_ssh_public_key(secret)
            except Exception as exc:
                logger.warning("Could not fetch OCI SSH key: %s", exc)

        job_service.update_progress(db, job_id, 25, "Launching compute instance…")
        result = await oci_service.launch_instance(
            compartment_id=compartment,
            availability_domain=payload.availability_domain,
            instance_name=payload.instance_name,
            shape=payload.shape,
            image_ocid=payload.image_ocid,
            subnet_ocid=payload.subnet_ocid or _cfg_svc.get("oci_default_subnet_ocid") or "",
            assign_public_ip=payload.assign_public_ip,
            ssh_public_key=ssh_public_key,
            ocpus=payload.ocpus,
            memory_gb=payload.memory_gb,
            boot_volume_gb=payload.boot_volume_gb,
            workgroup=payload.workgroup,
        )

        hostname = result.get("public_ip") or result.get("private_ip") or payload.instance_name
        final_meta = {
            "instance_ocid":  result["ocid"],
            "instance_name":  result["display_name"],
            "shape":          result.get("shape"),
            "ocpus":          result.get("ocpus"),
            "memory_gb":      result.get("memory_gb"),
            "lifecycle_state": result.get("lifecycle_state"),
            "public_ip":      result.get("public_ip"),
            "private_ip":     result.get("private_ip"),
            "availability_domain": result.get("availability_domain"),
            "image_ocid":     payload.image_ocid,
            "image_name":     payload.image_name,
            "region":         _region(),
        }

        # ── BeyondTrust PRA — Shell Jump (optional) ───────────────────────────
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import terraform_pra_service
            jump_group = (payload.jump_group or _cfg_svc.get("oci_bt_jump_group_name")
                          or _cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name)
            jumpoint_name = (payload.jumpoint_name or _cfg_svc.get("oci_jumpoint_name")
                             or _cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name)
            job_service.update_progress(db, job_id, 90, f"Instance launched ({hostname}), provisioning Shell Jump…")
            try:
                bt_result = await terraform_pra_service.provision_jump(
                    vm_name=payload.instance_name, hostname=hostname,
                    jump_group_name=jump_group, jumpoint_name=jumpoint_name, tag="OCI",
                )
                final_meta["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                final_meta["bt_jump_group_name"] = bt_result.get("jump_group_name")
                final_meta["bt_tf_state"] = bt_result.get("tf_state_json")
                job_service.update_progress(db, job_id, 95,
                    f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, group: {jump_group})")
            except Exception as bt_exc:
                final_meta["bt_error"] = str(bt_exc)
                job_service.update_progress(db, job_id, 95,
                    f"Instance deployed but Shell Jump provisioning failed: {bt_exc}")
        else:
            job_service.update_progress(db, job_id, 95, "Instance launched.")

        # Entitle — register as SSH ephemeral-accounts integration (per-build opt-in).
        from ..services import entitle_vm_hook
        if payload.register_in_entitle and entitle_vm_hook.registration_enabled():
            # Resolved login user (config override → request field → opc default);
            # _cfg_svc.get is store-only so a blank request field falls through.
            await entitle_vm_hook.register(db, job_id, payload.instance_name, hostname,
                                           private=not payload.assign_public_ip,
                                           result=final_meta, tag="OCI",
                                           sudo_user=_cfg_svc.get("oci_ssh_username") or payload.ssh_username or "opc",
                                           ssh_key_secret=secret)

        # Password Safe — onboard as a managed system + account (per-build opt-in).
        # OCI uses the traditional "ssh" method (no cloud-native plugin), so the
        # chosen key secret must carry the VM's private key.
        from ..services import ps_vm_hook
        if payload.register_in_passwordsafe and ps_vm_hook.registration_enabled():
            await ps_vm_hook.register(db, job_id, payload.instance_name, hostname,
                                      result=final_meta, tag="OCI", ssh_key_secret=secret)

        job_service.set_completed(db, job_id, final_meta)
        await cache_service.invalidate(cache_service.key_global("oci_instances"))
    except Exception as exc:
        logger.error("OCI deploy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()


@router.delete("/instances/{instance_ocid:path}")
async def destroy_instance(
    instance_ocid: str,
    background_tasks: BackgroundTasks = None,
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
    background_tasks.add_task(_run_destroy, job.id, instance_ocid, deploy_job.id if deploy_job else None)
    return {"job_id": job.id, "status": "pending", "message": "Terminating instance…"}


async def _run_destroy(job_id: str, instance_ocid: str, deploy_job_id: Optional[str] = None) -> None:
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        result = {"instance_ocid": instance_ocid}

        deploy_meta = {}
        if deploy_job_id:
            deploy_job = job_service.get_job(db, deploy_job_id)
            if deploy_job:
                deploy_meta = deploy_job.metadata_dict

        # Remove the BeyondTrust Shell Jump before terminating the instance.
        if deploy_meta.get("bt_tf_state"):
            job_service.update_progress(db, job_id, 20, "Removing BeyondTrust Shell Jump…")
            try:
                from ..services import terraform_pra_service
                await terraform_pra_service.remove_jump(deploy_meta["bt_tf_state"])
                result["bt_shell_jump_removed"] = deploy_meta.get("bt_shell_jump_id")
            except Exception as e:
                result["bt_error"] = str(e)
                logger.error("OCI Shell Jump removal failed: %s", e)

        if deploy_meta.get("entitle_registration_tf_state"):
            from ..services import entitle_vm_hook
            await entitle_vm_hook.deregister(deploy_meta, result)

        if deploy_meta.get("ps_registration_tf_state"):
            from ..services import ps_vm_hook
            await ps_vm_hook.deregister(deploy_meta, result)

        job_service.update_progress(db, job_id, 50, "Terminating instance…")
        await oci_service.terminate_instance(instance_ocid)

        if deploy_job_id:
            deploy_meta["destroyed"] = True
            if job_service.get_job(db, deploy_job_id):
                job_service.set_completed(db, deploy_job_id, deploy_meta)

        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("oci_instances"))
    except Exception as exc:
        logger.error("OCI destroy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()
