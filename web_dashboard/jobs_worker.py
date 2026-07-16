"""Dedicated background job runner.

A separate process (a second Compose service from the SAME image,
``python -m web_dashboard.jobs_worker``) that executes the long-running Terraform
jobs the API used to run inline on a gunicorn request worker. Gunicorn recycles
its workers (``--timeout``), which killed those in-process jobs mid-apply and
orphaned cloud resources (an ACTIVE EKS control plane with no nodes + a zombie
``running`` job). Running them here makes them survive worker recycling, crashes,
and redeploys.

The ``jobs`` table is the queue: the API creates a ``pending`` job (payload in
``extra_data``); this runner claims it atomically, dispatches to the **same**
service functions the API used to call, and writes progress + Live Output to the
DB — which the WebSocket endpoint polls — so the UI is unchanged.

No AWS/ECS dependency: it runs anywhere Docker runs, sharing the DB + config +
state backend with the app via the same env/secrets.
"""
import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from .database import SessionLocal, Job, init_db
from .logging_context import LOG_FORMAT, correlation, install_log_correlation
from .services import job_service

logger = logging.getLogger(__name__)

# Job types this runner owns. Beyond the Terraform provisions, the long image
# build / export / promote jobs now run here too — they used to be in-app FastAPI
# BackgroundTasks and got killed by gunicorn worker recycling (~5-min --timeout),
# leaving zombie 'running' jobs (the cloud op finished but nothing finalised it).
HANDLED_TYPES = (
    "k8s_provision", "k8s_decommission",
    "k8s_management", "k8s_secret_delivery", "k8s_entitle_agent", "k8s_entitle_register",
    "k8s_tunnel", "k8s_api_tunnel", "k8s_group_binding", "k8s_entra_federation",
    "rancher_node_deploy", "rancher_node_teardown", "rancher_entitle_register",
    "clouddb_provision", "clouddb_decommission", "clouddb_entitle_register",
    "vdesktop_pool_provision", "vdesktop_pool_teardown",
    "packer_aws_build", "packer_azure_build", "packer_gcp_build",
    "aws_export_image", "gcp_export_image", "azure_export_image",
    "image_promote_aws", "image_promote_azure", "image_promote_gcp",
)

POLL_INTERVAL = 2.0  # seconds between queue polls when idle

# While a worker owns a job it actively bumps the job's `updated_at` every
# HEARTBEAT_INTERVAL seconds. This is the liveness signal `reconcile_stale_jobs`
# keys off (its 10-min cutoff = ~10 missed beats): a job whose owner is alive
# keeps a fresh heartbeat even during quiet phases that don't stream output, so a
# starting/restarting SIBLING worker (or the app) can never reconcile-fail it.
# Must stay well under reconcile's stale_after_minutes * 60.
HEARTBEAT_INTERVAL = 60.0


