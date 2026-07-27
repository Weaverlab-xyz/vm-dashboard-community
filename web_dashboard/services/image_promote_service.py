"""Automated image promotion — the SDK-driven path behind /api/images/{id}/promote.

Dispatched by ``jobs_worker`` as ``image_promote_{aws,azure,gcp,oci}``. The endpoint
validates and enqueues; the promote itself runs here so it survives a gunicorn worker
recycle, and so the worker doesn't have to import the API package.
"""
import logging

from . import image_registry_service, job_service
from .image_registry_service import ImageRegistryError
from .promote_runner_service import PromoteRunnerError

logger = logging.getLogger(__name__)


async def run(job_id: str, job_type: str, meta: dict) -> None:
    """Run one automated promote. Collapses the runner's four near-identical
    branches — the only per-cloud difference is Azure also taking a resource group."""
    image_id = meta["image_id"]
    region = meta.get("target_region") or ""
    if job_type == "image_promote_azure":
        await _run_azure_automated_promote(
            image_id, meta.get("target_resource_group") or "", region, job_id)
        return
    fn = {"image_promote_aws": _run_aws_automated_promote,
          "image_promote_gcp": _run_gcp_automated_promote,
          "image_promote_oci": _run_oci_automated_promote}[job_type]
    await fn(image_id, region, job_id)



# ── Promote ──────────────────────────────────────────────────────────────────

async def _run_aws_automated_promote(
    image_id: str, target_region: str, job_id: str,
) -> None:
    """Background-task wrapper around image_registry_service.promote_to_aws_automated.
    Owns its own DB session because BackgroundTasks runs after the request
    handler returns and the request session is closed."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        # Mark running so the job UI shows a real status + duration instead of
        # sitting at "pending" for the whole promote.
        job_service.set_running(db, job_id)

        def _progress(pct: int, msg: str) -> None:
            try:
                job_service.update_progress(db, job_id, pct, msg[:200])
            except Exception:
                logger.exception("Failed to update job %s progress", job_id)

        try:
            updated = await image_registry_service.promote_to_aws_automated(
                db, image_id, target_region=target_region, progress_cb=_progress,
            )
            promo = (updated.get("promotions") or {}).get("aws") or {}
            job_service.set_completed(db, job_id, {
                "ami_id":     promo.get("image_id"),
                "region":     promo.get("region"),
                "promotions": updated.get("promotions"),
            })
        except (ImageRegistryError, PromoteRunnerError) as e:
            # Surface the runner log tail (if any) so the operator can read
            # qemu-img output / S3 upload errors from the Job page.
            extra = ""
            if isinstance(e, PromoteRunnerError) and getattr(e, "log_output", ""):
                extra = "\n--- runner log ---\n" + e.log_output[-4000:]
            job_service.set_failed(db, job_id, f"{e}{extra}")
            # Record the failed state on the image too so the /images page
            # row reflects reality.
            try:
                image_registry_service.record_promotion(
                    db, image_id, "aws",
                    status="failed",
                    region=target_region,
                    notes=str(e),
                )
            except Exception:
                logger.exception("Failed to record promotion failure for %s", image_id)
        except Exception as e:
            logger.exception("Automated AWS promote of %s raised unexpectedly", image_id)
            job_service.set_failed(db, job_id, f"Unexpected: {e}")
    finally:
        db.close()


async def _run_gcp_automated_promote(
    image_id: str,
    target_region: str,
    job_id: str,
) -> None:
    """Background-task wrapper for GCP automated promote. Mirrors the
    AWS / Azure wrappers."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        # Mark running so the job UI shows a real status + duration instead of
        # sitting at "pending" for the whole promote.
        job_service.set_running(db, job_id)

        def _progress(pct: int, msg: str) -> None:
            try:
                job_service.update_progress(db, job_id, pct, msg[:200])
            except Exception:
                logger.exception("Failed to update job %s progress", job_id)

        try:
            updated = await image_registry_service.promote_to_gcp_automated(
                db, image_id,
                target_region=target_region or None,
                progress_cb=_progress,
            )
            promo = (updated.get("promotions") or {}).get("gcp") or {}
            job_service.set_completed(db, job_id, {
                "self_link": promo.get("self_link") or promo.get("image_id"),
                "region":    promo.get("region"),
                "promotions": updated.get("promotions"),
            })
        except (ImageRegistryError, PromoteRunnerError) as e:
            extra = ""
            if isinstance(e, PromoteRunnerError) and getattr(e, "log_output", ""):
                extra = "\n--- runner log ---\n" + e.log_output[-4000:]
            job_service.set_failed(db, job_id, f"{e}{extra}")
            try:
                image_registry_service.record_promotion(
                    db, image_id, "gcp",
                    status="failed",
                    region=target_region,
                    notes=str(e),
                )
            except Exception:
                logger.exception("Failed to record promotion failure for %s", image_id)
        except Exception as e:
            logger.exception("Automated GCP promote of %s raised unexpectedly", image_id)
            job_service.set_failed(db, job_id, f"Unexpected: {e}")
    finally:
        db.close()


