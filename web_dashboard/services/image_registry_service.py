"""Image registry service.

Backs the /images page and /api/images router. The registry is operator-
maintained today (Phase 1): you tell the dashboard "this image exists at
this location" and the registry tracks where promotions land. Cross-cloud
promotion is a manual-steps payload returned by `compute_manual_steps` —
Phase 2 will replace it with native VM-import automation.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..database import RegisteredImage

logger = logging.getLogger(__name__)


VALID_CLOUDS = ("aws", "azure", "gcp")


class ImageRegistryError(Exception):
    pass


# ── Conversion helpers ────────────────────────────────────────────────────────

def _row_to_dict(row: RegisteredImage) -> dict:
    return {
        "id":              row.id,
        "name":            row.name,
        "version":         row.version,
        "description":     row.description,
        "source_cloud":    row.source_cloud,
        "source_image_id": row.source_image_id,
        "source_region":   row.source_region,
        "artefact_url":    row.artefact_url,
        "artefact_format": row.artefact_format,
        "promotions":      row.promotions_dict,
        "created_at":      row.created_at.isoformat() if row.created_at else "",
        "created_by":      row.created_by,
    }


# ── List / get ────────────────────────────────────────────────────────────────

def list_images(db: Session) -> list[dict]:
    rows = db.query(RegisteredImage).order_by(RegisteredImage.created_at.desc()).all()
    return [_row_to_dict(r) for r in rows]


def get_image(db: Session, image_id: str) -> Optional[dict]:
    row = db.get(RegisteredImage, image_id)
    return _row_to_dict(row) if row else None


# ── Register ──────────────────────────────────────────────────────────────────

def register_image(
    db: Session,
    *,
    name: str,
    version: str,
    source_cloud: str,
    created_by: str,
    description: Optional[str] = None,
    source_image_id: Optional[str] = None,
    source_region: Optional[str] = None,
    artefact_url: Optional[str] = None,
    artefact_format: Optional[str] = None,
) -> dict:
    if source_cloud not in VALID_CLOUDS:
        raise ImageRegistryError(
            f"Unknown source_cloud '{source_cloud}'. Valid: {', '.join(VALID_CLOUDS)}."
        )
    if not name.strip() or not version.strip():
        raise ImageRegistryError("name and version are required.")

    row = RegisteredImage(
        name=name.strip(),
        version=version.strip(),
        description=(description or "").strip() or None,
        source_cloud=source_cloud,
        source_image_id=(source_image_id or "").strip() or None,
        source_region=(source_region or "").strip() or None,
        artefact_url=(artefact_url or "").strip() or None,
        artefact_format=(artefact_format or "").strip() or None,
        promotions=None,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Registered image %s/%s (id=%s, source=%s)", row.name, row.version, row.id, row.source_cloud)
    return _row_to_dict(row)


# ── Delete ────────────────────────────────────────────────────────────────────

def delete_image(db: Session, image_id: str) -> bool:
    row = db.get(RegisteredImage, image_id)
    if not row:
        return False
    db.delete(row)
    db.commit()
    logger.info("Deleted registered image %s", image_id)
    return True


# ── Promote ───────────────────────────────────────────────────────────────────

def compute_manual_steps(image: dict, target_cloud: str) -> str:
    """Generate operator-readable instructions for promoting `image` to
    `target_cloud`. Phase 2 will replace this with automated VM-import calls;
    today the operator runs the listed commands by hand."""
    src = image["source_cloud"]
    artefact = image.get("artefact_url") or "<not set>"
    fmt = image.get("artefact_format") or "<not set>"
    name = image["name"]
    version = image["version"]
    src_id = image.get("source_image_id") or "<not set>"

    if src == target_cloud:
        # Same-cloud cross-region copy.
        if target_cloud == "aws":
            return (
                f"Copy AMI {src_id} to the target region:\n"
                f"  aws ec2 copy-image --source-image-id {src_id} \\\n"
                f"    --source-region {image.get('source_region') or '<source-region>'} \\\n"
                f"    --region <target-region> \\\n"
                f"    --name '{name}-{version}'\n"
                f"Then update the promotion record on /images with the new AMI ID."
            )
        if target_cloud == "azure":
            return (
                f"Same-region duplicate isn't useful for Azure managed images.\n"
                f"To copy to a different region, deallocate-generalize-capture in the\n"
                f"target region or use Shared Image Gallery replication:\n"
                f"  az sig image-version create --gallery-name <gallery> \\\n"
                f"    --gallery-image-definition <def> --gallery-image-version {version} \\\n"
                f"    --target-regions <region1> <region2> \\\n"
                f"    --managed-image {src_id}"
            )
        if target_cloud == "gcp":
            return (
                f"GCP custom images are global by default — they're already\n"
                f"reachable from every region. No promotion needed.\n"
                f"If you need a separate image record:\n"
                f"  gcloud compute images create {name}-{version}-copy --source-image={src_id}"
            )

    # Cross-cloud promotion. Each path is a 3-step (export → copy → import).
    pair = (src, target_cloud)
    if pair == ("aws", "azure"):
        return (
            f"Cross-cloud promote AWS → Azure (3 steps):\n"
            f"\n"
            f"1. Export the AMI to S3 as VHD:\n"
            f"   aws ec2 export-image --image-id {src_id} \\\n"
            f"     --disk-image-format VHD --s3-export-location S3Bucket=<your-bucket>,S3Prefix=images/\n"
            f"\n"
            f"2. Copy the VHD to Azure Blob Storage:\n"
            f"   azcopy copy 'https://<your-bucket>.s3.amazonaws.com/images/{src_id}.vhd' \\\n"
            f"     'https://<storage-account>.blob.core.windows.net/<container>/{name}-{version}.vhd'\n"
            f"\n"
            f"3. Create the Azure managed image from the VHD:\n"
            f"   az image create --name {name}-{version} --resource-group <rg> \\\n"
            f"     --source 'https://<storage-account>.blob.core.windows.net/<container>/{name}-{version}.vhd' \\\n"
            f"     --os-type Linux --hyper-v-generation V2"
        )
    if pair == ("aws", "gcp"):
        return (
            f"Cross-cloud promote AWS → GCP (3 steps):\n"
            f"\n"
            f"1. Export the AMI to S3 as RAW:\n"
            f"   aws ec2 export-image --image-id {src_id} \\\n"
            f"     --disk-image-format RAW --s3-export-location S3Bucket=<your-bucket>,S3Prefix=images/\n"
            f"\n"
            f"2. Copy the RAW to GCS (tar.gz wrap is required):\n"
            f"   aws s3 cp s3://<your-bucket>/images/{src_id}.raw - | gzip | \\\n"
            f"     gsutil cp - gs://<gcs-bucket>/{name}-{version}.tar.gz\n"
            f"\n"
            f"3. Create the GCP custom image:\n"
            f"   gcloud compute images create {name}-{version} \\\n"
            f"     --source-uri=gs://<gcs-bucket>/{name}-{version}.tar.gz"
        )
    if pair == ("azure", "aws"):
        return (
            f"Cross-cloud promote Azure → AWS (3 steps):\n"
            f"\n"
            f"1. Identify the underlying VHD blob URL of managed image {src_id}.\n"
            f"   Use 'az image show' to find the os-disk source URI.\n"
            f"\n"
            f"2. Copy the VHD to S3:\n"
            f"   azcopy copy '<vhd-blob-url>' 'https://<your-bucket>.s3.amazonaws.com/images/{name}-{version}.vhd'\n"
            f"\n"
            f"3. Import to AWS:\n"
            f"   aws ec2 import-image --description '{name}-{version}' \\\n"
            f"     --disk-containers Format=VHD,UserBucket={{S3Bucket=<your-bucket>,S3Key=images/{name}-{version}.vhd}}"
        )
    if pair == ("azure", "gcp"):
        return (
            f"Cross-cloud promote Azure → GCP (3 steps):\n"
            f"\n"
            f"1. Export Azure managed image to a VHD blob (az image export\n"
            f"   doesn't exist — copy the underlying os-disk VHD instead).\n"
            f"\n"
            f"2. Copy + repackage as RAW.tar.gz:\n"
            f"   azcopy copy '<vhd-blob-url>' /tmp/{name}.vhd\n"
            f"   qemu-img convert -f vpc -O raw /tmp/{name}.vhd /tmp/disk.raw\n"
            f"   tar czf {name}-{version}.tar.gz disk.raw\n"
            f"   gsutil cp {name}-{version}.tar.gz gs://<gcs-bucket>/\n"
            f"\n"
            f"3. Create the GCP custom image:\n"
            f"   gcloud compute images create {name}-{version} \\\n"
            f"     --source-uri=gs://<gcs-bucket>/{name}-{version}.tar.gz"
        )
    if pair == ("gcp", "aws"):
        return (
            f"Cross-cloud promote GCP → AWS (3 steps):\n"
            f"\n"
            f"1. Export the GCP custom image to GCS as tar.gz:\n"
            f"   gcloud compute images export --image={src_id} \\\n"
            f"     --destination-uri=gs://<gcs-bucket>/{name}-{version}.tar.gz\n"
            f"\n"
            f"2. Decompress + copy to S3 as VMDK or RAW:\n"
            f"   gsutil cp gs://<gcs-bucket>/{name}-{version}.tar.gz - | tar xzO disk.raw | \\\n"
            f"     aws s3 cp - s3://<your-bucket>/images/{name}-{version}.raw\n"
            f"\n"
            f"3. Import to AWS:\n"
            f"   aws ec2 import-image --description '{name}-{version}' \\\n"
            f"     --disk-containers Format=RAW,UserBucket={{S3Bucket=<your-bucket>,S3Key=images/{name}-{version}.raw}}"
        )
    if pair == ("gcp", "azure"):
        return (
            f"Cross-cloud promote GCP → Azure (3 steps):\n"
            f"\n"
            f"1. Export the GCP custom image to GCS:\n"
            f"   gcloud compute images export --image={src_id} \\\n"
            f"     --destination-uri=gs://<gcs-bucket>/{name}-{version}.tar.gz --export-format=vhd\n"
            f"\n"
            f"2. Copy the VHD to Azure Blob:\n"
            f"   gsutil cp gs://<gcs-bucket>/{name}-{version}.vhd /tmp/\n"
            f"   azcopy copy /tmp/{name}-{version}.vhd \\\n"
            f"     'https://<storage-account>.blob.core.windows.net/<container>/{name}-{version}.vhd'\n"
            f"\n"
            f"3. Create the Azure managed image:\n"
            f"   az image create --name {name}-{version} --resource-group <rg> \\\n"
            f"     --source 'https://<storage-account>.blob.core.windows.net/<container>/{name}-{version}.vhd' \\\n"
            f"     --os-type Linux --hyper-v-generation V2"
        )

    return f"No manual-steps template for {src} → {target_cloud}. Add one in image_registry_service.py."


# ── Pre-flight ────────────────────────────────────────────────────────────────
#
# Pure-Python checks (artefact recorded, format compat, cross-storage required,
# target creds configured) that surface obvious blockers before the operator
# runs the manual import. No network I/O — every probe reads local state, so
# the call returns synchronously in <100ms and there's no SaaS-only durability
# concern. Live cloud-side checks (vmimport role exists, quota available) can
# be layered on later without changing the response shape.

def _format_compat_check(fmt: str, target: str) -> dict:
    matrix = {
        "aws":   {"vhd": "pass", "vmdk": "pass", "raw": "pass", "ova": "pass"},
        "azure": {"vhd": "pass"},
        "gcp":   {"vhd": "pass", "raw": "pass", "vmdk": "warn"},
    }
    status = matrix.get(target, {}).get(fmt) or ("fail" if fmt else "warn")
    if not fmt:
        detail = "artefact_format is unset; the import API needs to know the format."
    elif status == "pass":
        detail = f"{target.upper()} import accepts {fmt.upper()} natively."
    elif status == "warn":
        detail = f"{target.upper()} import accepts {fmt.upper()} but conversion is slower or less reliable."
    else:
        detail = f"Format {fmt.upper()} not supported by {target.upper()} import. Convert with qemu-img first."
    return {"name": "Artefact format compatibility", "status": status, "detail": detail}


def _cross_storage_check(src: str, target: str) -> dict:
    return {
        "name":   "Cross-storage copy required",
        "status": "warn",
        "detail": (
            f"Artefact is in {src.upper()} storage; {target.upper()} import expects it in "
            f"{target.upper()} storage. Run azcopy/aws s3 cp/gsutil per the manual steps "
            f"(Phase 3 will automate this)."
        ),
    }


def _aws_creds_configured() -> dict:
    from . import config_service
    have = bool(config_service.get("aws_access_key_id")) and bool(config_service.get("aws_secret_access_key"))
    return {
        "name":   "Target credentials configured",
        "status": "pass" if have else "fail",
        "detail": (
            "AWS credentials present in config." if have
            else "aws_access_key_id / aws_secret_access_key not set. Configure in the setup wizard."
        ),
    }


def _azure_creds_configured() -> dict:
    from . import config_service
    have = all(config_service.get(k) for k in (
        "azure_client_id", "azure_client_secret", "azure_tenant_id", "azure_subscription_id"
    ))
    return {
        "name":   "Target credentials configured",
        "status": "pass" if have else "fail",
        "detail": (
            "Azure service-principal credentials present." if have
            else "azure_client_id/secret/tenant/subscription not set. Configure in the setup wizard."
        ),
    }


def _gcp_creds_configured() -> dict:
    from . import config_service
    have = bool(config_service.get("gcp_project_id")) and bool(config_service.get("gcp_service_account_json"))
    return {
        "name":   "Target credentials configured",
        "status": "pass" if have else "fail",
        "detail": (
            "GCP project + service-account JSON present." if have
            else "gcp_project_id / gcp_service_account_json not set. Configure in the setup wizard."
        ),
    }


def compute_preflight_checks(image: dict, target_cloud: str) -> list[dict]:
    """Return a list of {name, status, detail} pre-flight items.

    Each item has status in {"pass", "warn", "fail"}. None of the checks
    block the operator from proceeding to the manual-steps view; they're
    advisory. Phase 4 (SaaS-only) will add live cloud-side checks
    (vmimport role probe, quota probe, source-blob HEAD).
    """
    if target_cloud not in VALID_CLOUDS:
        raise ImageRegistryError(f"Unknown target_cloud '{target_cloud}'.")

    checks: list[dict] = []
    artefact_url = image.get("artefact_url")
    if artefact_url:
        checks.append({
            "name":   "Artefact recorded",
            "status": "pass",
            "detail": artefact_url,
        })
    else:
        checks.append({
            "name":   "Artefact recorded",
            "status": "fail",
            "detail": "No artefact_url on this image. Re-run the build (Phase 2 auto-export) or set the URL manually.",
        })

    checks.append(_format_compat_check((image.get("artefact_format") or "").lower(), target_cloud))

    src = image["source_cloud"]
    if src != target_cloud:
        checks.append(_cross_storage_check(src, target_cloud))

    checks.append({
        "aws":   _aws_creds_configured,
        "azure": _azure_creds_configured,
        "gcp":   _gcp_creds_configured,
    }[target_cloud]())

    return checks


def record_promotion(
    db: Session,
    image_id: str,
    target_cloud: str,
    *,
    status: str,
    image_id_value: Optional[str] = None,
    region: Optional[str] = None,
    self_link: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Update or insert a promotion record under image.promotions[target_cloud]."""
    if target_cloud not in VALID_CLOUDS:
        raise ImageRegistryError(f"Unknown target_cloud '{target_cloud}'.")
    row = db.get(RegisteredImage, image_id)
    if not row:
        raise ImageRegistryError(f"Image {image_id} not found.")
    promos = row.promotions_dict
    promos[target_cloud] = {
        "status":      status,
        "image_id":    image_id_value,
        "region":      region,
        "self_link":   self_link,
        "notes":       notes,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
    row.promotions = json.dumps(promos)
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


# ── Automated promote: AWS target ────────────────────────────────────────────
#
# Replaces the CLI walkthrough for AWS-as-target promotes. The dashboard runs
# the conversion + upload inside an ECS Fargate task (so multi-GB transfers
# don't block the gunicorn web tier), then calls ec2:ImportImage against the
# staged S3 object, polls until the resulting AMI is `Available`, and
# finally deletes the staged S3 blob.
#
# Same-cloud (AWS source → AWS target, cross-region) skips the runner — a
# native ec2 copy-image does it server-side.
#
# Azure / GCP targets are PR 4 / PR 5; this PR keeps their flow on the
# manual-steps return for backwards compatibility.


def _parse_hub_url(artefact_url: str) -> tuple[str, str]:
    """Return (backend, key) for an artefact_url written by Phase 3 export.
    `s3://bucket/key` -> ("s3", "key"), Azure https URL -> ("azure_blob",
    "<key-inside-container>"), gs://... -> ("gcs", "key"). Raises
    ImageRegistryError if the URL shape doesn't match a hub backend — the
    operator probably hand-rolled it and the automated promote can't drive
    it without parsing help."""
    url = (artefact_url or "").strip()
    if url.startswith("s3://"):
        rest = url[len("s3://"):]
        _, _, key = rest.partition("/")
        return ("s3", key)
    if url.startswith("gs://"):
        rest = url[len("gs://"):]
        _, _, key = rest.partition("/")
        return ("gcs", key)
    if url.startswith("https://") and ".blob.core.windows.net/" in url:
        # https://<account>.blob.core.windows.net/<container>/<key>
        after_host = url.split(".blob.core.windows.net/", 1)[1]
        _, _, key = after_host.partition("/")
        return ("azure_blob", key)
    raise ImageRegistryError(
        f"artefact_url '{url}' isn't on a recognised hub backend (s3/azure_blob/gcs). "
        "Re-run the build with Phase 3 export to populate the hub URL, or use the "
        "manual-steps fallback."
    )


async def promote_to_aws_automated(
    db: Session,
    image_id: str,
    *,
    target_region: str,
    progress_cb=None,
) -> dict:
    """Drive an end-to-end automated promote of `image_id` to AWS as `target_region`.

    Steps:
      1. Resolve hub artefact URL -> (backend, key).
      2. Pick a staging S3 bucket+key in the target.
      3. Launch the ECS promote-runner task with a presigned hub URL; wait.
      4. ec2:ImportImage from the staged S3 object; poll until terminal.
      5. After AMI reaches `Available`, delete the staged S3 blob.
      6. Update `RegisteredImage.promotions["aws"]` with the new AMI ID.

    progress_cb is an optional sync callable taking a single string; the
    caller wires it to job_service.update_progress.
    """
    from . import promote_runner_service, storage_service, aws_service

    if not target_region:
        raise ImageRegistryError("target_region is required for AWS promote.")
    image = get_image(db, image_id)
    if not image:
        raise ImageRegistryError(f"Image {image_id} not found.")
    if not image.get("artefact_url"):
        raise ImageRegistryError(
            "Image has no artefact_url. Re-build with Phase 3 export to populate it, "
            "or fall back to the manual-steps walkthrough."
        )

    hub_backend, hub_key = _parse_hub_url(image["artefact_url"])
    source_format = (image.get("artefact_format") or "vhd").lower()
    target_format = "vhd"  # AWS import_image accepts VHD natively; no conversion if source is VHD.

    dest_bucket, dest_key = promote_runner_service.resolve_aws_staging(image["name"], image["version"])

    def _say(msg: str) -> None:
        logger.info("[promote %s -> aws] %s", image_id, msg)
        if progress_cb:
            progress_cb(msg)

    # 1+2: kick the ECS runner
    _say(f"Launching promote runner: {hub_backend}://{hub_key} -> s3://{dest_bucket}/{dest_key}")
    await promote_runner_service.run_for_aws_target(
        job_id=image_id,  # caller picks the actual Job id; we just use it as a tag.
        hub_backend=hub_backend,
        hub_key=hub_key,
        source_format=source_format,
        target_format=target_format,
        dest_bucket=dest_bucket,
        dest_key=dest_key,
        aws_region=target_region,
    )

    # 3: ec2:ImportImage from the staged S3 object
    from . import config_service
    role_name = config_service.get("aws_vmimport_role_name") or "vmimport"
    _say(f"Calling ec2:ImportImage from s3://{dest_bucket}/{dest_key}")
    import_result = await aws_service.import_image_from_vhd(
        region=target_region,
        s3_bucket=dest_bucket,
        s3_key=dest_key,
        role_name=role_name,
        description=f"Promoted from registered image {image['name']}/{image['version']}",
        disk_format=target_format,
        progress_cb=_say,
    )
    new_ami_id = import_result["image_id"]
    _say(f"Import complete: {new_ami_id}")

    # 4: cleanup staged S3 blob — only after the AMI is Available (the import
    # poll above already waits for the terminal state, so we can delete now).
    # Use boto3 directly so we delete from the staging bucket the operator
    # configured, which may not be `storage_s3_bucket` (delete_image_in
    # hard-codes that one).
    try:
        _say(f"Cleaning up staged S3 object s3://{dest_bucket}/{dest_key}")
        import asyncio
        from .aws_service import _aws_kwargs
        import boto3
        await asyncio.to_thread(
            lambda: boto3.client("s3", **_aws_kwargs(target_region)).delete_object(
                Bucket=dest_bucket, Key=dest_key,
            )
        )
    except Exception as e:
        # Best-effort — staged object remaining is non-fatal, operator can
        # sweep manually. Log it but don't fail the promote.
        logger.warning(
            "Failed to delete staged S3 object s3://%s/%s after successful promote: %s",
            dest_bucket, dest_key, e,
        )

    # 5: record final promotion state
    updated = record_promotion(
        db,
        image_id,
        "aws",
        status="completed",
        image_id_value=new_ami_id,
        region=target_region,
        notes=f"Imported via promote-runner ECS task (import_task={import_result['task_id']}).",
    )
    return updated
