"""Image registry service.

Backs the /images page and /api/images router. The registry is operator-
maintained: you tell the dashboard "this image exists at this location" and
the registry tracks where promotions land. Cross-cloud promotion runs as
runner-driven, native VM-import automation for AWS/Azure/GCP targets (via
`promote_to_*_automated`); `compute_manual_steps` remains an operator
walkthrough fallback.
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
        "os_type":         row.os_type or "Linux",
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
    os_type: str = "Linux",
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
        os_type=(os_type or "").strip() or None,
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
    `target_cloud`. This is the manual fallback to the automated, runner-driven
    promote (`promote_to_*_automated`) — the listed commands let an operator do
    it by hand."""
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
# Azure / GCP targets have their own automated runner paths
# (`promote_to_azure_automated` / `promote_to_gcp_automated`); the
# manual-steps return remains available as a fallback.


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

    progress_cb is an optional sync callable taking (pct, msg); the caller
    wires it to job_service.update_progress.
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

    # progress_cb takes (pct, msg) so the job UI shows real phase progress
    # instead of a pinned number; string-only aws_service callbacks are adapted
    # per-phase with `lambda m: _say(<pct>, m)`.
    def _say(pct: int, msg: str) -> None:
        logger.info("[promote %s -> aws] %s", image_id, msg)
        if progress_cb:
            progress_cb(pct, msg)

    # 1+2: kick the ECS runner
    _say(10, f"Launching promote runner: {hub_backend}://{hub_key} -> s3://{dest_bucket}/{dest_key}")
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
    _say(60, f"Calling ec2:ImportImage from s3://{dest_bucket}/{dest_key}")
    import_result = await aws_service.import_image_from_vhd(
        region=target_region,
        s3_bucket=dest_bucket,
        s3_key=dest_key,
        role_name=role_name,
        description=f"Promoted from registered image {image['name']}/{image['version']}",
        disk_format=target_format,
        progress_cb=lambda m: _say(60, m),
    )
    new_ami_id = import_result["image_id"]
    _say(85, f"Import complete: {new_ami_id}")

    # 4: cleanup staged S3 blob — only after the AMI is Available (the import
    # poll above already waits for the terminal state, so we can delete now).
    # Use boto3 directly so we delete from the staging bucket the operator
    # configured, which may not be `storage_s3_bucket` (delete_image_in
    # hard-codes that one).
    try:
        _say(92, f"Cleaning up staged S3 object s3://{dest_bucket}/{dest_key}")
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


# ── Automated promote: Azure target ──────────────────────────────────────────


