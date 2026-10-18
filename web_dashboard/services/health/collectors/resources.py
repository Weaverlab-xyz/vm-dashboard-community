"""Resources: is everything the dashboard built or registered still there and running?

The object list is the Inventory's (``inventory_service.collect``), so every row on
/inventory gets a health row under the same id. Each object gets:

* **Its live state from its cloud**, for cloud VMs, managed databases and Kubernetes
  clusters, read in batches (``cloud_status``). A resource the cloud no longer has is
  failing: the dashboard believes it exists and is managing access to it.
* **Its recorded state**, for the labs, workload credentials, functions, desktops and
  POVs, which the dashboard itself drives.
* **Its access wiring and lifecycle**: Password Safe onboarding and PRA and Entitle
  links where they were set up, a recorded onboarding error, an auto-delete inside a
  day, a spend cap reached. Wiring that is not there was never asked for and is not
  reported; wiring that failed is degraded.

Hypervisor VMs come from the inventory cache, which the hypervisor sync keeps; nothing
is asked of the hypervisors here. Directories are left to their own area.

A cloud that cannot be asked makes its resources unknown for this run, with the reason,
rather than failing them.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from .. import core
from . import cloud_status
from ....utils.clock import utcnow

logger = logging.getLogger(__name__)

EXPIRY_WARN = timedelta(hours=24)

_STORED_OK = {"available", "active", "running", "ready", "deployed", "completed",
              "registered", "rotated", "healthy", "issued", "connected", "succeeded"}
_STORED_BUSY = {"provisioning", "pending", "creating", "applying", "deploying",
                "destroying", "decommissioning", "deleting", "queued", "building",
                "starting", "stopping", "updating"}
_STORED_FAIL = {"failed", "error"}

_KIND_LABELS = {"function": "Cloud function", "certlab": "Certificate lab",
                "spirelab": "SPIRE lab", "workloadk8s": "Workload token",
                "workloadcloud": "Workload cloud credential", "pov": "Proof of value",
                "desktop": "Virtual desktop", "vm": "Virtual machine",
                "database": "Database", "k8s": "Kubernetes cluster"}


# ── pure mappings ─────────────────────────────────────────────────────────────

def stored_check(state: str) -> dict:
    s = (state or "").lower()
    status = (core.OK if s in _STORED_OK else core.FAIL if s in _STORED_FAIL
              else core.UNKNOWN if s in _STORED_BUSY else core.WARN)
    return core.check("state", "State", status, f"Recorded state: {state or 'unknown'}.")


def vm_check(cloud: str, live: Optional[dict], *, stop_expected: str = "") -> dict:
    """The infrastructure check for one cloud VM. ``live`` is the cloud's answer for it:
    ``None`` when the cloud could not be asked, ``{}`` when it has no such instance.
    ``stop_expected`` names why a stopped VM is fine (the dashboard stopped it, a suspend
    schedule, a spend cap), or is empty."""
    label = "Instance"
    if live is None:
        return core.check("instance", label, core.UNKNOWN, "The cloud could not be asked.")
    if not live:
        return core.check("instance", label, core.FAIL,
                          f"{cloud.upper()} no longer has this instance, though the "
                          f"dashboard still lists it.")
    state = (live.get("state") or live.get("power") or "").lower()
    if state in ("terminated", "deleted", "shutting-down"):
        return core.check("instance", label, core.FAIL, f"The instance is {state}.")
    if (live.get("provisioning") or "").lower() == "failed":
        return core.check("instance", label, core.FAIL, "Azure reports provisioning failed.")
    if state in ("running", "started"):
        impaired = [p for p in ("system", "instance") if live.get(p) == "impaired"]
        if impaired:
            return core.check("instance", label, core.FAIL,
                              f"Running, but the {' and '.join(impaired)} status check "
                              f"reports impaired.")
        if live.get("events"):
            return core.check("instance", label, core.WARN,
                              "Running, with a scheduled event: "
                              + core.text("; ".join(live["events"]), 200))
        return core.check("instance", label, core.OK, "Running.")
    if state in ("stopped", "deallocated", "suspended"):
        if stop_expected:
            return core.check("instance", label, core.OK, f"Stopped ({stop_expected}).")
        return core.check("instance", label, core.WARN,
                          "Stopped, though nothing in the dashboard stopped it.")
    return core.check("instance", label, core.UNKNOWN, f"State: {state or 'unknown'}.")


_RDS_OK = {"available", "backing-up"}
_RDS_FAIL = {"failed", "storage-full", "inaccessible-encryption-credentials",
             "incompatible-network", "incompatible-option-group",
             "incompatible-parameters", "incompatible-restore", "restore-error"}
_AZ_DB_OK = {"ready"}
_AZ_DB_FAIL = {"disabled", "dropping"}


def database_check(provider: str, live) -> dict:
    """The infrastructure check for one managed database. ``live`` as for VMs."""
    label = "Database server"
    if live is None:
        return core.check("server", label, core.UNKNOWN, "The cloud could not be asked.")
    if not live:
        return core.check("server", label, core.FAIL,
                          "The cloud no longer has this database, though the dashboard "
                          "still lists it.")
    if provider == "rds":
        s = str(live).lower()
        status = core.OK if s in _RDS_OK else core.FAIL if s in _RDS_FAIL else core.WARN
        return core.check("server", label, status, f"RDS status: {s}.")
    if provider == "cloudsql":
        state = (live.get("state") or "").upper()
        if state == "RUNNABLE" and live.get("activation") == "NEVER":
            return core.check("server", label, core.WARN, "Stopped (activation policy NEVER).")
        status = (core.OK if state == "RUNNABLE" else
                  core.FAIL if state in ("FAILED", "UNKNOWN_STATE") else core.WARN)
        return core.check("server", label, status, f"Cloud SQL state: {state or 'unknown'}.")
    state = (live.get("state") or "").lower()
    status = core.OK if state in _AZ_DB_OK else core.FAIL if state in _AZ_DB_FAIL else core.WARN
    return core.check("server", label, status, f"Azure state: {state or 'unknown'}.")


def cluster_checks(cloud: str, live) -> list:
    """Checks for one managed Kubernetes cluster and its node pools."""
    if live is None:
        return [core.check("cluster", "Cluster", core.UNKNOWN, "The cloud could not be asked.")]
    if not live:
        return [core.check("cluster", "Cluster", core.FAIL,
                           f"{cloud.upper()} no longer has this cluster, though the "
                           f"dashboard still lists it.")]
    out = []
    if cloud == "aws":
        s = live.get("status", "")
        status = core.OK if s == "ACTIVE" else core.FAIL if s == "FAILED" else core.WARN
        detail = f"EKS status {s}." + (" " + core.text("; ".join(live["issues"]), 200)
                                       if live.get("issues") else "")
        out.append(core.check("cluster", "Cluster", core.WARN if status == core.OK
                              and live.get("issues") else status, detail))
        for name, ng in (live.get("nodegroups") or {}).items():
            ns = ng.get("status", "")
            st = core.OK if ns == "ACTIVE" else core.FAIL if ns in ("DEGRADED", "CREATE_FAILED",
                                                                      "DELETE_FAILED") else core.WARN
            out.append(core.check(f"pool:{name}", f"Node group {name}", st,
                                  f"{ns}." + (" " + core.text("; ".join(ng["issues"]), 200)
                                              if ng.get("issues") else "")))
    elif cloud == "gcp":
        s = live.get("status", "")
        status = (core.OK if s == "RUNNING" else core.FAIL if s == "ERROR" else core.WARN)
        out.append(core.check("cluster", "Cluster", status, f"GKE status {s}."
                              + (f" {core.text(live.get('message'), 200)}"
                                 if live.get("message") else "")))
        for name, ps in (live.get("nodepools") or {}).items():
            st = core.OK if ps == "RUNNING" else core.FAIL if ps == "ERROR" else core.WARN
            out.append(core.check(f"pool:{name}", f"Node pool {name}", st, f"{ps}."))
    else:
        prov, power = live.get("provisioning", ""), live.get("power", "")
        status = (core.FAIL if prov == "Failed" else core.WARN if power == "Stopped"
                  or prov != "Succeeded" else core.OK)
        out.append(core.check("cluster", "Cluster", status,
                              f"AKS provisioning {prov or 'unknown'}, power {power or 'unknown'}."))
        for name, ps in (live.get("pools") or {}).items():
            st = core.OK if ps == "Succeeded" else core.FAIL if ps == "Failed" else core.WARN
            out.append(core.check(f"pool:{name}", f"Node pool {name}", st, f"{ps}."))
    return out


def lifecycle_checks(*, expires_at: Optional[datetime], spend_capped: bool,
                     spend_warned: bool, now: datetime) -> list:
    out = []
    if expires_at:
        left = expires_at - now
        if left <= timedelta(0):
            out.append(core.check("expiry", "Auto-delete", core.WARN,
                                  "Past its auto-delete time; the next sweep removes it."))
        elif left <= EXPIRY_WARN:
            out.append(core.check("expiry", "Auto-delete", core.WARN,
                                  f"Will be deleted in {max(1, int(left.total_seconds() // 3600))} "
                                  f"h by the auto-delete timer."))
    if spend_capped:
        out.append(core.check("spend", "Spend cap", core.WARN, "Stopped at its spend cap."))
    elif spend_warned:
        out.append(core.check("spend", "Spend cap", core.WARN, "Close to its spend cap."))
    return out


def wiring_checks(*, ps_system: str = "", ps_account: str = "", ps_error: str = "",
                  pra: str = "", entitle: str = "") -> list:
    """Access wiring that was set up, and any recorded failure of it. Pure."""
    out = []
    if ps_error:
        out.append(core.check("password_safe", "Password Safe", core.WARN,
                              f"Onboarding failed: {core.text(ps_error, 250)}"))
    elif ps_system or ps_account:
        out.append(core.check("password_safe", "Password Safe", core.OK,
                              "Onboarded" + (f" (account {ps_account})" if ps_account else
                                             f" (managed system {ps_system})") + "."))
    if pra:
        out.append(core.check("pra", "PRA", core.OK, f"Reached through {core.text(pra, 120)}."))
    if entitle:
        out.append(core.check("entitle", "Entitle", core.OK, "Access is requestable."))
    return out


# ── collection ────────────────────────────────────────────────────────────────

async def _safe(fn, *args):
    """``fn(*args)`` (sync in a thread, or async), or None when it raised."""
    try:
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args)
        return await asyncio.to_thread(fn, *args)
    except Exception:  # noqa: BLE001 - the batch reads as unknown; details to the log
        logger.warning("health: resource status read %s%r failed", fn.__name__, args[:2],
                       exc_info=True)
        return None


def _when(value) -> Optional[datetime]:
    return core.parse_when(value) if isinstance(value, str) else value


async def _vm_live(jobs: list) -> dict:
    """``{job_id: live-or-None-or-{}}`` for the cloud VM deploy jobs."""
    from ... import gcp_service
    out: dict = {}
    aws_by_region: dict = {}
    gcp_projects: set = set()
    for job in jobs:
        meta = job.metadata_dict
        if job.job_type == "ec2_deploy" and meta.get("instance_id"):
            aws_by_region.setdefault(meta.get("region") or "", []).append(job)
        elif job.job_type == "gce_deploy" and meta.get("instance_name"):
            gcp_projects.add(meta.get("project_id") or gcp_service._gcp_project())
    for region, rows in aws_by_region.items():
        ans = await _safe(cloud_status.aws_instances, region,
                          [r.metadata_dict["instance_id"] for r in rows])
        for r in rows:
            out[r.id] = None if ans is None else ans.get(r.metadata_dict["instance_id"], {})
    gcp_answers = {p: await _safe(cloud_status.gcp_instances, p) for p in gcp_projects if p}
    for job in jobs:
        meta = job.metadata_dict
        if job.job_type == "gce_deploy" and meta.get("instance_name"):
            ans = gcp_answers.get(meta.get("project_id") or gcp_service._gcp_project())
            zone = (meta.get("zone") or "").rsplit("/", 1)[-1]
            if ans is None:
                out[job.id] = None
            else:
                status = ans.get((zone, meta["instance_name"]))
                out[job.id] = {} if status is None else {
                    "state": {"TERMINATED": "stopped", "SUSPENDED": "suspended"}.get(
                        status, status.lower())}
        elif job.job_type == "azure_deploy" and meta.get("vm_name"):
            from ...suspend_sweeper import _azure_rg
            out[job.id] = await _safe(cloud_status.azure_vm,
                                      meta.get("resource_group") or _azure_rg(),
                                      meta["vm_name"])
        elif job.job_type == "oci_deploy" and meta.get("instance_ocid"):
            state = await _safe(cloud_status.oci_instance, meta["instance_ocid"])
            out[job.id] = None if state is None else (
                {} if not state else {"state": {"STOPPED": "stopped"}.get(state, state.lower())})
    return out


async def _db_live(rows: list) -> dict:
    from ... import gcp_service
    out: dict = {}
    rds_regions = {r.region or "" for r in rows if r.provider == "rds" and r.cloud == "aws"}
    rds = {reg: await _safe(cloud_status.aws_databases, reg) for reg in rds_regions}
    sql = None
    if any(r.provider == "cloudsql" for r in rows):
        sql = await _safe(cloud_status.gcp_databases, gcp_service._gcp_project())
    for r in rows:
        if not r.instance_id or r.source == "registered" and r.cloud == "local":
            continue
        if r.provider == "rds":
            ans = rds.get(r.region or "")
            out[r.id] = None if ans is None else ans.get(r.instance_id, {})
        elif r.provider == "cloudsql":
            out[r.id] = None if sql is None else sql.get(r.instance_id, {})
        elif r.provider in ("flexibleserver", "sql_database"):
            out[r.id] = await _safe(cloud_status.azure_database, r.instance_id)
    return out


async def _k8s_live(rows: list) -> dict:
    from ... import gcp_service
    out: dict = {}
    aks = None
    if any(r.cloud == "azure" for r in rows):
        aks = await _safe(cloud_status.azure_clusters)
    for r in rows:
        if r.cloud == "aws" and r.region:
            out[r.id] = await _safe(cloud_status.aws_cluster, r.region, r.name)
        elif r.cloud == "gcp" and r.region:
            out[r.id] = await _safe(cloud_status.gcp_cluster, gcp_service._gcp_project(),
                                    r.region, r.name)
        elif r.cloud == "azure":
            out[r.id] = None if aks is None else aks.get(r.name, {})
    return out


async def collect(db) -> list:
    from ... import inventory_service, spend_sweeper
    from ....database import CloudDatabase, Job, K8sCluster
    now = utcnow()
    inventory = [i for i in inventory_service.collect(db) if i.get("kind") != "directory"]

    job_ids = [i["id"].split(":", 1)[1] for i in inventory if i["id"].startswith("job:")]
    jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_(job_ids)).all()} if job_ids else {}
    db_rows = {r.id: r for r in db.query(CloudDatabase).all()}
    k8s_rows = {r.id: r for r in db.query(K8sCluster).all()}
    try:
        stopped_by_dashboard = {k for k, v in spend_sweeper.last_power_action(db).items()
                                if v == "stop"}
    except Exception:  # noqa: BLE001
        stopped_by_dashboard = set()

    vm_live = await _vm_live(list(jobs.values()))
    db_live = await _db_live(list(db_rows.values()))
    k8s_live = await _k8s_live(list(k8s_rows.values()))

    items = []
    for inv in inventory:
        prefix, _, oid = inv["id"].partition(":")
        checks: list = []
        expires = _when(inv.get("expires_at"))
        if prefix == "job" and oid in jobs:
            job, meta = jobs[oid], jobs[oid].metadata_dict
            reason = ("stopped by the dashboard" if oid in stopped_by_dashboard else
                      "spend cap" if job.spend_capped_at else
                      "suspend schedule" if job.suspend_at_local else "")
            if oid in vm_live:
                checks.append(vm_check(inv["cloud"], vm_live[oid], stop_expected=reason))
            checks += wiring_checks(ps_system=str(meta.get("ps_managed_system_id") or ""),
                                    ps_error=str(meta.get("ps_error") or ""),
                                    pra=str(meta.get("jumpoint_name") or ""))
            checks += lifecycle_checks(expires_at=expires, now=now,
                                       spend_capped=bool(job.spend_capped_at),
                                       spend_warned=bool(job.spend_warned_at))
        elif prefix == "clouddb" and oid in db_rows:
            row = db_rows[oid]
            if oid in db_live:
                checks.append(database_check(row.provider or "", db_live[oid]))
            else:
                checks.append(stored_check(row.status))
            checks += wiring_checks(ps_account=str(row.ps_managed_account_id or ""),
                                    pra=str(row.jump_item_id and (row.jumpoint_name
                                                                  or "a PRA jump item") or ""),
                                    entitle=str(row.entitle_integration_id or ""))
            checks += lifecycle_checks(expires_at=row.expires_at, now=now,
                                       spend_capped=False, spend_warned=False)
        elif prefix == "k8s" and oid in k8s_rows:
            row = k8s_rows[oid]
            if oid in k8s_live:
                checks += cluster_checks(row.cloud, k8s_live[oid])
            else:
                checks.append(stored_check(row.status))
            checks += wiring_checks(ps_account=str(row.ps_token_account_id or ""),
                                    pra=str(row.pra_jump_id and (row.jumpoint_name
                                                                 or "a PRA tunnel") or ""))
            checks += lifecycle_checks(expires_at=row.expires_at, now=now,
                                       spend_capped=False, spend_warned=False)
        elif prefix == "hv":
            state = (inv.get("state") or "").lower()
            checks.append(core.check("power", "Power", core.OK if state == "running"
                                     else core.WARN if state else core.UNKNOWN,
                                     f"{(state or 'unknown').capitalize()}, as of the last "
                                     f"inventory sync."))
        else:
            checks.append(stored_check(inv.get("state") or ""))
            checks += lifecycle_checks(expires_at=expires, now=now, spend_capped=False,
                                       spend_warned=False)
        items.append(core.Item(
            object_id=inv["id"], name=inv.get("name") or inv["id"],
            kind=f"{_KIND_LABELS.get(inv.get('kind'), inv.get('kind') or '')}"
                 + (f" · {inv['cloud']}" if inv.get("cloud") else ""),
            href=inv.get("detail_href") or "/inventory", checks=checks))
    return items
