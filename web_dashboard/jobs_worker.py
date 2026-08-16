"""Dedicated background job runner.

A separate process (a second Compose service, or its own Container App, from the SAME
image — ``python -m web_dashboard.jobs_worker``) that executes the long-running Terraform
jobs the API used to run inline on a gunicorn request worker. Gunicorn recycles its
workers (``--timeout``), which killed those in-process jobs mid-apply and orphaned cloud
resources (an ACTIVE EKS control plane with no nodes + a zombie ``running`` job). Running
them here makes them survive worker recycling, crashes, and redeploys.

The ``jobs`` table is the queue: the API creates a ``pending`` job (payload in
``extra_data``); this runner claims it atomically, dispatches to the **same** service
functions the API used to call, and writes progress + Live Output to the DB — which the
WebSocket endpoint polls — so the UI is unchanged.

No AWS/ECS dependency: it runs anywhere Docker runs, sharing the DB + config + state
backend with the app via the same env/secrets.

**This runner executes several jobs at once, capped per tier.** It used to run exactly
one, and the answer to "run more" was more ``docker-compose`` replicas. That answer does
not survive contact with a PaaS host: on Azure Container Apps the worker is its own
Container App, so replicas exist — but each replica is another SQLAlchemy connection
pool, and on a small managed Postgres the connection budget, not CPU, is what caps total
concurrency. N slots inside one process share ONE pool; N replicas need N. So the
concurrency moved inside, and ``_run_loop`` became a supervisor that never awaits a job.

The caps are tiered because the jobs are not alike — a 40-minute Packer build and a
2-second expiry sweep were competing for the same single slot, in both directions. See
HEAVY/MEDIUM/LIGHT_TYPES below, and ``services/worker_policy.py`` for the numbers (which
are editable in Settings and re-read on every pass, so tuning needs no restart).
"""
import asyncio
import contextlib
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .database import SessionLocal, Job, init_db, pool_capacity
from .logging_context import LOG_FORMAT, correlation, install_log_correlation
from .services import job_service, worker_policy

logger = logging.getLogger(__name__)

# Job types this runner owns. Beyond the Terraform provisions, the long image
# build / export / promote jobs now run here too — they used to be in-app FastAPI
# BackgroundTasks and got killed by gunicorn worker recycling (~5-min --timeout),
# leaving zombie 'running' jobs (the cloud op finished but nothing finalised it).
HANDLED_TYPES = (
    "k8s_provision", "k8s_decommission",
    "k8s_management", "k8s_secret_delivery", "k8s_entitle_agent", "k8s_entitle_register",
    "k8s_tunnel", "k8s_api_tunnel", "k8s_group_binding", "k8s_impersonator_binding",
    "k8s_entra_federation", "k8s_ps_token",
    "rancher_node_deploy", "rancher_node_teardown", "rancher_entitle_register",
    "portainer_node_deploy", "portainer_node_teardown",
    "clouddb_provision", "clouddb_decommission", "clouddb_entitle_register",
    "cloudfn_deploy", "cloudfn_decommission",
    "ansible_cloud_run", "ansible_local", "epml_sync",
    "vdesktop_pool_provision", "vdesktop_pool_teardown",
    "packer_aws_build", "packer_azure_build", "packer_gcp_build", "packer_oci_build",
    "aws_export_image", "gcp_export_image", "azure_export_image", "oci_export_image",
    "image_promote_aws", "image_promote_azure", "image_promote_gcp", "image_promote_oci",
    "ec2_deploy", "ec2_bulk_deploy", "ec2_destroy", "ec2_create_image", "ami_copy",
    "oci_deploy", "oci_bulk_deploy", "oci_destroy",
    "azure_deploy", "azure_bulk_deploy", "azure_destroy", "azure_create_image",
    "gce_deploy", "gce_bulk_deploy", "gce_capture_image", "gce_destroy",
    "gateway_deploy", "gateway_teardown",
    # Auto-delete timer sweep. The app's loop only ENQUEUES one of these; _claim_one's
    # rowcount lock below is what makes a pass single-flight across two gunicorn workers
    # and three worker replicas. Running it here also gives each pass a job row, Live
    # Output and cancel — see services/expiry_reaper.
    "expiry_sweep",
)

# ── Concurrency tiers ─────────────────────────────────────────────────────────
#
# Every HANDLED_TYPES entry is in exactly ONE tuple below; tests/test_worker_tiers.py
# pins that statically, both directions, the same way test_worker_dispatch pins the
# dispatch table. An untiered type raises KeyError in the supervisor, which would take
# the loop down for EVERY job — hence a static test rather than a runtime default.
#
# The tiers are about which shared resource a job consumes, not how long it takes.