def _claim_one(db: Session) -> Optional[tuple]:
    """Atomically claim the oldest pending handled job and return ``(job_id,
    job_type, meta)`` — or ``None`` if the queue is empty.

    The ``UPDATE ... WHERE status='pending'`` rowcount is the lock: only the caller
    whose UPDATE matched the row owns it, so two runners (or a future scale-out)
    never double-execute. Portable across SQLite (dev) + Postgres (prod) — no
    Postgres-only ``SKIP LOCKED``. Primitives (not the ORM row) are returned so the
    dispatcher can use a fresh session without detached-instance surprises."""
    while True:
        job = (
            db.query(Job)
            .filter(Job.status == "pending", Job.job_type.in_(HANDLED_TYPES))
            .order_by(Job.created_at.asc())
            .first()
        )
        if job is None:
            return None
        now = datetime.utcnow()
        claimed = (
            db.query(Job)
            .filter(Job.id == job.id, Job.status == "pending")
            .update(
                {Job.status: "running", Job.started_at: now, Job.updated_at: now},
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed == 1:
            fresh = db.query(Job).filter(Job.id == job.id).first()
            return (fresh.id, fresh.job_type, fresh.metadata_dict or {})
        # Lost the race to another claimant — try the next candidate.


async def _dispatch(job_id: str, job_type: str, meta: dict) -> None:
    """Run one claimed job by calling the same service fn the API used to schedule.
    The service fns own their own ``set_completed``/``set_failed`` and don't raise;
    the caller still backstops a leaked error so a bad job can't wedge the loop."""
    from .services import k8s_service, cloud_database_service
    db = SessionLocal()
    try:
        if job_type == "k8s_provision":
            await k8s_service.run_provision_apply(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                cloud=meta["cloud"], tf_variables=meta["tf_variables"])
        elif job_type == "k8s_decommission":
            await k8s_service.run_decommission(
                db, cluster_id=meta["cluster_id"], job_id=job_id)
        elif job_type == "k8s_management":
            await k8s_service.run_management_plane(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                mgmt_kind=meta.get("mgmt_kind", "rancher"))
        elif job_type == "k8s_secret_delivery":
            await k8s_service.run_secret_delivery(
                db, cluster_id=meta["cluster_id"], job_id=job_id, kind=meta["kind"])
        elif job_type == "k8s_entitle_agent":
            await k8s_service.run_entitle_agent(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "install"))
        elif job_type == "k8s_entitle_register":
            await k8s_service.run_entitle_register(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "register"))
        elif job_type == "k8s_tunnel":
            await k8s_service.run_tunnel(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "register"),
                jump_group=meta.get("jump_group"), jumpoint_name=meta.get("jumpoint_name"),
                pra_credential_ref=meta.get("pra_credential_ref"),
                vault_inject=meta.get("vault_inject", False),
                vault_account_group_id=meta.get("vault_account_group_id"))
        elif job_type == "k8s_api_tunnel":
            await k8s_service.run_api_tunnel(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "register"),
                jump_group=meta.get("jump_group"), jumpoint_name=meta.get("jumpoint_name"),
                pra_credential_ref=meta.get("pra_credential_ref"))
        elif job_type == "k8s_group_binding":
            await k8s_service.run_group_binding(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "bind"),
                group_id=meta.get("group_id"), role=meta.get("role"))
        elif job_type == "k8s_entra_federation":
            await k8s_service.run_entra_federation(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "enable"))
        elif job_type == "rancher_node_deploy":
            from .services import rancher_node_service
            await rancher_node_service.run_deploy(db, job_id=job_id, meta=meta)
        elif job_type == "rancher_node_teardown":
            from .services import rancher_node_service
            await rancher_node_service.run_teardown(db, job_id=job_id, meta=meta)
        elif job_type == "rancher_entitle_register":
            await k8s_service.run_rancher_entitle_register(
                db, job_id=job_id, action=meta.get("action", "register"))
        elif job_type == "clouddb_provision":
            await cloud_database_service.run_provision_apply(
                db, db_id=meta["db_id"], job_id=job_id,
                engine=meta["engine"], tf_variables=meta["tf_variables"])
        elif job_type == "clouddb_decommission":
            await cloud_database_service.run_decommission(
                db, db_id=meta["db_id"], job_id=job_id)
        elif job_type == "clouddb_entitle_register":
            await cloud_database_service.run_entitle_register(
                db, db_id=meta["db_id"], job_id=job_id,
                action=meta.get("action", "register"))
        elif job_type == "vdesktop_pool_provision":
            # provision_seats / teardown_seats own their own SessionLocal + the
            # job lifecycle (set_running/set_completed) when given a job_id, so they
            # don't take this dispatcher's `db`. Args come from the job metadata the
            # desktops API stored at enqueue time.
            from .services import vdesktop_service
            await vdesktop_service.provision_seats(
                pool_name=meta["pool_name"], job_id=job_id,
                seat_ids=meta["seat_ids"], spec=meta["spec"])
        elif job_type == "vdesktop_pool_teardown":
            from .services import vdesktop_service
            await vdesktop_service.teardown_seats(meta["seat_ids"], job_id=job_id)
        elif job_type in ("packer_aws_build", "packer_azure_build", "packer_gcp_build"):
            # The build runners live in api/packer.py and own their full lifecycle
            # + the nested auto-export; reconstruct the Pydantic request from the
            # metadata the endpoint stored (secret refs only — resolved at launch).
            from .api import packer as _packer
            from .models.packer import (
                AWSPackerBuildRequest, AzurePackerBuildRequest, GCPPackerBuildRequest,
            )
            created_by = meta.get("created_by", "system")
            if job_type == "packer_aws_build":
                await _packer._run_aws_build(job_id, AWSPackerBuildRequest(**meta["req"]), created_by)
            elif job_type == "packer_azure_build":
                await _packer._run_azure_build(job_id, AzurePackerBuildRequest(**meta["req"]), created_by)
            else:
                await _packer._run_gcp_build(job_id, GCPPackerBuildRequest(**meta["req"]), created_by)
        elif job_type == "aws_export_image":
            from .api import packer as _packer
            await _packer.run_export_aws(job_id, meta)
        elif job_type == "gcp_export_image":
            from .api import packer as _packer
            await _packer.run_export_gcp(job_id, meta)
        elif job_type == "azure_export_image":
            from .api import packer as _packer
            await _packer.run_export_azure(job_id, meta)
        elif job_type == "image_promote_aws":
            from .api import images as _images
            await _images._run_aws_automated_promote(
                meta["image_id"], meta.get("target_region") or "", job_id)
        elif job_type == "image_promote_azure":
            from .api import images as _images
            await _images._run_azure_automated_promote(
                meta["image_id"], meta.get("target_resource_group") or "",
                meta.get("target_region") or "", job_id)
        elif job_type == "image_promote_gcp":
            from .api import images as _images
            await _images._run_gcp_automated_promote(
                meta["image_id"], meta.get("target_region") or "", job_id)
        else:  # pragma: no cover — HANDLED_TYPES guards the claim
            logger.warning("job runner: unhandled job_type %s (job %s)", job_type, job_id)
    finally:
        db.close()


