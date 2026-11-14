"""What a demo cell is costing: a list-price estimate per cell, for the Demo hub.

Presenters build, book and schedule cells on their own (#1325, #1332). The booking-time
policy check (#1335) stops what is not allowed; this is the other half -- what IS being
spent, where the person spending it can see it.

**No new price source.** Every number here comes from machinery the VM spend cap already
uses and documents (``spend_policy``, ``spend_sweeper``):

* the rate is ``spend_sweeper.rate_for`` -- ``pov_cloud_cost.hourly_for_vm`` at list price,
  compute while running, disk always;
* running versus stopped is the dashboard's OWN power jobs, never a live describe, so a VM
  stopped in the cloud console still reads as running. That errs high, which is the
  direction this whole estimate errs in (no reservations, credits, free tier, data
  transfer or snapshots).

Three numbers per cell:

* ``rate_usd_h``  -- what it costs per hour right now;
* ``so_far_usd``  -- the integral of that since each VM came up, split on its power history
  (a VM with a spend cap reports its accrued figure instead, so the hub and the VM's own
  spend panel never disagree);
* ``to_end_usd``  -- the rate now, held until the cell's end time. ``None`` without one.

**Clusters are priced as their nodes**: node count x the node size's VM rate, from what the
provisioning request asked for or the Terraform module's default. The control-plane fee is
not, and the row says so.

**What is not priced is said, never counted as zero.** Managed databases have no price
source in the app yet; a VM in a region the catalogue cannot reach has no rate.
Both land in ``unpriced`` with the reason. An agent cell builds nothing -- it runs on a VM
that is somebody else's line -- so it carries a note and no number.

Prices are fetched by blocking HTTP on a cold cache. Callers run :func:`price_cells` off
the event loop (api/demo uses ``cloud_executor``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from ..database import Job
from . import suspend_sweeper, vm_suspend_policy

logger = logging.getLogger(__name__)

CAVEAT = ("Estimated at list price: no Savings Plans, reservations, credits, free tier, "
          "data transfer or snapshots. Errs high.")
AGENT_NOTE = "runs on an existing VM; its cost is that VM's"
UNPRICED_KIND = "databases are not priced yet"
NO_CLUSTER_ROW = "no cluster record to price"

RateFn = Callable[..., Optional[float]]


def _naive(dt) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _hours(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600.0)


# ── What a cell is made of ──────────────────────────────────────────────────────

def billable(db: Session, kind: str, cell: dict) -> tuple:
    """``(vm_rows, clusters, unpriced, note)`` for one cell, as the summary lists it."""
    if kind == "agent":
        return [], [], [], AGENT_NOTE
    if kind in ("ot", "net"):
        row = db.query(Job).filter(Job.id == cell["id"]).first()
        if row is None:
            return [], [], [], ""
        rows = [row]
        broker_id = row.metadata_dict.get("ot_broker_job_id") if kind == "ot" else ""
        if broker_id:
            broker = db.query(Job).filter(Job.id == broker_id).first()
            if broker is not None:
                rows.append(broker)
        return rows, [], [], ""
    if kind == "custom":
        from ..database import K8sCluster
        from . import custom_cell_service
        rows, clusters, unpriced = [], [], []
        for m in custom_cell_service.members_of(db, cell["id"]):
            if m.status == "destroyed":
                continue
            if m.kind == "vm":
                job = db.query(Job).filter(Job.id == m.ref_id).first() if m.ref_id else None
                if job is not None:
                    rows.append(job)
                continue
            if m.kind == "k8s" and m.ref_id:
                cluster = db.query(K8sCluster).filter(K8sCluster.id == m.ref_id).first()
                if cluster is not None:
                    clusters.append(cluster)
                    continue
            unpriced.append({"name": m.label, "kind": m.kind,
                             "reason": NO_CLUSTER_ROW if m.kind == "k8s" else UNPRICED_KIND})
        return rows, clusters, unpriced, ""
    return [], [], [], ""


def _events_from(db: Session, job_types, key: str, ids) -> dict:
    wanted = set(ids)
    out: dict = {}
    if not wanted:
        return out
    for job in (db.query(Job)
                .filter(Job.job_type.in_(tuple(job_types)), Job.status == "completed")
                .order_by(Job.created_at.asc())
                .all()):
        m = job.metadata_dict
        target = m.get(key)
        action = m.get("action")
        if target in wanted and action in ("start", "stop"):
            out.setdefault(target, []).append(
                (_naive(job.completed_at or job.created_at), action))
    return out


def power_events(db: Session, job_ids) -> dict:
    """``{deploy_job_id: [(at, "start"|"stop"), ...]}``, oldest first, for these VMs only.

    The same source as ``spend_sweeper.last_power_action`` -- the dashboard's own completed
    ``*_power`` jobs -- but keeping every transition and its time, which "so far" needs.
    One query; the deploy id lives in metadata, so the narrowing is done here.
    """
    return _events_from(db, suspend_sweeper._POWER_JOB.values(), "deploy_job_id", job_ids)


def cluster_power_events(db: Session, cluster_ids) -> dict:
    """The same, for clusters: the dashboard's own ``k8s_power`` jobs."""
    from . import k8s_power
    return _events_from(db, (k8s_power.JOB_TYPE,), "cluster_id", cluster_ids)


