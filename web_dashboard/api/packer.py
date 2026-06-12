"""
Packer image-builder API endpoints.

POST /api/packer/aws/build    — build an AMI from a source AMI
POST /api/packer/azure/build  — build an Azure Managed Image
POST /api/packer/gcp/build    — build a GCP Custom Image
"""
import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.packer import (
    AWSPackerBuildRequest,
    AzurePackerBuildRequest,
    GCPPackerBuildRequest,
    PackerBuildResponse,
)
from ..services import (
    aws_service,
    azure_service,
    gcp_service,
    image_registry_service,
    job_service,
    packer_service,
    storage_service,
)
from ..services.packer_service import PackerError
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/packer", tags=["packer"])


# ── Config helpers ────────────────────────────────────────────────────────────

def _cfg(key: str, fallback: str = "") -> str:
    from ..services import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


# ── AWS build ─────────────────────────────────────────────────────────────────

@router.post("/aws/build", response_model=PackerBuildResponse)
async def build_aws_image(
    req: AWSPackerBuildRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """Build an AMI from a source AMI using Packer (amazon-ebs builder)."""
    if not req.source_ami:
        raise HTTPException(status_code=400, detail="source_ami is required.")
    if not req.image_name:
        raise HTTPException(status_code=400, detail="image_name is required.")

    job = job_service.create_job(
        db,
        job_type="packer_aws_build",
        created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            "source_ami": req.source_ami,
            "instance_type": req.instance_type,
        },
    )
    job_service.log_audit(
        db, current_user.username, "packer_aws_build",
        details={"image_name": req.image_name, "source_ami": req.source_ami},
    )
    background_tasks.add_task(_run_aws_build, job.id, req, current_user.username)
    return PackerBuildResponse(
        job_id=job.id,
        status="pending",
        message=f"Packer AWS build queued: {req.image_name} from {req.source_ami}",
    )


# ── Azure build ───────────────────────────────────────────────────────────────

@router.post("/azure/build", response_model=PackerBuildResponse)
async def build_azure_image(
    req: AzurePackerBuildRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """Build an Azure Managed Image using Packer (azure-arm builder)."""
    if not req.image_name:
        raise HTTPException(status_code=400, detail="image_name is required.")

    job = job_service.create_job(
        db,
        job_type="packer_azure_build",
        created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            "image_publisher": req.image_publisher,
            "image_offer": req.image_offer,
            "image_sku": req.image_sku,
            "os_type": req.os_type,
        },
    )
    job_service.log_audit(
        db, current_user.username, "packer_azure_build",
        details={"image_name": req.image_name, "image_sku": req.image_sku},
    )
    background_tasks.add_task(_run_azure_build, job.id, req, current_user.username)
    return PackerBuildResponse(
        job_id=job.id,
        status="pending",
        message=f"Packer Azure build queued: {req.image_name}",
    )


# ── GCP build ─────────────────────────────────────────────────────────────────