async def _heartbeat(job_id: str, interval: float = HEARTBEAT_INTERVAL) -> None:
    """Bump the owned job's ``updated_at`` every ``interval`` seconds so the row's
    heartbeat stays fresh independent of whether the job is streaming output.
    Guarded by ``status='running'`` so it can't resurrect a job the dispatch just
    completed/failed/cancelled. Runs for the lifetime of one ``_dispatch`` and is
    cancelled in its ``finally``. Best-effort — a DB hiccup must not kill the job."""
    while True:
        await asyncio.sleep(interval)
        db = SessionLocal()
        try:
            db.query(Job).filter(Job.id == job_id, Job.status == "running").update(
                {Job.updated_at: datetime.utcnow()}, synchronize_session=False
            )
            db.commit()
        except Exception:
            logger.exception("job runner: heartbeat failed for job %s", job_id)
        finally:
            db.close()


async def _run_loop(poll_interval: float = POLL_INTERVAL) -> None:
    # This runner now owns job execution → reconcile jobs whose heartbeat went
    # stale because a prior runner crashed/restarted, before claiming new work.
    db = SessionLocal()
    try:
        n = job_service.reconcile_stale_jobs(db)
        if n:
            logger.warning("job runner: reconciled %d stale job(s) at startup", n)
    finally:
        db.close()

    logger.info("job runner started; handling: %s", ", ".join(HANDLED_TYPES))
    while True:
        claim = None
        db = SessionLocal()
        try:
            claim = _claim_one(db)
        except Exception:
            logger.exception("job runner: claim failed")
        finally:
            db.close()

        if claim is None:
            await asyncio.sleep(poll_interval)
            continue

        job_id, job_type, meta = claim
        # Tag this job's whole fan-out (dispatch → service calls → WebSocket
        # progress writes) with the job id so its log lines are traceable.
        with correlation(job_id):
            logger.info("job runner: claimed %s job %s", job_type, job_id)
            # Keep this job's heartbeat fresh for its whole run so a sibling
            # worker's startup reconcile can't false-fail it (see _heartbeat).
            hb = asyncio.create_task(_heartbeat(job_id))
            try:
                await _dispatch(job_id, job_type, meta)
            except Exception as exc:
                logger.exception("job runner: dispatch crashed for job %s", job_id)
                # The service fns mark failed themselves; this backstops an error
                # raised around them (e.g. a missing metadata key) so the job is
                # never left stuck 'running'.
                db = SessionLocal()
                try:
                    cur = job_service.get_job(db, job_id)
                    if cur and cur.status == "running":
                        job_service.set_failed(db, job_id, f"job runner error: {exc}")
                except Exception:
                    logger.exception("job runner: could not mark job %s failed", job_id)
                finally:
                    db.close()
            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb


def main() -> None:
    install_log_correlation()
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
    )
    # depends_on only waits for the DB to be healthy, not for the app's migrations;
    # if the runner wins the boot race the new JobLog table must still exist.
    # init_db is advisory-locked (Postgres) + idempotent, so racing the app is safe.
    init_db()
    logger.info("job runner: database ready")
    asyncio.run(_run_loop())


if __name__ == "__main__":
    main()
