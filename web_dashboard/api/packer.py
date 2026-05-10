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
        await _export_and_register_aws(db, job_id, req, result, region, created_by)

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

        job_service.update_progress(db, job_id, 5, "Generating Packer template…")
        template = packer_service.generate_azure_template(
            image_publisher=req.image_publisher,
            image_offer=req.image_offer,
            image_sku=req.image_sku,
            vm_size=req.vm_size,
            image_name=req.image_name,
            has_provisioner=bool(req.provisioner_script.strip()),
        )
        (build_dir / "build.pkr.hcl").write_text(template)
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
        await _export_and_register_azure(db, job_id, req, result, resource_group, created_by)

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
        await _export_and_register_gcp(db, job_id, req, result, project_id, created_by)

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


# ── Export + auto-register helpers (Phase 2 — build-once, promote-many) ───────
#
# Each helper runs after the cloud-native Packer build succeeds. It checks that
# the operator's active /storage backend matches the build cloud (so the export
# lands in their authoritative artefact home), exports the freshly built image
# to a portable VHD via cloud-native APIs, and registers the result on the
# /images page so it can be tracked + promoted.
#
# Mismatch (e.g. AWS build but active backend is GCS) is non-fatal — the build
# itself succeeded and the operator can promote manually via /images. Phase 3
# will add cross-backend copy so any active backend can host any cloud's
# exports.

def _versioned_blob_name(image_name: str, ext: str = "vhd") -> str:
    from datetime import datetime
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"images/{image_name}-{ts}.{ext}"


async def _export_and_register_aws(
    db, job_id: str, req: AWSPackerBuildRequest, result: dict,
    region: str, created_by: str,
) -> None:
    artefact_id = result.get("artifact_id")
    if not artefact_id:
        result["export_skipped"] = "no AMI ID parsed from packer output"
        return

    backend = storage_service.active_backend()
    if backend != "s3":
        msg = (
            f"Export skipped: active storage backend is '{backend or 'none'}', "
            f"expected 's3' for AWS builds. Promote manually from /images."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return

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

        registered = image_registry_service.register_image(
            db,
            name=req.image_name,
            version=export["task_id"],
            source_cloud="aws",
            created_by=created_by,
            description=f"Auto-registered from packer build {job_id}",
            source_image_id=artefact_id,
            source_region=region,
            artefact_url=export["s3_url"],
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("AWS export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)


async def _export_and_register_azure(
    db, job_id: str, req: AzurePackerBuildRequest, result: dict,
    resource_group: str, created_by: str,
) -> None:
    artefact_id = result.get("artifact_id")
    if not artefact_id:
        result["export_skipped"] = "no managed image ID parsed from packer output"
        return

    backend = storage_service.active_backend()
    if backend != "azure_blob":
        msg = (
            f"Export skipped: active storage backend is '{backend or 'none'}', "
            f"expected 'azure_blob' for Azure builds. Promote manually from /images."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return

    # Packer azure-arm artifact ID is the full resource path. The image name is
    # the last segment; the resource group is configurable on the build request.
    image_name = artefact_id.rstrip("/").split("/")[-1]
    storage_account = _cfg("storage_azure_account")
    container = _cfg("storage_azure_container") or "playbooks"
    blob_name = _versioned_blob_name(req.image_name)

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(db, job_id, 96, f"Exporting {image_name} to blob {storage_account}/{container}/{blob_name}")
        export = await azure_service.export_managed_image_to_vhd(
            image_rg=resource_group,
            image_name=image_name,
            dest_storage_account=storage_account,
            dest_container=container,
            dest_blob_name=blob_name,
            progress_cb=_on_progress,
        )
        result["export"] = export

        registered = image_registry_service.register_image(
            db,
            name=req.image_name,
            version=blob_name.split("/")[-1].rsplit(".", 1)[0],
            source_cloud="azure",
            created_by=created_by,
            description=f"Auto-registered from packer build {job_id}",
            source_image_id=artefact_id,
            source_region=_cfg("azure_location") or "centralus",
            artefact_url=export["blob_url"],
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("Azure export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)


async def _export_and_register_gcp(
    db, job_id: str, req: GCPPackerBuildRequest, result: dict,
    project_id: str, created_by: str,
) -> None:
    artefact_id = result.get("artifact_id")
    if not artefact_id:
        result["export_skipped"] = "no image name parsed from packer output"
        return

    backend = storage_service.active_backend()
    if backend != "gcs":
        msg = (
            f"Export skipped: active storage backend is '{backend or 'none'}', "
            f"expected 'gcs' for GCP builds. Promote manually from /images."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return

    bucket = _cfg("storage_gcs_bucket")
    object_path = _versioned_blob_name(req.image_name)
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

        registered = image_registry_service.register_image(
            db,
            name=req.image_name,
            version=export.get("build_id") or object_path.split("/")[-1].rsplit(".", 1)[0],
            source_cloud="gcp",
            created_by=created_by,
            description=f"Auto-registered from packer build {job_id}",
            source_image_id=artefact_id,
            source_region=_cfg("gcp_zone") or "",
            artefact_url=export["gs_url"],
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("GCP export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
