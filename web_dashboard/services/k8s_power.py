"""Stop and start for dashboard-provisioned Kubernetes clusters.

The cluster half of a demo cell's auto-suspend (services/clouddb_power is the database
half, and the module docstring there explains why neither goes through Terraform).

What "stopped" means differs by cloud, because only one of them has a real stop:

  * **AKS** -- ``managedClusters/stop`` / ``start``. The control plane and every node go
    away and Azure keeps the node pools' configuration, so nothing is recorded to restore.
  * **GKE, EKS** -- no managed stop. Every node pool / managed nodegroup is scaled to 0,
    with autoscaling turned off first (GKE) or its floor dropped to 0 (EKS, whose API
    insists max >= 1), so nothing scales it straight back up. The control plane keeps
    running and keeps billing its flat fee; the nodes are the cost that matters. The
    sizes and autoscaling bounds are saved in ``K8sCluster.power_state["restore"]``
    BEFORE the first pool is touched, and a start puts back exactly those.

Row states: ``registered`` / ``managed`` / ``awaiting_agent`` (the three a live cluster
can be in) -> ``stopping`` -> ``stopped`` -> ``starting`` -> whichever of the three it was
stopped from. As for databases, the transitional status is written when the job is
queued, and a failed job puts the row back.

Terraform: the GKE and EKS modules ``ignore_changes`` the node counts (see
terraform/k8s_cluster/*), so a stopped cluster's drift is not "fixed" by a later apply.
A destroy never cared -- it deletes from state whatever the sizes are.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

JOB_TYPE = "k8s_power"
ACTIONS = ("stop", "start")

STATUS_STOPPED = "stopped"
STATUS_STOPPING = "stopping"
STATUS_STARTING = "starting"
TRANSITIONAL = frozenset({STATUS_STOPPING, STATUS_STARTING})
POWER_STATUSES = frozenset({STATUS_STOPPED, STATUS_STOPPING, STATUS_STARTING})
#: The statuses of a live, idle cluster -- the ones a stop may start from.
RUNNING_STATUSES = frozenset({"registered", "managed", "awaiting_agent"})
POWERABLE_CLOUDS = frozenset({"gcp", "aws", "azure"})

POWER_TIMEOUT_S = 45 * 60
POLL_INTERVAL_S = 15.0
_AKS_API = "2024-02-01"
_GKE = "https://container.googleapis.com/v1"


class PowerRefused(Exception):
    """Not in the row's current state (HTTP 409)."""