async def promote_to_azure_automated(
    db: Session,
    image_id: str,
    *,
    target_resource_group: Optional[str] = None,
    target_location: Optional[str] = None,
    os_type: Optional[str] = None,
    hyper_v_generation: str = "V2",
    progress_cb=None,
) -> dict:
    """End-to-end Azure promote: parse hub URL, kick the ACI runner,
    call `compute.images.begin_create_or_update`, cleanup staging,
    record promotion.

    Mirrors `promote_to_aws_automated` — same resolve-hub / kick-runner /
    SDK-import / cleanup / record shape. Diffs:
      - Runner is ACI in target Azure RG instead of ECS Fargate.
      - SDK call is `images.begin_create_or_update(...).result()` instead
        of `ec2.import_image` + poll.
      - `promotions["azure"]` carries `resource_id` (full ARM path)
        instead of an AMI id.

    target_resource_group / target_location default to the operator's
    Azure RG / location config; os_type defaults to the registry record
    (then Linux) + V2, matching the Phase-1 manual-steps walkthrough.
    """
    from . import promote_runner_service, azure_service, config_service

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
    target_format = "vhd"  # Azure managed image only accepts VHD.
    os_type = os_type or image.get("os_type") or "Linux"

    target_rg = target_resource_group or config_service.get(
        "promote_runner_azure_target_resource_group"
    ) or config_service.get("azure_resource_group")
    if not target_rg:
        raise ImageRegistryError(
            "target_resource_group is required (no fallback in promote_runner_azure_target_resource_group or azure_resource_group)."
        )
    target_loc = target_location or config_service.get("azure_location") or "centralus"
    target_storage_account_id = config_service.get("promote_runner_azure_target_storage_account_id") or ""

    dest_account, dest_container, dest_blob = promote_runner_service.resolve_azure_staging(
        image["name"], image["version"],
    )
    blob_uri = f"https://{dest_account}.blob.core.windows.net/{dest_container}/{dest_blob}"
    target_image_name = f"{image['name']}-{image['version']}"

    # progress_cb takes (pct, msg) so the job UI shows real phase progress
    # instead of a pinned number. azure_service functions want a string-only
    # callback, so we adapt them per-phase with `lambda m: _say(<pct>, m)`.
    def _say(pct: int, msg: str) -> None:
        logger.info("[promote %s -> azure] %s", image_id, msg)
        if progress_cb:
            progress_cb(pct, msg)

    # 1+2: kick the ACI runner — converts (if formats differ) + uploads to dest.
    _say(10, f"Launching promote runner: {hub_backend}://{hub_key} -> {blob_uri}")
    await promote_runner_service.run_for_azure_target(
        job_id=image_id,
        hub_backend=hub_backend,
        hub_key=hub_key,
        source_format=source_format,
        target_format=target_format,
        dest_account=dest_account,
        dest_container=dest_container,
        dest_blob=dest_blob,
        # Linux images must have waagent baked in during promotion or the
        # resulting Azure VM never finishes OS provisioning (deploy hangs at
        # "Creating"). Windows images ship the Azure VM agent already.
        install_linux_agent=(os_type or "Linux").lower() != "windows",
    )

    # 3: ask Azure compute to create a managed image from the staged blob.
    _say(60, f"Creating managed image '{target_image_name}' in {target_rg} from {blob_uri[:80]}…")
    img_result = await azure_service.create_image_from_blob(
        target_rg=target_rg,
        location=target_loc,
        image_name=target_image_name,
        blob_uri=blob_uri,
        os_type=os_type,
        hyper_v_generation=hyper_v_generation,
        storage_account_id=target_storage_account_id,
        progress_cb=lambda m: _say(60, m),
    )
    resource_id = img_result["resource_id"]
    state = img_result["provisioning_state"]
    _say(85, f"Image create returned: {state} ({resource_id})")

    if state and state.lower() != "succeeded":
        # Cloud-side import didn't reach a clean state — record failure and
        # leave the staged blob in place so the operator can inspect.
        raise ImageRegistryError(
            f"Image create finished with provisioning_state '{state}' — see Azure activity log."
        )

    # 4: cleanup staged blob — only after the cloud-side image is Succeeded.
    try:
        _say(92, f"Cleaning up staged blob {dest_account}/{dest_container}/{dest_blob}")
        await azure_service.delete_staged_blob(dest_account, dest_container, dest_blob)
    except Exception as e:
        logger.warning(
            "Failed to delete staged blob %s/%s/%s after successful promote: %s",
            dest_account, dest_container, dest_blob, e,
        )

    # 5: record final promotion state.
    updated = record_promotion(
        db,
        image_id,
        "azure",
        status="completed",
        image_id_value=resource_id,
        region=target_loc,
        notes=f"Imported via promote-runner ACI in {target_rg}.",
    )
    return updated


# ── Automated promote: GCP target ────────────────────────────────────────────