async def _run_oci_automated_promote(
    image_id: str,
    target_region: str,
    job_id: str,
) -> None:
    """Background-task wrapper for OCI automated promote. Mirrors the GCP wrapper."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.set_running(db, job_id)

        def _progress(pct: int, msg: str) -> None:
            try:
                job_service.update_progress(db, job_id, pct, msg[:200])
            except Exception:
                logger.exception("Failed to update job %s progress", job_id)

        try:
            updated = await image_registry_service.promote_to_oci_automated(
                db, image_id,
                target_region=target_region or None,
                progress_cb=_progress,
            )
            promo = (updated.get("promotions") or {}).get("oci") or {}
            job_service.set_completed(db, job_id, {
                "image_ocid": promo.get("image_id"),
                "region":     promo.get("region"),
                "promotions": updated.get("promotions"),
            })
        except (ImageRegistryError, PromoteRunnerError) as e:
            extra = ""
            if isinstance(e, PromoteRunnerError) and getattr(e, "log_output", ""):
                extra = "\n--- runner log ---\n" + e.log_output[-4000:]
            job_service.set_failed(db, job_id, f"{e}{extra}")
            try:
                image_registry_service.record_promotion(
                    db, image_id, "oci", status="failed",
                    region=target_region, notes=str(e),
                )
            except Exception:
                logger.exception("Failed to record promotion failure for %s", image_id)
        except Exception as e:
            logger.exception("Automated OCI promote of %s raised unexpectedly", image_id)
            job_service.set_failed(db, job_id, f"Unexpected: {e}")
    finally:
        db.close()


async def _run_azure_automated_promote(
    image_id: str,
    target_resource_group: str,
    target_region: str,
    job_id: str,
) -> None:
    """Background-task wrapper for Azure automated promote. Mirrors the AWS
    wrapper; separate function so each cloud's failure path can record
    state in its own promotions slot."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        # Mark running so the job UI shows a real status + duration instead of
        # sitting at "pending" for the whole promote.
        job_service.set_running(db, job_id)

        def _progress(pct: int, msg: str) -> None:
            try:
                job_service.update_progress(db, job_id, pct, msg[:200])
            except Exception:
                logger.exception("Failed to update job %s progress", job_id)

        try:
            updated = await image_registry_service.promote_to_azure_automated(
                db, image_id,
                target_resource_group=target_resource_group,
                target_location=target_region or None,
                progress_cb=_progress,
            )
            promo = (updated.get("promotions") or {}).get("azure") or {}
            job_service.set_completed(db, job_id, {
                "resource_id": promo.get("image_id"),
                "region":      promo.get("region"),
                "promotions":  updated.get("promotions"),
            })
        except (ImageRegistryError, PromoteRunnerError) as e:
            extra = ""
            if isinstance(e, PromoteRunnerError) and getattr(e, "log_output", ""):
                extra = "\n--- runner log ---\n" + e.log_output[-4000:]
            job_service.set_failed(db, job_id, f"{e}{extra}")
            try:
                image_registry_service.record_promotion(
                    db, image_id, "azure",
                    status="failed",
                    region=target_region,
                    notes=str(e),
                )
            except Exception:
                logger.exception("Failed to record promotion failure for %s", image_id)
        except Exception as e:
            logger.exception("Automated Azure promote of %s raised unexpectedly", image_id)
            job_service.set_failed(db, job_id, f"Unexpected: {e}")
    finally:
        db.close()