# HEAVY — a long LOCAL subprocess whose output is streamed line by line. Each holds a
# terraform/packer/docker process, at least one executor thread, and writes a JobLog row
# PER OUTPUT LINE through api.websocket.broadcast_progress. That per-line write is the
# real constraint: it is an INSERT plus a commit, so two concurrent streams are enough to
# saturate a small managed Postgres's IOPS. This tier is the reason the worker was serial.
HEAVY_TYPES = (
    "k8s_provision", "k8s_decommission",              # k8s_service, terraform apply/destroy
    "clouddb_provision", "clouddb_decommission",      # cloud_database_service, same
    "packer_aws_build", "packer_azure_build",         # packer_service._stream_command
    "packer_gcp_build", "packer_oci_build",
    "ansible_local",                                  # ansible_local_service, `docker run`
)

# MEDIUM — cloud-SDK orchestration that ALSO shells out BRIEFLY: a terraform init+apply in
# a TemporaryDirectory for a PRA jump / Entitle integration / Password Safe resource, or
# kubectl+helm when k8s_runner is "local" — which is the DEFAULT (k8s_runner_service.mode).
# Short, but they are real processes sharing one plugin cache (they serialize on
# terraform.plugin_cache_lock), and `helm upgrade --wait` holds a thread for minutes.
MEDIUM_TYPES = (
    # kubectl / helm in-process
    "k8s_management", "k8s_secret_delivery", "k8s_entitle_agent",
    "k8s_group_binding", "k8s_impersonator_binding", "k8s_entra_federation",
    # the above plus a short terraform (terraform_pra_service / entitle_registration_service
    # / ps_resource_service — k8s_ps_token applies RBAC with kubectl AND creates up to two
    # Password Safe managed systems, each a terraform apply in a tempdir)
    "k8s_tunnel", "k8s_api_tunnel", "k8s_entitle_register", "k8s_ps_token",
    "rancher_entitle_register", "clouddb_entitle_register",
    # cloud SDK + HTTP readiness poll + an OPT-IN short terraform for the PRA Web Jump
    "rancher_node_deploy", "rancher_node_teardown",
    "portainer_node_deploy", "portainer_node_teardown",
    # cloud SDK + a short terraform for the per-VM PRA Shell Jump. These do NOT stream and
    # run no long subprocess — worth stating because their names suggest otherwise.
    "ec2_deploy", "ec2_bulk_deploy", "ec2_destroy",
    "azure_deploy", "azure_bulk_deploy", "azure_destroy",
    "gce_deploy", "gce_bulk_deploy", "gce_destroy",
    "oci_deploy", "oci_bulk_deploy", "oci_destroy",
    "vdesktop_pool_provision", "vdesktop_pool_teardown",
    # Polls a CLOUD runner, but its cloud="local" branch shells out to `docker run` and
    # reads the container's output line by line. MEDIUM is the tier that admits a local
    # process without granting it light-tier concurrency.
    "ansible_cloud_run",
)

# LIGHT — start a cloud operation, then sleep and check its API. No local process, no
# shared filesystem, no streamed output. Minutes to two hours of almost pure waiting —
# these are the jobs that must never queue behind a Packer build, and the whole reason
# this tiering exists.
LIGHT_TYPES = (
    "ec2_create_image", "ami_copy",                    # boto3 poll loops, no terraform
    "azure_create_image", "gce_capture_image",
    "aws_export_image", "azure_export_image",          # up to a 7200s timeout apiece
    "gcp_export_image", "oci_export_image",
    "image_promote_aws", "image_promote_azure",
    "image_promote_gcp", "image_promote_oci",
    "gateway_deploy", "gateway_teardown",              # pure cloud SDK (jumpoint_host_service)
    "epml_sync",                                       # HTTP download + storage upload
    "expiry_sweep",                                    # pure DB, sub-second
)

# At most ONE in flight per type, whatever the tier allows. These write DEPLOYMENT-global
# state, not just per-job state: the rancher/portainer node jobs rewrite global
# config_service keys (rancher_node_service), and config_service is process-wide.
# api/expiry.py's force_sweep also deliberately allows a second expiry_sweep row to be
# queued. A tier cap does not make any of that safe — this does.
#
# epml_sync is here for a different reason: MEMORY. It is the one light-tier job that holds
# a large payload in RAM — epml_service.download_package returns the whole agent package as
# bytes, and every asset-storage backend's upload takes bytes, so there is nowhere to
# stream it to without reworking a shared upload path that playbooks and scripts also use.
# One at a time bounds the worker's peak at a single package instead of multiplying it by
# the light cap, which matters on a container with a hard memory limit: an OOM there kills
# the container, and with it every OTHER job in flight.
SINGLETON_TYPES = frozenset((
    "rancher_node_deploy", "rancher_node_teardown",
    "portainer_node_deploy", "portainer_node_teardown",
    "expiry_sweep",
    "epml_sync",
))