async def promote_to_gcp_automated(
    db: Session,
    image_id: str,
    *,
    target_region: Optional[str] = None,
    progress_cb=None,
) -> dict:
    """End-to-end GCP promote: parse hub URL, kick the Cloud Run runner
    (which converts vhd → raw and tar.gz-wraps), call `images.insert`,
    cleanup staging, record promotion.

    Mirrors `promote_to_aws_automated` / `promote_to_azure_automated`.
    Diffs:
      - Runner is Cloud Run Job in the dashboard's GCP project.
      - SDK call is `images.insert(rawDisk.source=gs://...tar.gz)`.
      - `promotions["gcp"]` carries `self_link` instead of an AMI id /
        Azure resource ID, normalized into the `image_id` slot.

    target_region is informational on GCP — custom images are global,
    not region-bound. We still record it on `promotions["gcp"].region`
    so the operator can read back which region's Cloud Run handled the
    promote (latency / sovereignty signal).
    """
    from . import promote_runner_service, gcp_service, config_service

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

    project_id = config_service.get("gcp_project_id")
    if not project_id:
        raise ImageRegistryError(
            "gcp_project_id is not set — needed to invoke compute.images.insert."
        )
    target_loc = target_region or config_service.get("promote_runner_gcp_region") or config_service.get("gcp_region") or ""
    family = config_service.get("promote_runner_gcp_image_family") or ""

    dest_bucket, dest_object = promote_runner_service.resolve_gcp_staging(
        image["name"], image["version"],
    )
    gcs_url = f"gs://{dest_bucket}/{dest_object}"
    # GCP custom-image names must be lowercase, alphanumeric + dash, ≤ 63
    # chars. The registry name+version usually already conforms; lowercase
    # to be safe and let the caller sort out edge cases via the existing
    # validation surface.
    target_image_name = f"{image['name']}-{image['version']}".lower()

    # progress_cb takes (pct, msg) so the job UI shows real phase progress
    # instead of a pinned number; string-only gcp_service callbacks are adapted
    # per-phase with `lambda m: _say(<pct>, m)`.
    def _say(pct: int, msg: str) -> None:
        logger.info("[promote %s -> gcp] %s", image_id, msg)
        if progress_cb:
            progress_cb(pct, msg)

    # 1+2: kick the Cloud Run runner — vhd → raw → tar.gz → GCS.
    _say(10, f"Launching promote runner: {hub_backend}://{hub_key} -> {gcs_url}")
    await promote_runner_service.run_for_gcp_target(
        job_id=image_id,
        hub_backend=hub_backend,
        hub_key=hub_key,
        source_format=source_format,
        dest_bucket=dest_bucket,
        dest_object=dest_object,
        # Linux images need google-guest-agent baked in so the promoted image
        # applies ssh-keys metadata (key-based SSH + PS SSH-rotation plugin).
        # Windows uses the separate GCEWindowsAgent.
        install_guest_agent=(image.get("os_type") or "Linux").lower() != "windows",
    )

    # 3: ask GCP compute to create a custom image from the staged tar.gz.
    _say(60, f"Calling compute.images.insert '{target_image_name}' from {gcs_url}")
    img_result = await gcp_service.create_image_from_gcs(
        project_id=project_id,
        image_name=target_image_name,
        gcs_url=gcs_url,
        description=f"Promoted from registered image {image['name']}/{image['version']}",
        family=family,
        progress_cb=lambda m: _say(60, m),
    )
    self_link = img_result["self_link"]
    status = (img_result.get("status") or "").upper()
    _say(85, f"Image insert returned: status={status} ({self_link})")

    if status and status != "READY":
        raise ImageRegistryError(
            f"images.insert finished with status '{status}' — see GCP Activity log."
        )

    # 4: cleanup the staged GCS tar.gz now that the image is READY.
    try:
        _say(92, f"Cleaning up staged object gs://{dest_bucket}/{dest_object}")
        await gcp_service.delete_gcs_object(dest_bucket, dest_object)
    except Exception as e:
        logger.warning(
            "Failed to delete staged GCS object gs://%s/%s after successful promote: %s",
            dest_bucket, dest_object, e,
        )

    # 5: record final promotion state. Self-link goes into both `image_id`
    # (so the record_promotion contract is uniform) and `self_link` for the
    # UI to render a deep-link.
    updated = record_promotion(
        db,
        image_id,
        "gcp",
        status="completed",
        image_id_value=self_link,
        region=target_loc,
        self_link=self_link,
        notes=f"Imported via promote-runner Cloud Run job in {project_id}/{target_loc or '<no-region>'}.",
    )
    return updated