# ── The arithmetic ──────────────────────────────────────────────────────────────

def span_cost(start, end, events, *, rate_running: float, rate_stopped: float,
              since=None) -> float:
    """USD between ``start`` and ``end``: running spans at one rate, stopped at the other.

    ``events`` are ``(at, "start"|"stop")``, oldest first; the resource is running until
    the first stop. ``since`` floors the window (the budget's "this month") without
    forgetting whether the resource was running when the window opened.
    """
    start, end = _naive(start), _naive(end)
    if start is None or end is None or end <= start:
        return 0.0
    lo = max(start, _naive(since)) if since is not None else start
    total, at, running = 0.0, start, True
    for when, action in events or []:
        if when is None or when <= at:
            running = action == "start"
            continue
        if when >= end:
            break
        a, b = max(at, lo), when
        if b > a:
            total += _hours(a, b) * (rate_running if running else rate_stopped)
        at, running = when, action == "start"
    a = max(at, lo)
    if end > a:
        total += _hours(a, end) * (rate_running if running else rate_stopped)
    return total


def vm_cost(row, events: list, *, rate_running: float, rate_stopped: float,
            now: datetime) -> float:
    """USD so far for one VM: running spans at ``rate_running``, stopped at ``rate_stopped``.

    From when the deploy completed (the VM existed) to now, or to the row's last update
    once it is destroyed. A capped VM's accrued figure wins -- see the module docstring.
    """
    if getattr(row, "spend_accrued_at", None) is not None:
        return float(row.spend_estimate_usd or 0.0)
    return span_cost(row.completed_at or row.created_at, _vm_end(row, now), events,
                     rate_running=rate_running, rate_stopped=rate_stopped)


def _vm_end(row, now) -> datetime:
    end = _naive(now)
    if row.metadata_dict.get("destroyed") and row.updated_at:
        end = min(end, _naive(row.updated_at))
    return end


def _where(row) -> str:
    m = row.metadata_dict
    cloud = vm_suspend_policy.cloud_of(row.job_type) or "?"
    region = m.get("region") or m.get("location") or ""
    zone = m.get("zone") or ""
    if not region and zone:
        # Named as the price lookup keys it: a GCP zone is priced by its region.
        region = zone
        if cloud == "gcp":
            from . import region_catalog
            region = region_catalog.region_from_zone(zone) or zone
    return f"{cloud}/{region or '?'}"


def _vm_name(row) -> str:
    m = row.metadata_dict
    return (m.get("instance_name") or m.get("vm_name") or m.get("instance_id") or row.id)