class PowerUnsupported(Exception):
    """This cluster can never be powered by the dashboard (HTTP 400)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_state(row) -> dict:
    """``power_state`` as a dict: ``state`` ("running" | "stopped"), ``resume_status``,
    ``prior_status``, ``restore`` ({pool name: {...}}), ``stopped_at`` / ``started_at``,
    ``job_id``, ``action``, ``last_error``."""
    raw = getattr(row, "power_state", None)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _write_state(row, state: dict) -> None:
    row.power_state = json.dumps(state, sort_keys=True)


def is_stopped(row) -> bool:
    return (getattr(row, "status", "") or "") in (STATUS_STOPPED, STATUS_STOPPING)


def unsupported_reason(row) -> Optional[str]:
    if (getattr(row, "source", None) or "registered") != "provisioned":
        return ("a registered cluster belongs to somebody else's account; the dashboard "
                "does not stop or start it")
    if (getattr(row, "cloud", "") or "") not in POWERABLE_CLOUDS:
        return (f"{getattr(row, 'cloud', '')} clusters have no stop/start the dashboard "
                f"drives (supported: GKE, EKS, AKS)")
    if not getattr(row, "deploy_job_id", None):
        return "no provisioning job is recorded for this cluster"
    return None


def merge_restore(previous: dict, snapshot: dict) -> dict:
    """The restore map to save before a stop.

    A stop that died halfway left some pools at 0. Re-snapshotting would record those
    zeros as the sizes to restore -- and a start would then "restore" a cluster to no
    nodes. So a pool now at 0 keeps the entry an earlier snapshot recorded for it.
    """
    out = {}
    for name, cur in (snapshot or {}).items():
        old = (previous or {}).get(name)
        out[name] = old if (old and not _pool_size(cur) and _pool_size(old)) else cur
    return out


def _pool_size(entry) -> int:
    try:
        return int((entry or {}).get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _active_power_job(db, cluster_id: str):
    from ..database import Job
    from . import job_service
    jobs = (db.query(Job)
              .filter(Job.job_type == JOB_TYPE,
                      Job.status.in_(job_service.ACTIVE_STATUSES))
              .order_by(Job.created_at.desc()).all())
    return next((j for j in jobs
                 if (j.metadata_dict or {}).get("cluster_id") == cluster_id), None)


# ── enqueue ──────────────────────────────────────────────────────────────────

def start_power(db, cluster_id: str, action: str, actor: str = "") -> dict:
    """Queue a ``k8s_power`` job and flip the row to its transitional status. Same
    idempotency contract as ``clouddb_power.start_power``. Raises ``K8sError`` (unknown
    id), :class:`PowerUnsupported` or :class:`PowerRefused`."""
    from ..database import K8sCluster
    from . import job_service
    from .k8s_service import K8sError

    if action not in ACTIONS:
        raise PowerUnsupported(f"unknown power action {action!r} (expected stop or start)")
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    reason = unsupported_reason(row)
    if reason:
        raise PowerUnsupported(reason)

    status = row.status or ""
    active = _active_power_job(db, cluster_id)
    if active is not None:
        if (active.metadata_dict or {}).get("action") == action:
            return {"ok": True, "cluster_id": cluster_id, "action": action,
                    "job_id": active.id, "status": status}
        raise PowerRefused(f"a {(active.metadata_dict or {}).get('action')} of this "
                           f"cluster is already in progress (job {active.id})")

    if status in TRANSITIONAL:
        prior = read_state(row).get("prior_status") or "registered"   # a dead job's row
    elif action == "stop":
        if status == STATUS_STOPPED:
            return {"ok": True, "noop": True, "cluster_id": cluster_id, "action": action,
                    "status": status}
        if status not in RUNNING_STATUSES:
            raise PowerRefused(f"cluster is {status!r} -- only a running cluster "
                               f"({', '.join(sorted(RUNNING_STATUSES))}) can be stopped")
        prior = status
    else:
        if status in RUNNING_STATUSES:
            return {"ok": True, "noop": True, "cluster_id": cluster_id, "action": action,
                    "status": status}
        if status != STATUS_STOPPED:
            raise PowerRefused(f"cluster is {status!r} -- only a stopped cluster can be "
                               f"started")
        prior = status

    state = read_state(row)
    state["prior_status"] = prior
    if action == "stop" and prior in RUNNING_STATUSES:
        state["resume_status"] = prior
    _write_state(row, state)
    row.status = STATUS_STOPPING if action == "stop" else STATUS_STARTING
    db.commit()
    job = job_service.create_job(
        db, job_type=JOB_TYPE, created_by=actor or row.created_by or "system",
        metadata={"cluster_id": cluster_id, "action": action, "cloud": row.cloud,
                  "name": row.name},
        workgroup=row.workgroup,
    )
    try:
        job_service.log_audit(db, actor or "system", f"k8s_power_{action}",
                              details={"cluster_id": cluster_id, "job_id": job.id,
                                       "cloud": row.cloud, "name": row.name})
    except Exception as exc:  # noqa: BLE001
        logger.warning("k8s power: audit for %s failed: %s", cluster_id, exc)
    return {"ok": True, "cluster_id": cluster_id, "action": action, "job_id": job.id,
            "status": row.status}


def revert_stale(row) -> None:
    """Put a row a dead power job left mid-transition back (job_service's reconciler)."""
    if (row.status or "") in TRANSITIONAL:
        row.status = read_state(row).get("prior_status") or "registered"


# ── cloud backends ───────────────────────────────────────────────────────────
#
# ``snapshot()`` -> {pool: entry}; ``stop(restore)``; ``start(restore, fallback_size)``.
# All blocking, each run whole on the k8s pool (the worker's per-call deadline is hours).
# Node-pool operations on one GKE cluster are serialised by GKE itself, which is why the
# pools are walked one at a time and every operation is waited out before the next.

class _GkeBackend:
    def __init__(self, project: str, cluster: str, location: str = ""):
        self.project, self.cluster, self.location = project, cluster, location

    def _s(self):
        from . import gcp_service
        return gcp_service._authed_session()

    def _loc(self) -> str:
        if not self.location:
            from . import gcp_service
            _name, self.location = gcp_service.find_gke_cluster(self.cluster, self.project)
        return self.location

    def _base(self) -> str:
        return (f"{_GKE}/projects/{self.project}/locations/{self._loc()}"
                f"/clusters/{self.cluster}")

    def _wait(self, s, body: dict, what: str) -> None:
        op = (body or {}).get("name") or ""
        if not op:
            return
        url = f"{_GKE}/projects/{self.project}/locations/{self._loc()}/operations/{op}"
        deadline = time.monotonic() + POWER_TIMEOUT_S
        while True:
            r = s.get(url)
            r.raise_for_status()
            b = r.json() or {}
            if b.get("status") == "DONE":
                err = b.get("error") or {}
                if err.get("message") or b.get("statusMessage", "").lower().startswith("error"):
                    raise RuntimeError(f"GKE {what}: {err.get('message') or b.get('statusMessage')}")
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"GKE {what} did not finish within {POWER_TIMEOUT_S}s")
            time.sleep(POLL_INTERVAL_S)

    def _post(self, s, pool: str, verb: str, payload: dict) -> None:
        r = s.post(f"{self._base()}/nodePools/{pool}:{verb}", json=payload)
        r.raise_for_status()
        self._wait(s, r.json(), f"{verb} {pool}")

    def _pool_size(self, s, pool: dict) -> int:
        # The node pool resource carries no current size; its instance group managers do
        # (one per zone, and setSize is per zone, so the largest is the per-zone count).
        sizes = []
        for url in pool.get("instanceGroupUrls") or []:
            try:
                r = s.get(url.replace("/instanceGroups/", "/instanceGroupManagers/"))
                r.raise_for_status()
                sizes.append(int((r.json() or {}).get("targetSize") or 0))
            except Exception as exc:  # noqa: BLE001 - fall back to the declared size
                logger.info("gke power: could not read %s: %s", url, exc)
        return max(sizes) if sizes else int(pool.get("initialNodeCount") or 0)

    def snapshot(self) -> dict:
        s = self._s()
        r = s.get(f"{self._base()}/nodePools")
        r.raise_for_status()
        out = {}
        for pool in (r.json() or {}).get("nodePools") or []:
            auto = pool.get("autoscaling") or {}
            out[pool["name"]] = {
                "size": self._pool_size(s, pool),
                "autoscaling": auto if auto.get("enabled") else None,
            }
        return out

    def stop(self, restore: dict) -> None:
        s = self._s()
        for name, entry in restore.items():
            if (entry or {}).get("autoscaling"):
                self._post(s, name, "setAutoscaling", {"autoscaling": {"enabled": False}})
            self._post(s, name, "setSize", {"nodeCount": 0})

    def start(self, restore: dict, fallback_size: int) -> None:
        s = self._s()
        if not restore:
            restore = {n: {"size": fallback_size, "autoscaling": None}
                       for n in self.snapshot()}
        for name, entry in restore.items():
            self._post(s, name, "setSize",
                       {"nodeCount": _pool_size(entry) or fallback_size})
            if (entry or {}).get("autoscaling"):
                self._post(s, name, "setAutoscaling", {"autoscaling": entry["autoscaling"]})


