"""EPM-L package sync — download from BeyondTrust, upload to asset storage.

Dispatched by ``jobs_worker`` as an ``epml_sync`` job. It is a job rather than a
request-blocking call because the agent packages are large and BeyondTrust's download
links expire roughly 30 minutes after listing, so the sync re-lists to get fresh ones
and can take a while.

Mirrors ``ansible_cloud_run_service.run`` / ``ansible_local_run_service.run`` so the
runner's dispatch branches stay interchangeable.
"""
import logging

from . import epml_service
from . import job_service

logger = logging.getLogger(__name__)


async def run(db, *, job_id: str, meta: dict) -> None:
    """Sync EPM-L packages into the storage backend named in the job metadata."""
    backend = (meta or {}).get("backend") or ""
    job_service.set_running(db, job_id)
    try:
        job_service.update_progress(db, job_id, 10, "Checking available EPM-L packages…")
        result = await epml_service.sync_packages_to_storage(backend)

        uploaded = [name for name, done in (("RPM", result.get("rpm_uploaded")),
                                            ("DEB", result.get("deb_uploaded"))) if done]
        # "no new packages" is a legitimate outcome, not a failure — BeyondTrust may
        # simply have nothing built yet. Say which it was rather than a bare "done".
        summary = (f"{' + '.join(uploaded)} uploaded to {backend or 'the active backend'}"
                   if uploaded else
                   "no packages uploaded — none were available to download")
        job_service.update_progress(db, job_id, 90, f"Storage sync complete — {summary}")
        job_service.set_completed(db, job_id, {**result, "summary": summary,
                                               "backend": backend})
    except Exception as e:  # noqa: BLE001 — surfaced on the job, not raised at the runner
        logger.exception("epml_sync job %s failed: %s", job_id, e)
        job_service.set_failed(db, job_id, str(e))