# ── Clusters ────────────────────────────────────────────────────────────────────
#
# A cluster's NODES are priced: node count x the node size's VM rate. Its control plane
# is not (EKS and GKE bill a flat hourly fee the price lookup does not serve; AKS's free
# tier bills none), and the row says so. Where the provisioning request left the size or
# the count to Terraform, the module's own default is what was built.

CONTROL_PLANE_NOTE = "control-plane fee not included"

#: ``cloud -> (size key in tf_variables, count key, config default key, module size,
#: module count, node disk GB)``, read from terraform/k8s_cluster/*.
_NODE_SPEC = {
    "aws":   ("node_instance_type", "node_desired", "aws_eks_node_instance_type",
              "t3.small", 2, 20),
    "azure": ("vm_size", "node_count", "azure_aks_node_vm_size", "Standard_B2s", 2, 32),
    "gcp":   ("machine_type", "node_count", "gcp_gke_machine_type", "e2-standard-2", 3, 50),
}


def node_pool(db: Session, cluster) -> Optional[dict]:
    """``{size, count, disk_gb, region}`` as built, or None for a cloud with no spec."""
    tfv: dict = {}
    if cluster.deploy_job_id:
        prov = db.query(Job).filter(Job.id == cluster.deploy_job_id).first()
        tfv = ((prov.metadata_dict or {}).get("tf_variables") or {}) if prov else {}
    return pool_from(cluster.cloud or "", tfv, cluster.region or "")


def pool_for_request(cloud: str, region: str, spec: dict) -> Optional[dict]:
    """The same, for a cluster only booked: its ``ClusterProvisionRequest`` fields."""
    keys = _NODE_SPEC.get(cloud)
    if keys is None:
        return None
    tfv = {keys[0]: spec.get("node_instance_type"), keys[1]: spec.get("node_count"),
           "zone": spec.get("zone")}
    return pool_from(cloud, {k: v for k, v in tfv.items() if v}, region)


def pool_from(cloud: str, tfv: dict, region: str) -> Optional[dict]:
    spec = _NODE_SPEC.get(cloud)
    if spec is None:
        return None
    size_key, count_key, cfg_key, size_default, count_default, disk = spec
    size = tfv.get(size_key) or ""
    if not size:
        try:
            from . import config_service
            size = config_service.get(cfg_key, "") or ""
        except Exception:  # noqa: BLE001 -- the module default is still right
            size = ""
    try:
        count = int(tfv.get(count_key) or count_default)
    except (TypeError, ValueError):
        count = count_default
    region = region or tfv.get("zone") or tfv.get("region") or tfv.get("location") or ""
    return {"size": size or size_default, "count": max(0, count), "disk_gb": disk,
            "region": region}


def cluster_rates(db: Session, cluster, rate_spec) -> tuple:
    """``(rate_running, rate_stopped, reason)``. A stopped cluster's pools are at zero
    nodes (k8s_power scales them down; AKS's managed stop deallocates them), so its node
    cost is nothing."""
    if (cluster.source or "") != "provisioned":
        return None, None, "a registered cluster is billed to its own account"
    pool = node_pool(db, cluster)
    if pool is None:
        return None, None, f"no node pricing for {cluster.cloud} clusters"
    node = rate_spec(cluster.cloud, {"region": pool["region"], "instance_type": pool["size"],
                                     "disk_gb": pool["disk_gb"]}, running=True)
    if node is None:
        return None, None, f"no price source for {cluster.cloud}/{pool['region'] or '?'}"
    return node * pool["count"], 0.0, ""


def _cluster_running(cluster, history) -> bool:
    from . import k8s_power
    if k8s_power.is_stopped(cluster):
        return False
    return (history[-1][1] if history else "start") != "stop"


# ── One cell ────────────────────────────────────────────────────────────────────