class _EksBackend:
    def __init__(self, cluster: str, region: str):
        self.cluster, self.region = cluster, region

    def _c(self):
        from . import aws_service
        return aws_service.aws_client("eks", self.region)

    def _groups(self, c) -> list:
        names, token = [], None
        while True:
            kw = {"clusterName": self.cluster, **({"nextToken": token} if token else {})}
            resp = c.list_nodegroups(**kw)
            names += resp.get("nodegroups") or []
            token = resp.get("nextToken")
            if not token:
                return names

    def _apply(self, c, name: str, cfg: dict) -> None:
        c.update_nodegroup_config(clusterName=self.cluster, nodegroupName=name,
                                  scalingConfig=cfg)
        c.get_waiter("nodegroup_active").wait(
            clusterName=self.cluster, nodegroupName=name,
            WaiterConfig={"Delay": max(1, int(POLL_INTERVAL_S)),
                          "MaxAttempts": max(1, int(POWER_TIMEOUT_S
                                                    // max(1.0, POLL_INTERVAL_S)))})

    def snapshot(self) -> dict:
        c = self._c()
        out = {}
        for name in self._groups(c):
            sc = (c.describe_nodegroup(clusterName=self.cluster, nodegroupName=name)
                  .get("nodegroup") or {}).get("scalingConfig") or {}
            out[name] = {"size": int(sc.get("desiredSize") or 0),
                         "min": int(sc.get("minSize") or 0),
                         "max": int(sc.get("maxSize") or 1)}
        return out

    def stop(self, restore: dict) -> None:
        c = self._c()
        for name, entry in restore.items():
            # maxSize must stay >= 1 (EKS rejects 0); min and desired at 0 is the stop.
            self._apply(c, name, {"minSize": 0, "desiredSize": 0,
                                  "maxSize": max(1, int((entry or {}).get("max") or 1))})

    def start(self, restore: dict, fallback_size: int) -> None:
        c = self._c()
        if not restore:
            restore = {n: {"size": fallback_size, "min": fallback_size,
                           "max": max(fallback_size, 1)} for n in self._groups(c)}
        for name, entry in restore.items():
            size = _pool_size(entry) or fallback_size
            lo = min(int(entry.get("min") or 0), size)
            hi = max(int(entry.get("max") or 1), size, 1)
            self._apply(c, name, {"minSize": lo, "desiredSize": size, "maxSize": hi})


class _AksBackend:
    """ARM REST -- the image has no azure-mgmt-containerservice, and POST .../stop is
    what ``managedClusters.begin_stop`` sends."""

    def __init__(self, resource_id: str, credential):
        self.resource_id, self.credential = resource_id, credential

    def _h(self) -> dict:
        from . import azure_service
        tok = self.credential.get_token(f"{azure_service._ARM}/.default").token
        return {"Authorization": f"Bearer {tok}"}

    def _url(self, suffix: str = "") -> str:
        from . import azure_service
        return f"{azure_service._ARM}{self.resource_id}{suffix}?api-version={_AKS_API}"

    def _read(self) -> tuple:
        import httpx
        r = httpx.get(self._url(), headers=self._h(), timeout=30)
        r.raise_for_status()
        props = (r.json() or {}).get("properties") or {}
        return ((props.get("powerState") or {}).get("code") or "",
                props.get("provisioningState") or "")

    def _drive(self, verb: str, want: str) -> None:
        import httpx
        deadline = time.monotonic() + POWER_TIMEOUT_S
        requested = False
        while True:
            power, prov = self._read()
            settled = prov.lower() in ("succeeded", "failed", "canceled", "")
            if power.lower() == want and settled:
                return
            if settled and prov.lower() != "succeeded" and requested:
                raise RuntimeError(f"AKS {verb} ended {prov} (power state {power})")
            if settled and not requested:
                r = httpx.post(self._url(f"/{verb}"), headers=self._h(), timeout=60)
                if r.status_code not in (200, 202, 204):
                    raise RuntimeError(f"AKS {verb} returned HTTP {r.status_code}: "
                                       f"{r.text[:300]}")
                requested = True
            if time.monotonic() >= deadline:
                raise RuntimeError(f"AKS {verb} did not finish within {POWER_TIMEOUT_S}s "
                                   f"(power {power}, provisioning {prov})")
            time.sleep(POLL_INTERVAL_S)

    def snapshot(self) -> dict:
        return {}          # Azure keeps the pools' configuration across a managed stop

    def stop(self, restore: dict) -> None:
        self._drive("stop", "stopped")

    def start(self, restore: dict, fallback_size: int) -> None:
        self._drive("start", "running")


#: Test seam: ``callable(row) -> backend``. None in production.
_BACKEND_FACTORY = None


async def _backend_for(db, row):
    if _BACKEND_FACTORY is not None:
        return _BACKEND_FACTORY(row)
    from ..database import Job
    from . import k8s_service
    prov = db.query(Job).filter(Job.id == row.deploy_job_id).first()
    tfv = ((prov.metadata_dict or {}).get("tf_variables") or {}) if prov else {}
    if row.cloud == "gcp":
        name = tfv.get("cluster_name") or k8s_service._gke_name(f"k8s-{row.name}")
        project = (tfv.get("project") or k8s_service._cfg("gcp_project")
                   or k8s_service._cfg("gcp_project_id"))
        if not project:
            raise RuntimeError("gcp_project is not configured")
        return _GkeBackend(project, name, tfv.get("zone") or "")
    if row.cloud == "aws":
        name = tfv.get("cluster_name") or k8s_service._eks_name(f"k8s-{row.name}")
        return _EksBackend(name, row.region or tfv.get("region") or k8s_service._cfg("aws_region"))
    if row.cloud == "azure":
        from . import azure_service
        credential, sub = await azure_service._ensure_creds()
        name = tfv.get("cluster_name") or k8s_service._eks_name(f"k8s-{row.name}")
        rg = tfv.get("resource_group_name") or "vm-cli-rg"
        return _AksBackend(azure_service.aks_cluster_resource_id(sub, rg, name), credential)
    raise PowerUnsupported(f"no power backend for cloud {row.cloud!r}")


def _fallback_size(db, row) -> int:
    """Nodes per pool for a start with nothing recorded to restore: what Terraform was
    asked for, else 1."""
    from ..database import Job
    try:
        prov = db.query(Job).filter(Job.id == row.deploy_job_id).first()
        tfv = ((prov.metadata_dict or {}).get("tf_variables") or {}) if prov else {}
        return max(1, int(tfv.get("node_count") or tfv.get("node_desired") or 1))
    except Exception:  # noqa: BLE001
        return 1


# ── the job ──────────────────────────────────────────────────────────────────

async def run_power(db, *, cluster_id: str, job_id: str, action: str) -> None:
    """Worker body of a ``k8s_power`` job; owns its own completion and never raises."""
    from ..database import K8sCluster
    from . import job_service
    from .k8s_service import _to_thread

    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        job_service.set_failed(db, job_id, f"cluster {cluster_id} not found")
        return
    job_service.set_running(db, job_id)
    state = read_state(row)
    prior = state.get("prior_status") or (STATUS_STOPPED if action == "start"
                                          else "registered")
    try:
        if action not in ACTIONS:
            raise PowerUnsupported(f"unknown power action {action!r}")
        reason = unsupported_reason(row)
        if reason:
            raise PowerUnsupported(reason)
        backend = await _backend_for(db, row)
        if action == "stop":
            job_service.update_progress(db, job_id, 10, "Recording node pool sizes…")
            snap = await _to_thread(backend.snapshot)
            restore = merge_restore(state.get("restore") or {}, snap)
            # Committed BEFORE the first pool is touched: a stop that dies halfway must
            # still know what to put back.
            st = read_state(row)
            st["restore"] = restore
            _write_state(row, st)
            db.commit()
            job_service.update_progress(db, job_id, 30, "Stopping the cluster…")
            await _to_thread(backend.stop, restore)
        else:
            job_service.update_progress(db, job_id, 20, "Starting the cluster…")
            await _to_thread(backend.start, state.get("restore") or {},
                             _fallback_size(db, row))
    except Exception as exc:  # noqa: BLE001 - recorded on the row and the job
        db.rollback()
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is not None:
            st = read_state(row)
            st.update(last_error=str(exc)[:500], job_id=job_id, action=action)
            _write_state(row, st)
            if (row.status or "") in TRANSITIONAL:
                row.status = prior
            db.commit()
        job_service.set_failed(db, job_id, f"cluster {action} failed: {exc}")
        logger.warning("k8s power %s cluster_id=%s failed: %s", action, cluster_id, exc)
        return

    db.refresh(row)
    st = read_state(row)
    st.pop("last_error", None)
    st.update(job_id=job_id, action=action)
    if action == "stop":
        st.update(state="stopped", stopped_at=_now().isoformat())
        row.status = STATUS_STOPPED
    else:
        st.update(state="running", started_at=_now().isoformat())
        st.pop("stopped_at", None)
        st.pop("restore", None)       # put back; the next stop snapshots afresh
        row.status = st.get("resume_status") or "registered"
    st.pop("prior_status", None)
    _write_state(row, st)
    db.commit()
    job_service.set_completed(db, job_id, {"cluster_id": cluster_id, "action": action,
                                           "status": row.status})
    logger.info("k8s power %s cluster_id=%s -> %s", action, cluster_id, row.status)


async def ensure_running_for_teardown(db, row, job_id: str) -> Optional[str]:
    """Start a stopped AKS cluster before a decommission.

    The decommission's first steps revoke in-cluster ServiceAccounts and bindings
    through the API server (Password Safe token, PRA tunnel, Entra binding). A stopped
    AKS cluster has no API server, so each of those would fail and leave the row
    ``failed``. GKE and EKS keep their control plane when stopped -- only the nodes are
    gone, and none of those steps needs a node -- so they are destroyed as they are.

    Returns a warning when the start failed (the teardown still runs), else None.
    """
    from . import job_service
    from .k8s_service import _to_thread
    if read_state(row).get("state") != "stopped" or row.cloud != "azure":
        return None
    if unsupported_reason(row):
        return None
    job_service.update_progress(db, job_id, 10,
                                "Starting the stopped AKS cluster so it can be torn down…")
    try:
        backend = await _backend_for(db, row)
        await _to_thread(backend.start, {}, 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("k8s decommission: start-before-teardown for %s failed: %s",
                       row.id, exc)
        return f"starting the stopped AKS cluster before teardown failed: {exc}"
    st = read_state(row)
    st.update(state="running", started_at=_now().isoformat())
    _write_state(row, st)
    db.commit()
    return None