_TIER_TYPES = (("heavy", HEAVY_TYPES), ("medium", MEDIUM_TYPES), ("light", LIGHT_TYPES))
_TIER_OF = {t: tier for tier, types in _TIER_TYPES for t in types}

POLL_INTERVAL = 2.0  # seconds between queue polls when idle

# While a worker owns a job it actively bumps the job's `updated_at` every
# HEARTBEAT_INTERVAL seconds. This is the liveness signal `reconcile_stale_jobs`
# keys off (its 10-min cutoff = ~10 missed beats): a job whose owner is alive
# keeps a fresh heartbeat even during quiet phases that don't stream output, so a
# starting/restarting SIBLING worker (or the app) can never reconcile-fail it.
# Must stay well under STALE_AFTER_MINUTES * 60.
HEARTBEAT_INTERVAL = 60.0

# The staleness cutoff, in ONE place. Passed to reconcile_stale_jobs and used by the
# shutdown drain to backdate abandoned heartbeats past it. Two different numbers would
# mean either a zombie that still looks alive, or a row failed out from under a live
# sibling worker.
STALE_AFTER_MINUTES = 10

# What one in-flight job costs the connection pool, and what the process needs on top.
# A job is not one connection: _dispatch holds a session for the whole job and several
# services open their own. The reserve covers the claim query, a heartbeat beat, the
# notification drain and the startup reconcile.
_SESSIONS_PER_JOB = 2
_POOL_RESERVED = 4

# Last limits we logged/published, so a re-read every 2s doesn't mean a log line every 2s.
_last_published: Optional[tuple] = None


