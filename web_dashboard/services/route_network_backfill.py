"""Record the network of cloud VMs deployed before their launchers recorded it.

A Config-Management route may be narrowed to one network — an AWS VPC, an Azure VNet or a
GCP network (config_mgmt_route_service.network_of). New deploys record it as they launch;
this asks the cloud once for every live deploy that has none, so those VMs can match a
network-narrowed route too. Until it has run they simply do not: a VM with no recorded
network falls back to a broader route or the remote worker, never to a guessed network.

Dispatched by ``jobs_worker`` as ``route_network_backfill``. Queued from the Config
Routes page (**Look up networks**), and once automatically when a network-narrowed route
is first saved while such VMs exist (:func:`enqueue_if_needed`).

Writes only empty keys, so a re-run is a no-op. A VM the cloud no longer has is skipped
and logged, never marked destroyed: deciding that belongs to the per-cloud reconcile.
"""
import logging

logger = logging.getLogger(__name__)

JOB_TYPE = "route_network_backfill"
_DEPLOYS = ("ec2_deploy", "azure_deploy", "gce_deploy")


def _candidates(db) -> list:
    """Live AWS, Azure and GCP deploy jobs with a private address and no recorded network."""
    from ..database import Job
    from . import config_mgmt_route_service as cmr
    out = []
    for job in (db.query(Job)
                .filter(Job.job_type.in_(_DEPLOYS), Job.status == "completed").all()):
        meta = job.metadata_dict
        if meta.get("destroyed") or not str(meta.get("private_ip") or "").strip():
            continue
        scope, _region = cmr.scope_of_deploy(job.job_type, meta)
        if not cmr.network_of(scope, meta):
            out.append(job)
    return out


def missing_count(db) -> int:
    return len(_candidates(db))


def _busy(db) -> bool:
    from ..database import Job
    return db.query(Job.id).filter(Job.job_type == JOB_TYPE,
                                   Job.status.in_(("pending", "running"))).first() is not None


def enqueue(db, *, created_by: str) -> str:
    """Queue a pass and return its job id, or "" if one is already queued or running."""
    from . import job_service
    if _busy(db):
        return ""
    job = job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                 metadata={"description": "Look up the networks of cloud VMs"})
    return job.id


def enqueue_if_needed(db, *, created_by: str) -> str:
    """After a network-narrowed route is saved: queue a pass if any VM lacks a network, so
    the operator is not left with VMs that silently fall through."""
    return enqueue(db, created_by=created_by) if missing_count(db) else ""


async def _lookup(job) -> tuple:
    """``(keys to merge, why skipped)`` — exactly one of them is empty."""
    from . import aws_service, azure_service, config_service, gcp_service
    meta = job.metadata_dict
    if job.job_type == "ec2_deploy":
        region = str(meta.get("region") or "").strip()
        iid = str(meta.get("instance_id") or job.cloud_resource_id or "").strip()
        if not (region and iid):
            return {}, "no region or instance id recorded"
        found = await aws_service.instance_network(region, iid)
        if found is None:
            return {}, f"instance {iid} no longer exists"
        return {k: v for k, v in found.items() if v and not meta.get(k)}, ""
    if job.job_type == "azure_deploy":
        rg = str(meta.get("resource_group") or "").strip()
        nic = str(meta.get("nic_name") or "").strip()
        if not (rg and nic):
            return {}, "no resource group or NIC recorded"
        vnet = await azure_service.nic_vnet(rg, nic)
        if vnet is None:
            return {}, f"NIC {nic} no longer exists"
        return ({"vnet_id": vnet} if vnet else {}), ("" if vnet else "its NIC has no subnet")
    project = (config_service.get("gcp_project_id") or "").strip()
    zone = str(meta.get("zone") or "").strip()
    name = str(meta.get("instance_name") or "").strip()
    if not (project and zone and name):
        return {}, "no project, zone or instance name recorded"
    network = await gcp_service.instance_network(project, zone, name)
    if network is None:
        return {}, f"instance {name} no longer exists"
    return ({"network": network} if network else {}), ("" if network else "it has no NIC")


async def run(db, *, job_id: str, meta: dict) -> None:
    from . import job_service
    job_service.set_running(db, job_id)
    filled, skipped, failed = [], [], []
    todo = _candidates(db)
    for n, job in enumerate(todo, 1):
        job_service.update_progress(db, job_id, int(100 * (n - 1) / max(1, len(todo))),
                                    f"Looking up {n} of {len(todo)}")
        try:
            keys, why = await _lookup(job)
        except Exception as exc:  # noqa: BLE001 — one cloud's failure must not stop the rest
            logger.warning("route network backfill: %s %s: %s", job.job_type, job.id, exc)
            failed.append({"job_id": job.id, "error": str(exc)[:300]})
            continue
        if not keys:
            logger.info("route network backfill: %s skipped: %s", job.id, why)
            skipped.append({"job_id": job.id, "reason": why})
            continue
        current = job.metadata_dict
        current.update({k: v for k, v in keys.items() if not current.get(k)})
        job.metadata_dict = current
        db.commit()
        filled.append(job.id)
    job_service.set_completed(db, job_id, {"filled": len(filled), "skipped": skipped,
                                           "failed": failed})