@router.post("/gcp/build", response_model=PackerBuildResponse)
async def build_gcp_image(
    req: GCPPackerBuildRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Build a GCP Custom Image using Packer (googlecompute builder)."""
    if not req.source_image:
        raise HTTPException(status_code=400, detail="source_image is required.")
    if not req.image_name:
        raise HTTPException(status_code=400, detail="image_name is required.")

    job = job_service.create_job(
        db,
        job_type="packer_gcp_build",
        created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            "source_image": req.source_image,
            "machine_type": req.machine_type,
        },
    )
    job_service.log_audit(
        db, current_user.username, "packer_gcp_build",
        details={"image_name": req.image_name, "source_image": req.source_image},
    )
    background_tasks.add_task(_run_gcp_build, job.id, req, current_user.username)
    return PackerBuildResponse(
        job_id=job.id,
        status="pending",
        message=f"Packer GCP build queued: {req.image_name} from {req.source_image}",
    )


# ── Background task runners ───────────────────────────────────────────────────

async def _run_aws_build(job_id: str, req: AWSPackerBuildRequest, created_by: str = "system") -> None:
    db = _get_db_session()
    build_dir = packer_service.BUILDS_DIR / job_id
    try:
        job_service.set_running(db, job_id)
        build_dir.mkdir(parents=True, exist_ok=True)

        # Build env
        region = _cfg("aws_region") or "us-east-2"
        env = _base_env()
        env["AWS_ACCESS_KEY_ID"] = _cfg("aws_access_key_id")
        env["AWS_SECRET_ACCESS_KEY"] = _cfg("aws_secret_access_key")
        env["AWS_DEFAULT_REGION"] = region
        env["PKR_VAR_region"] = region

        # Generate template
        job_service.update_progress(db, job_id, 5, "Generating Packer template…")
        template = packer_service.generate_aws_template(
            source_ami=req.source_ami,
            instance_type=req.instance_type,
            ssh_username=req.ssh_username,
            image_name=req.image_name,
            has_provisioner=bool(req.provisioner_script.strip()),
        )
        (build_dir / "build.pkr.hcl").write_text(template)
        _write_provisioner(build_dir, req.provisioner_script)

        def on_progress(pct, msg):
            job_service.update_progress(db, job_id, pct, msg)

        result = await packer_service.run_build("aws", build_dir, env, on_progress)

        # Archive template
        if req.archive_template:
            bucket = _cfg("packer_aws_s3_bucket")
            if bucket:
                try:
                    uri = await packer_service.archive_to_s3(
                        build_dir / "build.pkr.hcl",
                        job_id, req.image_name, bucket,
                        {"aws_access_key_id": env["AWS_ACCESS_KEY_ID"],
                         "aws_secret_access_key": env["AWS_SECRET_ACCESS_KEY"],
                         "aws_region": region},
                    )
                    result["template_archive"] = uri
                    job_service.update_progress(db, job_id, 98, f"Template archived: {uri}")
                except Exception as e:
                    result["archive_error"] = str(e)

        # Export to portable VHD + auto-register (Phase 2)
        export_result = await export_and_register_aws(
            db, job_id, req.image_name, result.get("artifact_id") or "", region, created_by,
        )
        result.update(export_result)

        job_service.set_completed(db, job_id, result)

    except PackerError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_azure_build(job_id: str, req: AzurePackerBuildRequest, created_by: str = "system") -> None:
    db = _get_db_session()
    build_dir = packer_service.BUILDS_DIR / job_id
    try:
        job_service.set_running(db, job_id)
        build_dir.mkdir(parents=True, exist_ok=True)

        # Azure credentials
        client_id = _cfg("azure_client_id")
        client_secret = _cfg("azure_client_secret")
        tenant_id = _cfg("azure_tenant_id")
        subscription_id = _cfg("azure_subscription_id")
        resource_group = _cfg("azure_resource_group") or "dashboard-rg"
        location = _cfg("azure_location") or "centralus"

        env = _base_env()
        env["ARM_CLIENT_ID"] = client_id
        env["ARM_CLIENT_SECRET"] = client_secret
        env["ARM_TENANT_ID"] = tenant_id
        env["ARM_SUBSCRIPTION_ID"] = subscription_id
        env["PKR_VAR_resource_group"] = resource_group
        env["PKR_VAR_location"] = location

        is_windows = req.os_type.lower() == "windows"
        job_service.update_progress(db, job_id, 5, "Generating Packer template…")
        generate = (packer_service.generate_azure_windows_template if is_windows
                    else packer_service.generate_azure_template)
        template = generate(
            image_publisher=req.image_publisher,
            image_offer=req.image_offer,
            image_sku=req.image_sku,
            vm_size=req.vm_size,
            image_name=req.image_name,
            has_provisioner=bool(req.provisioner_script.strip()),
        )
        (build_dir / "build.pkr.hcl").write_text(template)
        if is_windows:
            _write_ps_provisioner(build_dir, req.provisioner_script)
            if req.vm_size in ("Standard_B1s", "Standard_B2s"):
                job_service.update_progress(
                    db, job_id, 5,
                    f"Note: {req.vm_size} (≤4 GB RAM) makes Windows builds very slow — "
                    "Standard_D2s_v3 or larger is recommended.",
                )
        else:
            _write_provisioner(build_dir, req.provisioner_script)

        def on_progress(pct, msg):
            job_service.update_progress(db, job_id, pct, msg)

        result = await packer_service.run_build("azure", build_dir, env, on_progress)

        # Archive template
        if req.archive_template:
            storage_account = _cfg("packer_azure_storage_account")
            container = _cfg("packer_azure_archive_container") or "packer-templates"
            if storage_account:
                try:
                    uri = await packer_service.archive_to_azure_blob(
                        build_dir / "build.pkr.hcl",
                        job_id, req.image_name, storage_account, container,
                        {"azure_client_id": client_id, "azure_client_secret": client_secret,
                         "azure_tenant_id": tenant_id},
                    )
                    result["template_archive"] = uri
                    job_service.update_progress(db, job_id, 98, f"Template archived: {uri}")
                except Exception as e:
                    result["archive_error"] = str(e)

        # Export to portable VHD + auto-register (Phase 2)
        export_result = await export_and_register_azure(
            db, job_id, req.image_name, result.get("artifact_id") or "", resource_group, created_by,
            os_type=req.os_type,
        )
        result.update(export_result)

        job_service.set_completed(db, job_id, result)

    except PackerError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_gcp_build(job_id: str, req: GCPPackerBuildRequest, created_by: str = "system") -> None:
    db = _get_db_session()
    build_dir = packer_service.BUILDS_DIR / job_id
    creds_file = None
    try:
        job_service.set_running(db, job_id)
        build_dir.mkdir(parents=True, exist_ok=True)

        project_id = _cfg("gcp_project_id")
        zone = _cfg("gcp_zone") or "us-central1-a"
        sa_json = _cfg("gcp_service_account_json")

        if not project_id:
            raise PackerError("GCP project_id not configured. Go to Setup → GCP.")

        env = _base_env()
        env["PKR_VAR_project_id"] = project_id
        env["PKR_VAR_zone"] = zone

        # Write service account key to a temp file; set ADC env var
        if sa_json:
            creds_file = build_dir / "credentials.json"
            creds_file.write_text(sa_json)
            env["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_file)

        job_service.update_progress(db, job_id, 5, "Generating Packer template…")
        template = packer_service.generate_gcp_template(
            source_image=req.source_image,
            machine_type=req.machine_type,
            ssh_username=req.ssh_username,
            image_name=req.image_name,
            project_id=project_id,
            zone=zone,
            has_provisioner=bool(req.provisioner_script.strip()),
        )
        (build_dir / "build.pkr.hcl").write_text(template)
        _write_provisioner(build_dir, req.provisioner_script)

        def on_progress(pct, msg):
            job_service.update_progress(db, job_id, pct, msg)

        result = await packer_service.run_build("gcp", build_dir, env, on_progress)

        # Archive template
        if req.archive_template:
            bucket = _cfg("packer_gcs_bucket")
            if bucket and sa_json:
                try:
                    uri = await packer_service.archive_to_gcs(
                        build_dir / "build.pkr.hcl",
                        job_id, req.image_name, bucket,
                        {"gcp_service_account_json": sa_json},
                    )
                    result["template_archive"] = uri
                    job_service.update_progress(db, job_id, 98, f"Template archived: {uri}")
                except Exception as e:
                    result["archive_error"] = str(e)

        # Export to portable VHD + auto-register (Phase 2)
        export_result = await export_and_register_gcp(
            db, job_id, req.image_name, result.get("artifact_id") or "", project_id, created_by,
        )
        result.update(export_result)

        job_service.set_completed(db, job_id, result)

    except PackerError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        # Always remove the service account key from disk
        if creds_file and creds_file.exists():
            try:
                creds_file.unlink()
            except Exception:
                pass
        db.close()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _base_env() -> dict:
    """Start from the current process env so PATH, HOME, etc. are inherited."""
    return dict(os.environ)


def _write_provisioner(build_dir: Path, script: str) -> None:
    """Write provision.sh. Always creates the file even if empty."""
    content = script.strip()
    if not content:
        content = "#!/bin/sh\necho 'Build complete.'"
    if not content.startswith("#!"):
        content = "#!/bin/sh\n" + content
    path = build_dir / "provision.sh"
    path.write_text(content)
    path.chmod(0o755)


def _write_ps_provisioner(build_dir: Path, script: str) -> None:
    """Write provision.ps1 for Windows builds. Always creates the file."""
    content = script.strip() or "Write-Output 'Build complete.'"
    (build_dir / "provision.ps1").write_text(content)


# ── Export + auto-register helpers (Phase 2/3 — build-once, promote-many) ────
#
# Each helper runs after the cloud-native Packer build succeeds. The flow:
#
#   1. Native export to same-cloud storage (only cloud-native APIs can pull
#      bytes out of a freshly built AMI / managed image / GCE image, so the
#      first hop is always same-cloud — S3 for AWS, Blob for Azure, GCS for
#      GCP).
#   2. If the hub backend (`storage_service.hub_backend()`) is the same as
#      same-cloud storage, register with the URL the native export already
#      produced — done.
#   3. Else, copy the artefact from same-cloud staging to the hub backend
#      (Phase 3 cross-backend copy), delete the same-cloud staging blob, and
#      register with the hub URL.
#
# Result: `RegisteredImage.artefact_url` always points at the hub regardless
# of which cloud built the image, satisfying the "hub holds the source of
# truth" design contract. Per-cloud storage is still required for the export
# step (AWS only exports to S3, Azure to Blob, GCS to GCS) but it's used as
# transient staging when the operator's hub is on a different cloud.

def _versioned_blob_name(image_name: str, ext: str = "vhd") -> str:
    from datetime import datetime
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"images/{image_name}-{ts}.{ext}"


async def _land_on_hub(
    db,
    job_id: str,
    *,
    build_backend: str,
    build_key: str,
    image_name: str,
    image_ext: str = "vhd",
) -> tuple[str, str]:
    """Ensure the artefact at `build_key` on `build_backend` ends up on the
    hub backend. If the hub IS the build backend, no-op (returns the input).
    Otherwise copies to the hub, deletes the build-side staging copy, and
    returns the new (hub_backend, hub_key) pair.

    Returns: (final_backend, final_key) for use in artefact_url generation.
    Raises StorageError on copy failure — caller decides whether to surface
    it to the build job as an export_error or fall back to the build-side URL.
    """
    hub = storage_service.hub_backend()
    if not hub:
        # No usable hub — treat the build-side staging as the artefact home.
        # This matches pre-Phase-3 behavior for installs that haven't set up
        # any backend at all.
        return (build_backend, build_key)
    if hub == build_backend:
        return (build_backend, build_key)

    hub_key = storage_service.image_key(hub, image_name, ext=image_ext)
    job_service.update_progress(
        db, job_id, 97,
        f"Copying artefact to hub backend '{hub}': {build_backend}://{build_key} → {hub}://{hub_key}",
    )
    await storage_service.copy(
        src_backend=build_backend, src_key=build_key,
        dst_backend=hub, dst_key=hub_key,
    )
    # Clean up the build-side staging copy so the operator doesn't pay for
    # two copies of every multi-GB VHD. Best-effort — log if it fails but
    # don't fail the whole build, the canonical artefact is already on the
    # hub at this point.
    try:
        job_service.update_progress(db, job_id, 98, f"Cleaning up build-side staging on '{build_backend}'")
        await storage_service.delete_image_in(build_backend, build_key)
    except Exception as e:
        logger.warning(
            "Failed to delete build-side staging copy %s://%s after hub copy: %s",
            build_backend, build_key, e,
        )
    return (hub, hub_key)


async def export_and_register_aws(
    db, job_id: str, image_name: str, artefact_id: str,
    region: str, created_by: str,
) -> dict:
    """Export an AMI to VHD on the hub backend and register the result.

    Returns a dict with keys among {export, registered_image_id,
    artefact_backend, export_error, export_skipped}. Used by both the
    Packer post-build flow and the manual /api/aws/images/{id}/export
    endpoint."""
    result: dict = {}
    if not artefact_id:
        result["export_skipped"] = "no AMI ID provided"
        return result

    # AWS ec2 export-image only writes to S3 — the operator must have an S3
    # bucket configured even if their hub backend is Azure/GCS. After the
    # native export, _land_on_hub() handles the cross-backend copy when the
    # hub isn't S3.
    if not _cfg("storage_s3_bucket"):
        msg = (
            "Export skipped: no S3 bucket configured (set storage_s3_bucket on /storage). "
            "AWS native export only writes to S3 — required even when the hub is on another cloud."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return result

    bucket = _cfg("storage_s3_bucket")
    s3_prefix = (_cfg("storage_s3_prefix") or "config-mgmt").rstrip("/") + "/images/"
    role_name = _cfg("aws_vmimport_role_name") or "vmimport"

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(db, job_id, 96, f"Exporting {artefact_id} to VHD on s3://{bucket}/{s3_prefix}")
        export = await aws_service.export_image_to_vhd(
            region=region,
            ami_id=artefact_id,
            s3_bucket=bucket,
            s3_prefix=s3_prefix,
            role_name=role_name,
            description=f"Exported by job {job_id}",
            progress_cb=_on_progress,
        )
        result["export"] = export

        # AWS export_image picks the object name (`<task-id>.vhd`). Derive the
        # S3 key from the returned URL so _land_on_hub knows what to copy.
        s3_url = export["s3_url"]
        build_key = s3_url[len(f"s3://{bucket}/"):]
        final_backend, final_key = await _land_on_hub(
            db, job_id,
            build_backend="s3", build_key=build_key,
            image_name=image_name, image_ext="vhd",
        )
        artefact_url = storage_service.image_url(final_backend, final_key)

        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=export["task_id"],
            source_cloud="aws",
            created_by=created_by,
            description=f"Registered by job {job_id}",
            source_image_id=artefact_id,
            source_region=region,
            artefact_url=artefact_url,
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        result["artefact_backend"] = final_backend
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("AWS export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
    return result


async def export_and_register_azure(
    db, job_id: str, image_name: str, artefact_id: str,
    resource_group: str, created_by: str, os_type: str = "Linux",
) -> dict:
    """Export an Azure managed image to VHD on the hub and register.

    `artefact_id` is the managed-image full resource path (from Packer's
    `artifact_id`) or a bare name. The last path segment is taken as
    the source image name. `image_name` is the registry name."""
    result: dict = {}
    if not artefact_id:
        result["export_skipped"] = "no managed image ID provided"
        return result

    if not _cfg("storage_azure_account"):
        msg = (
            "Export skipped: no Azure storage account configured "
            "(set storage_azure_account on /storage). "
            "Azure native export only writes to Blob — required even when the hub is on another cloud."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return result

    src_image_name = artefact_id.rstrip("/").split("/")[-1]
    storage_account = _cfg("storage_azure_account")
    container = _cfg("storage_azure_container") or "playbooks"
    blob_name = _versioned_blob_name(image_name)

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(db, job_id, 96, f"Exporting {src_image_name} to blob {storage_account}/{container}/{blob_name}")
        export = await azure_service.export_managed_image_to_vhd(
            image_rg=resource_group,
            image_name=src_image_name,
            dest_storage_account=storage_account,
            dest_container=container,
            dest_blob_name=blob_name,
            progress_cb=_on_progress,
        )
        result["export"] = export

        final_backend, final_key = await _land_on_hub(
            db, job_id,
            build_backend="azure_blob", build_key=blob_name,
            image_name=image_name, image_ext="vhd",
        )
        artefact_url = storage_service.image_url(final_backend, final_key)

        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=blob_name.split("/")[-1].rsplit(".", 1)[0],
            source_cloud="azure",
            created_by=created_by,
            description=f"Registered by job {job_id}",
            source_image_id=artefact_id,
            source_region=_cfg("azure_location") or "centralus",
            artefact_url=artefact_url,
            artefact_format="vhd",
            os_type=os_type,
        )
        result["registered_image_id"] = registered["id"]
        result["artefact_backend"] = final_backend
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("Azure export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
    return result


async def export_and_register_gcp(
    db, job_id: str, image_name: str, artefact_id: str,
    project_id: str, created_by: str,
) -> dict:
    """Export a GCP custom image to VHD on the hub and register.

    `artefact_id` is the source custom image name. `image_name` is the
    registry name."""
    result: dict = {}
    if not artefact_id:
        result["export_skipped"] = "no image name provided"
        return result

    if not _cfg("storage_gcs_bucket"):
        msg = (
            "Export skipped: no GCS bucket configured (set storage_gcs_bucket on /storage). "
            "GCP native export only writes to GCS — required even when the hub is on another cloud."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return result

    bucket = _cfg("storage_gcs_bucket")
    object_path = _versioned_blob_name(image_name)
    network = _cfg("gcp_export_network") or ""
    subnet = _cfg("gcp_export_subnet") or ""

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(db, job_id, 96, f"Exporting {artefact_id} to gs://{bucket}/{object_path}")
        export = await gcp_service.export_custom_image_to_vhd(
            project_id=project_id,
            image_name=artefact_id,
            dest_bucket=bucket,
            dest_object=object_path,
            network=network,
            subnet=subnet,
            progress_cb=_on_progress,
        )
        result["export"] = export

        final_backend, final_key = await _land_on_hub(
            db, job_id,
            build_backend="gcs", build_key=object_path,
            image_name=image_name, image_ext="vhd",
        )
        artefact_url = storage_service.image_url(final_backend, final_key)

        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=export.get("build_id") or object_path.split("/")[-1].rsplit(".", 1)[0],
            source_cloud="gcp",
            created_by=created_by,
            description=f"Registered by job {job_id}",
            source_image_id=artefact_id,
            source_region=_cfg("gcp_zone") or "",
            artefact_url=artefact_url,
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        result["artefact_backend"] = final_backend
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("GCP export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
    return result