def _claim_one(db: Session, allowed: tuple = HANDLED_TYPES,
               max_attempts: int = 10) -> Optional[tuple]:
    """Atomically claim the oldest pending handled job and return ``(job_id,
    job_type, meta)`` — or ``None`` if nothing claimable is queued.

    The ``UPDATE ... WHERE status='pending'`` rowcount is the lock: only the caller
    whose UPDATE matched the row owns it, so two runners — or two TASKS inside one
    runner — never double-execute. Portable across SQLite (dev) + Postgres (prod) — no
    Postgres-only ``SKIP LOCKED``. Primitives (not the ORM row) are returned so the
    dispatcher can use a fresh session without detached-instance surprises.

    ``allowed`` is the set of job types the caller has capacity for right now; the
    tiered supervisor passes a subset. It goes INTO THE QUERY and must stay there:
    fetching the oldest pending row of any type and rejecting it in Python would
    re-SELECT that same unclaimable row on every attempt, forever. An empty ``allowed``
    means "no room anywhere", which is a fast return rather than a query.

    ``max_attempts`` bounds the lost-race retry. Losing the race cannot normally repeat
    (a row we lost is no longer ``pending``, so the next SELECT skips it), but that
    argument rests on nothing ever writing ``pending`` back onto a running row — an
    invariant this function cannot enforce. A bound turns a hypothetical hot loop into a
    logged giveup that the next poll tick retries.

    NOTE the ordering change a narrowed ``allowed`` introduces: a NEWER light job
    overtakes an OLDER pending heavy one whose tier is full. That is the point — an
    image-export poll must not wait 40 minutes behind a Packer build — so ``created_at``
    order is now only guaranteed WITHIN a tier. A heavy job cannot be starved by it:
    heavy capacity is only ever consumed by heavy jobs.
    """
    if not allowed:
        return None
    for _ in range(max_attempts):
        job = (
            db.query(Job)
            .filter(Job.status == "pending", Job.job_type.in_(allowed))
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
    logger.warning("job runner: gave up claiming after %d lost races", max_attempts)
    return None


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
        elif job_type == "k8s_impersonator_binding":
            await k8s_service.run_impersonator_binding(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "apply"),
                group_id=meta.get("group_id"))
        elif job_type == "k8s_entra_federation":
            await k8s_service.run_entra_federation(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "enable"))
        elif job_type == "k8s_ps_token":
            from .services import ps_k8s_token_service
            await ps_k8s_token_service.run(
                db, cluster_id=meta["cluster_id"], job_id=job_id,
                action=meta.get("action", "register"),
                **{k: v for k, v in meta.items()
                   if k in ("mode", "ttl_seconds", "namespace", "service_account",
                            "cluster_name", "resource_group", "location",
                            "functional_account", "change_on_register", "mirror_to_pra")
                   and v is not None})
        elif job_type == "rancher_node_deploy":
            from .services import rancher_node_service
            await rancher_node_service.run_deploy(db, job_id=job_id, meta=meta)
        elif job_type == "rancher_node_teardown":
            from .services import rancher_node_service
            await rancher_node_service.run_teardown(db, job_id=job_id, meta=meta)
        elif job_type == "rancher_entitle_register":
            await k8s_service.run_rancher_entitle_register(
                db, job_id=job_id, action=meta.get("action", "register"))
        elif job_type == "portainer_node_deploy":
            from .services import portainer_node_service
            await portainer_node_service.run_deploy(db, job_id=job_id, meta=meta)
        elif job_type == "portainer_node_teardown":
            from .services import portainer_node_service
            await portainer_node_service.run_teardown(db, job_id=job_id, meta=meta)
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
        elif job_type == "cloudfn_deploy":
            from .services import cloud_function_service
            await cloud_function_service.run_deploy_apply(
                db, fn_id=meta["fn_id"], job_id=job_id,
                tf_variables=meta["tf_variables"])
        elif job_type == "cloudfn_decommission":
            from .services import cloud_function_service
            await cloud_function_service.run_decommission(
                db, fn_id=meta["fn_id"], job_id=job_id)
        elif job_type == "ansible_cloud_run":
            # Config-Management localhost Ansible run against a Kubernetes cluster or
            # cloud database — always executes on a transient in-cloud runner.
            from .services import ansible_cloud_run_service
            await ansible_cloud_run_service.run(db, job_id=job_id, meta=meta)
        elif job_type == "epml_sync":
            # EPM-L: download the agent packages from BeyondTrust and upload them to
            # asset storage. A job because the packages are large and BeyondTrust's
            # download links expire ~30 min after listing.
            from .services import epml_sync_service
            await epml_sync_service.run(db, job_id=job_id, meta=meta)
        elif job_type == "ansible_local":
            # Config-Management SSH/WinRM run against a VM or hypervisor group — the
            # counterpart of ansible_cloud_run above. The service owns its own
            # SessionLocal and the job lifecycle, and reconstructs the run's arguments
            # from the metadata the endpoint persisted (services/ansible_run_meta).
            from .services import ansible_local_run_service
            await ansible_local_run_service.run(db, job_id=job_id, meta=meta)
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
        elif job_type in ("packer_aws_build", "packer_azure_build", "packer_gcp_build",
                          "packer_oci_build"):
            # Packer image build (+ the nested auto-export). The service rebuilds the
            # request from the metadata the endpoint stored — secret refs only,
            # resolved at build launch.
            from .services import packer_build_service
            await packer_build_service.run_build(job_id, job_type, meta)
        elif job_type in ("aws_export_image", "gcp_export_image", "azure_export_image",
                          "oci_export_image"):
            from .services import packer_build_service
            await packer_build_service.run_export(job_id, job_type, meta)
        elif job_type in ("image_promote_aws", "image_promote_azure",
                          "image_promote_gcp", "image_promote_oci"):
            # SDK-driven automated promote to a target cloud.
            from .services import image_promote_service
            await image_promote_service.run(job_id, job_type, meta)
        elif job_type in ("ec2_deploy", "ec2_bulk_deploy", "ec2_destroy",
                          "ec2_create_image", "ami_copy"):
            # EC2 lifecycle. The bulk children are created `queued`, so the claim
            # query above cannot pick them up alongside their ec2_bulk_deploy parent.
            from .services import aws_vm_service
            await aws_vm_service.run(job_id, job_type, meta)
        elif job_type in ("oci_deploy", "oci_bulk_deploy", "oci_destroy"):
            # OCI compute lifecycle. Same parent/child split as EC2: a count > 1 deploy
            # creates `queued` children driven by the oci_bulk_deploy parent.
            from .services import oci_vm_service
            await oci_vm_service.run(job_id, job_type, meta)
        elif job_type in ("azure_deploy", "azure_bulk_deploy", "azure_destroy",
                          "azure_create_image"):
            # Azure VM lifecycle. Same parent/child split as EC2: the bulk children are
            # created `queued` so only the azure_bulk_deploy parent is claimable.
            from .services import azure_vm_service
            await azure_vm_service.run(job_id, job_type, meta)
        elif job_type in ("gce_deploy", "gce_bulk_deploy", "gce_capture_image",
                          "gce_destroy"):
            # GCE lifecycle. Same parent/child split as EC2, and the gce_bulk_deploy
            # parent additionally acquires ONE shared Jumpoint for the whole batch
            # instead of a paired bt-jumpoint-<vm> per instance.
            from .services import gcp_vm_service
            await gcp_vm_service.run(job_id, job_type, meta)
        elif job_type in ("gateway_deploy", "gateway_teardown"):
            # An operator-requested BeyondTrust Gateway host. Unlike the auto-ensured
            # one, nothing reference-counts it — it lives until someone removes it.
            from .services import gateway_service
            await gateway_service.run(db, job_id=job_id, meta=meta)
        elif job_type == "expiry_sweep":
            # One auto-delete pass. The app enqueues these on a timer; winning the claim
            # above is what guarantees exactly one runs, however many app workers or
            # worker replicas are up.
            from .services import expiry_reaper
            await expiry_reaper.run(db, job_id=job_id, meta=meta)
        else:  # pragma: no cover — HANDLED_TYPES guards the claim
            logger.warning("job runner: unhandled job_type %s (job %s)", job_type, job_id)
    finally:
        db.close()