def cell_cost(db: Session, kind: str, cell: dict, *, now: datetime,
              events: Optional[dict] = None, rate_for: Optional[RateFn] = None,
              rate_spec: Optional[RateFn] = None) -> dict:
    """The three numbers for one cell, plus what could not be priced and why."""
    from . import spend_sweeper
    rate_for = rate_for or spend_sweeper.rate_for
    rate_spec = rate_spec or spend_sweeper.rate_for_spec
    rows, clusters, unpriced, note = billable(db, kind, cell)
    if events is None:
        events = power_events(db, [r.id for r in rows])
    rate_now, so_far, priced = 0.0, 0.0, 0
    for row in rows:
        m = row.metadata_dict
        if m.get("destroyed") or row.status != "completed":
            continue
        history = events.get(row.id) or []
        running_now = (history[-1][1] if history else "start") != "stop"
        on = rate_for(row, m, running=True)
        off = rate_for(row, m, running=False)
        if on is None or off is None:
            unpriced.append({"name": _vm_name(row), "kind": "vm",
                             "reason": f"no price source for {_where(row)}"})
            continue
        priced += 1
        rate_now += on if running_now else off
        so_far += vm_cost(row, history, rate_running=on, rate_stopped=off, now=now)

    cluster_events = cluster_power_events(db, [c.id for c in clusters]) if clusters else {}
    for cluster in clusters:
        on, off, reason = cluster_rates(db, cluster, rate_spec)
        if on is None or off is None:
            unpriced.append({"name": cluster.name, "kind": "k8s", "reason": reason})
            continue
        history = cluster_events.get(cluster.id) or []
        priced += 1
        rate_now += on if _cluster_running(cluster, history) else off
        so_far += span_cost(cluster.created_at, now, history,
                            rate_running=on, rate_stopped=off)
        unpriced.append({"name": cluster.name, "kind": "k8s-control-plane",
                         "reason": CONTROL_PLANE_NOTE})

    end = _naive(cell.get("end_at"))
    to_end = None
    if end is not None and priced:
        to_end = so_far + rate_now * _hours(_naive(now), end)
    return {
        "rate_usd_h": round(rate_now, 4) if priced else None,
        "so_far_usd": round(so_far, 2) if priced else None,
        "to_end_usd": round(to_end, 2) if to_end is not None else None,
        "priced": priced,
        "unpriced": unpriced,
        "note": note,
    }


def price_cells(db: Session, cells_by_kind: dict, *, now: Optional[datetime] = None,
                rate_for: Optional[RateFn] = None) -> dict:
    """``{"cells": {kind: {id: cost}}, "totals": {kind: {...}}, "caveat"}``.

    ``cells_by_kind`` is ``{kind: [cell, ...]}`` exactly as the hub's summary lists them, so
    nothing is priced that the caller cannot see. One cell that fails is that cell's note;
    it never takes the others with it.
    """
    now = now or datetime.now(timezone.utc)
    out: dict = {"cells": {}, "totals": {}, "caveat": CAVEAT}
    for kind, cells in cells_by_kind.items():
        per, rate, so_far, any_priced = {}, 0.0, 0.0, False
        for cell in cells:
            try:
                c = cell_cost(db, kind, cell, now=now, rate_for=rate_for)
            except Exception:  # noqa: BLE001 -- one cell never blanks the rest
                logger.warning("demo cell cost: %s %s", kind, cell.get("id"), exc_info=True)
                c = {"rate_usd_h": None, "so_far_usd": None, "to_end_usd": None,
                     "priced": 0, "unpriced": [], "note": "could not be estimated"}
            per[cell["id"]] = c
            if c["priced"]:
                any_priced = True
                rate += c["rate_usd_h"] or 0.0
                so_far += c["so_far_usd"] or 0.0
        out["cells"][kind] = per
        out["totals"][kind] = {"rate_usd_h": round(rate, 4) if any_priced else None,
                               "so_far_usd": round(so_far, 2) if any_priced else None}
    return out