def _beat_once(job_id: str) -> None:
    """One heartbeat write. Synchronous on purpose — the caller hands it to a thread."""
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.id == job_id, Job.status == "running").update(
            {Job.updated_at: datetime.utcnow()}, synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


async def _heartbeat(job_id: str, interval: float = HEARTBEAT_INTERVAL) -> None:
    """Bump the owned job's ``updated_at`` every ``interval`` seconds so the row's
    heartbeat stays fresh independent of whether the job is streaming output.
    Guarded by ``status='running'`` so it can't resurrect a job the dispatch just
    completed/failed/cancelled. Runs for the lifetime of one ``_run_job`` and is
    cancelled in its ``finally``. Best-effort — a DB hiccup must not kill the job.

    The write goes through ``to_thread`` because this loop shares its event loop with
    every other in-flight job: a slow round trip here would delay their progress writes
    and their ``asyncio.sleep`` timers, and a heartbeat is precisely the thing that must
    not be late (10 missed beats and a sibling's reconcile fails a live job)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_beat_once, job_id)
        except Exception:
            logger.exception("job runner: heartbeat failed for job %s", job_id)


def _fail_backstop(job_id: str, exc: BaseException) -> None:
    """The service fns mark themselves failed; this backstops an error raised AROUND
    them (a missing metadata key, an import error) so a bad job is never left `running`.
    Lives here rather than in the supervisor so the per-job task owns it — an exception
    escaping a Task surfaces only as an "exception was never retrieved" warning on GC,
    and the row would sit `running` until the reconciler failed it ten minutes later."""
    db = SessionLocal()
    try:
        cur = job_service.get_job(db, job_id)
        if cur and cur.status == "running":
            job_service.set_failed(db, job_id, f"job runner error: {exc}")
    except Exception:
        logger.exception("job runner: could not mark job %s failed", job_id)
    finally:
        db.close()


async def _run_job(job_id: str, job_type: str, meta: dict) -> None:
    """One claimed job, start to finish, in its own task.

    ``correlation`` is entered HERE, not in the supervisor. contextvars are SNAPSHOTTED
    per Task at ``create_task`` time, so a ``with correlation(job_id)`` in the loop body
    would tag whichever job the loop happened to be starting at that instant and leave
    every concurrent job's log lines carrying the same wrong id. Inside the task the
    value is private to that task — which is also why the heartbeat task created below
    inherits the right id for free.

    Nothing may propagate out of here: one job's crash can take down neither the loop nor
    a sibling.
    """
    with correlation(job_id):
        # Keep this job's heartbeat fresh for its whole run so a sibling worker's
        # startup reconcile can't false-fail it (see _heartbeat).
        hb = asyncio.create_task(_heartbeat(job_id), name=f"hb:{job_id}")
        try:
            await _dispatch(job_id, job_type, meta)
        except asyncio.CancelledError:
            # Shutdown drain. Deliberately NOT marked failed here: _drain backdates the
            # heartbeat so the next process's startup reconcile owns the row, and only
            # reconcile_stale_jobs also flips the owning cluster/database row.
            # (CancelledError is a BaseException on 3.8+, so `except Exception` below
            # already misses it — this clause is here so nobody "fixes" that into
            # `except BaseException`.)
            raise
        except Exception as exc:
            logger.exception("job runner: dispatch crashed for job %s", job_id)
            await asyncio.to_thread(_fail_backstop, job_id, exc)
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb


def _reconcile_at_startup() -> None:
    """Fail jobs whose heartbeat went stale because a prior runner crashed or was
    restarted, before claiming new work. Extracted from the loop so the loop can be
    tested with no database. ``STALE_AFTER_MINUTES`` is passed explicitly so it cannot
    drift from the value the shutdown drain backdates past."""
    db = SessionLocal()
    try:
        n = job_service.reconcile_stale_jobs(db, stale_after_minutes=STALE_AFTER_MINUTES)
        if n:
            logger.warning("job runner: reconciled %d stale job(s) at startup", n)
    finally:
        db.close()


def _limits() -> dict:
    """Per-tier in-flight caps, clamped to what this process's DB pool can serve.

    Read on EVERY supervisor pass, not once at startup: the numbers live in
    ``worker_policy`` → ``config_service``, whose 5s cache makes the call essentially
    free and lets a Settings change on the app take effect here within ~5s without a
    restart or a redeploy. Same pattern as ``main._expiry_sweeper_loop`` re-reading its
    interval every iteration.

    The clamp exists because a job is not one connection (see _SESSIONS_PER_JOB).
    Clamping rather than refusing to boot is deliberate: a pool-exhausted worker fails
    jobs with opaque ``QueuePool limit ... timeout`` errors, which is strictly worse than
    running fewer at once and saying so — in the log AND in the Settings readout, since
    "I raised the cap and nothing changed" is otherwise undiagnosable from the UI.
    """
    caps = worker_policy.caps()
    total = caps["total"]
    capacity = pool_capacity()          # 0 = unbounded (SQLite's NullPool)
    reason = ""
    if capacity:
        affordable = max(1, (capacity - _POOL_RESERVED) // _SESSIONS_PER_JOB)
        if total > affordable:
            reason = (f"DB pool of {capacity} serves {affordable} concurrent job(s); "
                      f"raise DB_POOL_SIZE / DB_MAX_OVERFLOW to use {total}")
            total = affordable
    return {"heavy": caps["heavy"], "medium": caps["medium"], "light": caps["light"],
            "total": total, "configured": caps, "clamp_reason": reason,
            "capacity": capacity}


def _allowed_types(limits: dict, running: dict) -> tuple:
    """The job types there is room for right now, for ``_claim_one``'s IN clause.

    Returning an ALLOWLIST rather than post-filtering is what keeps the claim one round
    trip and ``_claim_one``'s retry finite (see its docstring). ``running`` maps task →
    ``(tier, job_type)`` so the singleton check costs nothing extra.
    """
    if len(running) >= limits["total"]:
        return ()
    in_flight = {"heavy": 0, "medium": 0, "light": 0}
    live_types = set()
    for tier, job_type in running.values():
        in_flight[tier] += 1
        live_types.add(job_type)
    out = []
    for tier, types in _TIER_TYPES:
        if in_flight[tier] < limits[tier]:
            out.extend(t for t in types
                       if not (t in SINGLETON_TYPES and t in live_types))
    return tuple(out)


async def _idle(running: dict, poll_interval: float, shutdown: asyncio.Event) -> None:
    """Wait until there is plausibly something to do: the next poll tick, a job finishing
    (which frees a tier slot worth refilling at once rather than after a full poll
    interval), or shutdown. ``asyncio.wait`` neither cancels the tasks nor consumes their
    results, so the done-callback bookkeeping is unaffected."""
    if not running:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
        return
    await asyncio.wait(list(running), timeout=poll_interval,
                       return_when=asyncio.FIRST_COMPLETED)


def _publish_if_changed(limits: dict, in_flight: int) -> None:
    """Log and publish the resolved limits, but only when they actually change — this is
    called on every pass and a re-read every 2s must not mean a log line every 2s."""
    global _last_published
    threads = _executor_size(limits)
    sig = (limits["heavy"], limits["medium"], limits["light"], limits["total"],
           limits["clamp_reason"], threads)
    if sig == _last_published:
        return
    _last_published = sig
    if limits["clamp_reason"]:
        logger.warning("job runner: concurrency clamped — %s", limits["clamp_reason"])
    logger.info("job runner: caps heavy=%d medium=%d light=%d total=%d (threads=%d)",
                limits["heavy"], limits["medium"], limits["light"], limits["total"],
                threads)
    worker_policy.publish_runtime_status(
        configured=limits["configured"],
        effective={"heavy": limits["heavy"], "medium": limits["medium"],
                   "light": limits["light"], "total": limits["total"]},
        clamp_reason=limits["clamp_reason"], executor_threads_used=threads,
        pool_capacity=limits["capacity"], in_flight=in_flight)


async def _run_loop(poll_interval: float = POLL_INTERVAL,
                    shutdown: Optional[asyncio.Event] = None) -> None:
    """Claim work and run it concurrently, up to a per-tier cap.

    This is a supervisor, not a worker: it never awaits a job. The previous shape —
    claim, then wait for the dispatch to return — is what serialized everything, so a
    two-second expiry_sweep queued behind a forty-minute Packer build and vice versa.

    ``shutdown`` is created by the caller (``_main``) rather than held as a module global:
    an ``asyncio.Event`` binds to whichever loop first awaits it, so a module-level one
    works exactly once per process. That is true of production — ``main()`` calls
    ``asyncio.run`` once — but it makes the loop untestable and would break the moment
    anything wanted to restart it, which is a poor trade for a global.
    """
    if shutdown is None:
        shutdown = asyncio.Event()
    _reconcile_at_startup()
    running: dict = {}                  # task -> (tier, job_type)
    logger.info("job runner started; handling %d job types", len(HANDLED_TYPES))

    while not shutdown.is_set():
        limits = _limits()
        _publish_if_changed(limits, len(running))
        allowed = _allowed_types(limits, running)

        claim = None
        if allowed:
            db = SessionLocal()
            try:
                claim = _claim_one(db, allowed)
            except Exception:
                logger.exception("job runner: claim failed")
            finally:
                db.close()

        if claim is None:
            await _idle(running, poll_interval, shutdown)
            continue

        job_id, job_type, meta = claim
        tier = _TIER_OF[job_type]
        task = asyncio.create_task(_run_job(job_id, job_type, meta), name=f"job:{job_id}")
        # Registered BEFORE the done-callback so the callback can never fire against a
        # dict the task was never in.
        running[task] = (tier, job_type)
        task.add_done_callback(running.pop)
        logger.info("job runner: claimed %s job %s (tier=%s, in flight=%d)",
                    job_type, job_id, tier, len(running))
        # No sleep: loop straight back so ONE pass fills every free slot. That is what
        # makes a burst of queued exports start together instead of one every 2s.

    await _drain(running, float(worker_policy.drain_timeout_s()))


def _expire_heartbeats(job_ids: list) -> None:
    """Backdate abandoned jobs' ``updated_at`` past reconcile's cutoff. Guarded on
    ``status='running'`` so it can never touch a job that finished cleanly, or one a
    SIBLING worker owns."""
    if not job_ids:
        return
    stale = datetime.utcnow() - timedelta(minutes=STALE_AFTER_MINUTES + 1)
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.id.in_(job_ids), Job.status == "running").update(
            {Job.updated_at: stale}, synchronize_session=False)
        db.commit()
    except Exception:
        logger.exception("job runner: could not expire heartbeats on shutdown")
    finally:
        db.close()


async def _drain(running: dict, timeout: float) -> None:
    """Stop owning work as cleanly as the platform's grace period allows.

    Claiming has already stopped (the supervisor's loop condition). Give what is in
    flight ``timeout`` seconds — enough for an expiry_sweep, a kubectl apply or an
    Entitle terraform, nowhere near enough for a two-hour image export. That asymmetry is
    the honest reason this is not "wait for the jobs".

    Anything still running is deliberately NOT requeued: the cloud side effect is already
    under way and re-running would launch it twice — the same reasoning that makes bulk
    children ``queued``. Instead the heartbeat is backdated past reconcile's cutoff, so
    the reconcile the NEXT process runs at startup — seconds later — fails the row
    immediately, with the resource-row flip, rather than it looking alive for ten more
    minutes. Backdated BEFORE the cancel as well as after: if the platform's SIGKILL
    lands during the cancel window we still want the first UPDATE committed. Both are
    idempotent.
    """
    if not running:
        return
    ids = [t.get_name().removeprefix("job:") for t in running]
    logger.warning("job runner: shutting down with %d job(s) in flight, %.0fs budget",
                   len(ids), timeout)
    await asyncio.to_thread(_expire_heartbeats, ids)

    pending = set(running)
    if timeout > 0:
        _, pending = await asyncio.wait(list(running), timeout=timeout)
        if not pending:
            logger.info("job runner: all in-flight jobs finished before shutdown")
            return
        ids = [t.get_name().removeprefix("job:") for t in pending]
    for t in pending:
        t.cancel()
    # Cancelling lets each job's `finally` run (which stops its heartbeat), so the second
    # backdate below cannot be undone by a beat firing in the gap.
    with contextlib.suppress(Exception):
        await asyncio.wait(pending, timeout=5)
    await asyncio.to_thread(_expire_heartbeats, ids)
    logger.warning("job runner: abandoned %d job(s) to the next startup reconcile: %s",
                   len(ids), ", ".join(ids))


def _executor_size(limits: dict) -> int:
    """Threads for the worker's DEFAULT executor.

    Every blocking cloud SDK call in this codebase goes through ``asyncio.to_thread``,
    which uses the loop's default ThreadPoolExecutor — sized ``min(32, cpu_count + 4)``.
    ``os.cpu_count()`` is not cgroup-aware on 3.12, so in a container that is computed
    from the HOST's CPU count: a non-deterministic ceiling that has nothing to do with
    the CPU this process was actually given.

    It has to be explicit here because several cloud pollers are SYNCHRONOUS
    ``while True: ... time.sleep(n)`` loops wrapped in ``to_thread`` (an image export
    runs to a 7200s timeout), so they OCCUPY a thread for the whole wait rather than per
    call. Exhaust the pool and the next ``to_thread`` — a terraform init, a helm call —
    queues behind a two-hour sleep: the job sits ``running`` at 0% while its heartbeat,
    which runs on the event loop, keeps beating. A hang that reports healthy is the worst
    failure mode available, so the pool is sized generously; threads parked in ``sleep``
    cost little.

    Sized only in the worker: the app process's default executor serves request-path work
    with completely different characteristics.
    """
    explicit = worker_policy.executor_threads()
    if explicit > 0:
        return explicit
    derived = (4 * limits["heavy"] + 3 * limits["medium"] + 2 * limits["light"] + 8)
    # heavy  x4: init (which BLOCKS on the plugin-cache flock), apply, `output -json`,
    #            and the destroy-retry path.
    # medium x3: kubectl -> helm repo add/update -> helm upgrade, or init -> apply ->
    #            output; sequential per job but overlapping across jobs.
    # light  x2: the parked synchronous poller, plus one for whatever else it calls.
    # +8:        heartbeats, cancel_checks, config reads, the notification drain.
    return min(worker_policy.EXECUTOR_THREADS_CEILING, max(16, derived))


def _install_signal_handlers(loop, shutdown: asyncio.Event) -> None:
    """Set the shutdown event on SIGTERM/SIGINT.

    The worker had no signal handling at all, and a PaaS host sends SIGTERM on EVERY
    revision change and scale-in. With the default disposition the process dies instantly
    and every in-flight job sits ``running`` until the 10-minute reconciler — so a
    redeploy silently broke whatever was mid-flight and the operator saw nothing for ten
    minutes. With several jobs in flight that multiplies.

    Only the FIRST signal drains: the handler is not re-armed, so a second signal takes
    the default disposition and an operator can always force the issue.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except (NotImplementedError, RuntimeError, AttributeError):
            # Windows dev host: add_signal_handler is not implemented there. call_soon_
            # threadsafe because signal.signal's handler runs in the main thread's signal
            # context, not on the loop — setting an Event from there is not safe.
            with contextlib.suppress(ValueError, OSError, AttributeError):
                signal.signal(
                    sig, lambda *_: loop.call_soon_threadsafe(shutdown.set))


async def _main() -> None:
    """Run the job runner and the notification drain concurrently.

    The drain is a *peer* of the job runner, not a job type and not a step inside
    ``_run_loop``: putting delivery in the queue would park an alert behind a 30-minute
    terraform apply — and park the apply behind a stalled webhook POST. It is started
    unconditionally and no-ops while notifications are off, so flipping the Settings
    toggle needs no restart. Its failures are its own (it owns a try/except per pass) and
    it can never take the job runner down with it.

    The runner itself now runs SEVERAL jobs at once, capped per tier — see the tier
    tuples above and ``services/worker_policy``. ``docker-compose`` replicas still
    multiply those caps (the claim is atomic), so replica-based scale-out is unchanged;
    the caps exist because replicas are not a free, or even an available, knob everywhere.
    """
    from .services import cloud_executor, notification_service

    loop = asyncio.get_running_loop()
    threads = _executor_size(_limits())
    executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="jobs-worker")
    loop.set_default_executor(executor)
    logger.info("job runner: default executor sized to %d threads "
                "(the stdlib default here would be %d)",
                threads, min(32, (os.cpu_count() or 1) + 4))
    # Blocking cloud SDK calls have their own per-provider pools and their own deadline.
    # This process must say so BEFORE any job runs: the request-path default is 60s, which
    # is right for a dashboard tile and would kill a two-hour image export.
    cloud_executor.use_worker_defaults()
    # Owned here, not at module level: an asyncio.Event binds to the loop that first
    # awaits it (see _run_loop).
    shutdown = asyncio.Event()
    _install_signal_handlers(loop, shutdown)

    drain = asyncio.create_task(notification_service.drain_loop(), name="notify-drain")
    try:
        await _run_loop(shutdown=shutdown)   # returns once shutdown is set and jobs drain
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        # Non-blocking: a poller's thread still parked in time.sleep() would otherwise
        # hold shutdown open for its full remaining wait.
        executor.shutdown(wait=False, cancel_futures=True)


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
    asyncio.run(_main())


if __name__ == "__main__":
    main()
