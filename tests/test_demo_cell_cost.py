"""What a demo cell costs, as the Demo hub shows it.

What this file holds in place:

  * **The arithmetic.** Running spans at the running rate, stopped spans at the disk-only
    rate, split on the dashboard's own power jobs; a destroyed VM stops accruing.
  * **What a cell is made of.** An OT cell includes its DMZ broker; a custom cell sums its VM
    members; an agent cell builds nothing and says so.
  * **What cannot be priced is listed, never counted as $0.** Clusters, databases, and a VM
    in a region the price catalogue cannot reach.
  * **A capped VM reports its accrued figure,** so the hub agrees with its spend panel.
  * **Nothing is priced that the list hides.** /costs and /summary read one helper.
  * **One broken cell never blanks the others.**

The price itself is stubbed: ``rate_for`` is passed in, and no cloud is touched.

Runs under pytest, or standalone:
    python tests/test_demo_cell_cost.py
"""
import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="demo-cell-cost-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-demo-cell-cost")

from web_dashboard.database import (AgentCell, Base, CustomCell,  # noqa: E402
                                    CloudDatabase, CustomCellMember, Job, K8sCluster,
                                    SessionLocal, User, engine)
from web_dashboard.services import demo_cell_cost as cost  # noqa: E402
from web_dashboard.services import role_service, spend_sweeper  # noqa: E402

Base.metadata.create_all(bind=engine)

NOW = datetime(2026, 11, 14, 12, 0, 0)
ON, OFF = 0.50, 0.05


def _rate(row, meta, *, running):
    """$0.50/h running, $0.05/h stopped -- except in a region with no price source."""
    if meta.get("region") == "nowhere-1":
        return None
    return ON if running else OFF


def _clean(db):
    for model in (Job, CustomCellMember, CustomCell, AgentCell, K8sCluster, CloudDatabase):
        db.query(model).delete()
    db.commit()


def _vm(db, *, hours_up=3.0, meta=None, workgroup="demo", job_type="gce_deploy", **kw):
    up = NOW - timedelta(hours=hours_up)
    j = Job(id=str(uuid.uuid4()), job_type=job_type, workgroup=workgroup,
            status="completed", created_by="someone", created_at=up, completed_at=up, **kw)
    j.metadata_dict = {"instance_name": "vm-" + j.id[:4], "zone": "us-central1-a",
                       **(meta or {})}
    db.add(j)
    return j


def _power(db, deploy, action, at):
    j = Job(id=str(uuid.uuid4()), job_type="gce_power", status="completed",
            created_by="someone", created_at=at, completed_at=at)
    j.metadata_dict = {"deploy_job_id": deploy.id, "action": action}
    db.add(j)
    return j


NODE = {"e2-standard-2": 0.08, "e2-standard-8": 0.30, "t3.small": 0.02}


def _spec(cloud, meta, *, running):
    """A node's rate by size; no price source in nowhere-1."""
    if meta.get("region") == "nowhere-1":
        return None
    return NODE.get(meta.get("instance_type"), 0.10) if running else 0.0


def _dbr(cloud, region, spec):
    """A database: $0.05/h compute and $0.01/h storage; no price in nowhere-1."""
    return None if region == "nowhere-1" else (0.05, 0.01)


def _cost(db, kind, cell):
    return cost.cell_cost(db, kind, cell, now=NOW, rate_for=_rate, rate_spec=_spec,
                          db_rates=_dbr)


def _close(a, b):
    return a is not None and abs(a - b) < 0.011


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_a_running_vm_costs_its_rate_times_its_hours():
    db = SessionLocal()
    try:
        _clean(db)
        vm = _vm(db, hours_up=3, meta={"netcell": True})
        db.commit()
        c = _cost(db, "net", {"id": vm.id})
        assert c["rate_usd_h"] == ON
        assert _close(c["so_far_usd"], 1.50), c
        assert c["to_end_usd"] is None, "a cell with no end has no 'by end' figure"
    finally:
        db.close()


def test_a_stopped_span_is_billed_at_the_disk_only_rate():
    db = SessionLocal()
    try:
        _clean(db)
        vm = _vm(db, hours_up=4, meta={"netcell": True})
        _power(db, vm, "stop", NOW - timedelta(hours=3))
        _power(db, vm, "start", NOW - timedelta(hours=1))
        db.commit()
        c = _cost(db, "net", {"id": vm.id})
        # 1 h on, 2 h off, 1 h on.
        assert _close(c["so_far_usd"], 2 * ON + 2 * OFF), c
        assert c["rate_usd_h"] == ON
        _power(db, vm, "stop", NOW - timedelta(minutes=30))
        db.commit()
        c = _cost(db, "net", {"id": vm.id})
        assert c["rate_usd_h"] == OFF, "a stopped VM's rate now is its disk"
    finally:
        db.close()


def test_a_destroyed_vm_stops_accruing():
    db = SessionLocal()
    try:
        _clean(db)
        vm = _vm(db, hours_up=5, meta={"destroyed": True},
                 updated_at=NOW - timedelta(hours=3))
        db.commit()
        assert _close(cost.vm_cost(vm, [], rate_running=ON, rate_stopped=OFF, now=NOW), 1.0)
    finally:
        db.close()


def test_by_end_holds_the_rate_now_until_the_end():
    db = SessionLocal()
    try:
        _clean(db)
        vm = _vm(db, hours_up=2, meta={"netcell": True})
        db.commit()
        end = (NOW + timedelta(hours=4)).isoformat()
        c = _cost(db, "net", {"id": vm.id, "end_at": end})
        assert _close(c["to_end_usd"], 1.00 + 4 * ON), c
    finally:
        db.close()


def test_a_capped_vm_reports_its_accrued_figure():
    db = SessionLocal()
    try:
        _clean(db)
        vm = _vm(db, hours_up=3, meta={"netcell": True}, spend_cap_usd=50.0,
                 spend_estimate_usd=7.25, spend_accrued_at=NOW - timedelta(minutes=5))
        db.commit()
        assert _cost(db, "net", {"id": vm.id})["so_far_usd"] == 7.25, (
            "the hub disagrees with the VM's own spend panel")
    finally:
        db.close()


# ── what a cell is made of ───────────────────────────────────────────────────

def test_an_ot_cell_includes_its_broker():
    db = SessionLocal()
    try:
        _clean(db)
        broker = _vm(db, hours_up=2, meta={"ot_broker": True})
        cell = _vm(db, hours_up=2, meta={"ot_cell": True, "ot_broker_job_id": broker.id})
        db.commit()
        c = _cost(db, "ot", {"id": cell.id})
        assert c["priced"] == 2 and c["rate_usd_h"] == 2 * ON
        assert _close(c["so_far_usd"], 2.0)
    finally:
        db.close()


def test_a_custom_cell_sums_its_vms_and_lists_the_rest():
    db = SessionLocal()
    try:
        _clean(db)
        cell = CustomCell(id=str(uuid.uuid4()), name="cc", status="active", workgroup="demo")
        db.add(cell)
        for n in range(2):
            vm = _vm(db, hours_up=1, meta={"custom_cell_id": cell.id})
            db.add(CustomCellMember(cell_id=cell.id, kind="vm", cloud="gcp",
                                    label=f"web-{n}", ref_id=vm.id, status="active"))
        db.add(CustomCellMember(cell_id=cell.id, kind="k8s", cloud="gcp", label="gke",
                                ref_id=str(uuid.uuid4()), status="active"))
        db.add(CustomCellMember(cell_id=cell.id, kind="database", cloud="gcp", label="db",
                                ref_id=str(uuid.uuid4()), status="active"))
        db.add(CustomCellMember(cell_id=cell.id, kind="database", cloud="gcp", label="old",
                                status="destroyed"))
        db.commit()
        c = _cost(db, "custom", {"id": cell.id})
        assert c["priced"] == 2 and _close(c["so_far_usd"], 1.0)
        assert sorted(u["name"] for u in c["unpriced"]) == ["db", "gke"], c["unpriced"]
        reasons = {u["name"]: u["reason"] for u in c["unpriced"]}
        assert reasons == {"db": cost.NO_DB_ROW, "gke": cost.NO_CLUSTER_ROW}, reasons
    finally:
        db.close()


def _cluster_cell(db, *, cloud="gcp", tfv=None, status="registered", source="provisioned",
                  region="us-central1", hours_up=2.0):
    cell = CustomCell(id=str(uuid.uuid4()), name="kc", status="active", workgroup="demo")
    db.add(cell)
    prov = Job(id=str(uuid.uuid4()), job_type="k8s_provision", status="completed",
               created_by="someone")
    prov.metadata_dict = {"tf_variables": tfv or {}}
    db.add(prov)
    cl = K8sCluster(id=str(uuid.uuid4()), cloud=cloud, name="gke-1", status=status,
                    source=source, region=region, deploy_job_id=prov.id,
                    created_at=NOW - timedelta(hours=hours_up))
    db.add(cl)
    db.add(CustomCellMember(cell_id=cell.id, kind="k8s", cloud=cloud, label="gke",
                            ref_id=cl.id, status="active"))
    db.commit()
    return cell, cl


def test_a_cluster_is_priced_as_its_nodes():
    db = SessionLocal()
    try:
        _clean(db)
        cell, _ = _cluster_cell(db, tfv={"machine_type": "e2-standard-8", "node_count": 4})
        c = _cost(db, "custom", {"id": cell.id})
        plane = cost.CONTROL_PLANE_DEFAULTS["gcp"]
        assert c["priced"] == 1 and _close(c["rate_usd_h"], 4 * 0.30 + plane), c
        assert _close(c["so_far_usd"], 2 * (4 * 0.30 + plane)), c
        assert c["unpriced"] == [], "the control plane is priced now, not left out"
    finally:
        db.close()


def test_a_cluster_with_nothing_recorded_is_priced_at_the_modules_defaults():
    db = SessionLocal()
    try:
        _clean(db)
        cell, _ = _cluster_cell(db, tfv={})
        assert _close(_cost(db, "custom", {"id": cell.id})["rate_usd_h"], 3 * 0.08 + 0.10), (
            "GKE's module builds three e2-standard-2 nodes, plus the control plane")
        cell, _ = _cluster_cell(db, cloud="aws", tfv={}, region="us-east-1")
        assert _close(_cost(db, "custom", {"id": cell.id})["rate_usd_h"], 2 * 0.02 + 0.10), (
            "EKS's module builds two t3.small nodes, plus the control plane")
    finally:
        db.close()


def test_a_stopped_clusters_nodes_cost_nothing():
    from web_dashboard.services import k8s_power
    db = SessionLocal()
    try:
        _clean(db)
        cell, cl = _cluster_cell(db, tfv={"node_count": 2}, status=k8s_power.STATUS_STOPPED,
                                 hours_up=4)
        stop = Job(id=str(uuid.uuid4()), job_type=k8s_power.JOB_TYPE, status="completed",
                   created_by="someone", created_at=NOW - timedelta(hours=1),
                   completed_at=NOW - timedelta(hours=1))
        stop.metadata_dict = {"cluster_id": cl.id, "action": "stop"}
        db.add(stop)
        db.commit()
        c = _cost(db, "custom", {"id": cell.id})
        assert _close(c["rate_usd_h"], 0.10), "stopped: the control plane still bills"
        assert _close(c["so_far_usd"], 3 * (2 * 0.08 + 0.10) + 1 * 0.10), c
    finally:
        db.close()


def test_a_registered_or_unpriceable_cluster_is_listed_not_priced():
    db = SessionLocal()
    try:
        _clean(db)
        cell, _ = _cluster_cell(db, source="registered")
        c = _cost(db, "custom", {"id": cell.id})
        assert c["priced"] == 0 and "registered" in c["unpriced"][0]["reason"], c
        cell, _ = _cluster_cell(db, region="nowhere-1")
        c = _cost(db, "custom", {"id": cell.id})
        assert c["priced"] == 0 and "gcp/nowhere-1" in c["unpriced"][0]["reason"], c
    finally:
        db.close()


def _db_cell(db, *, engine="postgres", status="available", source="provisioned",
             region="us-central1", hours_up=2.0):
    cell = CustomCell(id=str(uuid.uuid4()), name="dc", status="active", workgroup="demo")
    db.add(cell)
    row = CloudDatabase(id=str(uuid.uuid4()), engine=engine, cloud="gcp", region=region,
                        status=status, source=source, created_by="someone",
                        created_at=NOW - timedelta(hours=hours_up))
    db.add(row)
    prov = Job(id=str(uuid.uuid4()), job_type="clouddb_provision", status="completed",
               created_by="someone")
    prov.metadata_dict = {"db_id": row.id, "engine": engine, "cloud": "gcp",
                          "tf_variables": {"tier": "db-f1-micro", "disk_size": 20}}
    db.add(prov)
    db.add(CustomCellMember(cell_id=cell.id, kind="database", cloud="gcp", label="db",
                            ref_id=row.id, job_id=prov.id, status="ready"))
    db.commit()
    return cell, row


def test_a_database_is_priced_as_compute_plus_storage():
    db = SessionLocal()
    try:
        _clean(db)
        cell, _ = _db_cell(db)
        c = _cost(db, "custom", {"id": cell.id})
        assert c["priced"] == 1 and _close(c["rate_usd_h"], 0.06), c
        assert _close(c["so_far_usd"], 0.12), c
        cell, _ = _db_cell(db, status="stopped")
        assert _close(_cost(db, "custom", {"id": cell.id})["rate_usd_h"], 0.01), (
            "a stopped database still bills its storage, and only that")
    finally:
        db.close()


def test_an_unpriced_engine_or_a_registered_database_is_listed():
    db = SessionLocal()
    try:
        _clean(db)
        cell, _ = _db_cell(db, engine="sqlserver")
        c = _cost(db, "custom", {"id": cell.id})
        assert c["priced"] == 0 and "PostgreSQL and MySQL" in c["unpriced"][0]["reason"], c
        cell, _ = _db_cell(db, source="registered")
        c = _cost(db, "custom", {"id": cell.id})
        assert "registered" in c["unpriced"][0]["reason"], c
        cell, _ = _db_cell(db, region="nowhere-1")
        c = _cost(db, "custom", {"id": cell.id})
        assert "gcp/nowhere-1" in c["unpriced"][0]["reason"], c
    finally:
        db.close()


def test_the_control_plane_fee_is_an_admin_setting():
    from web_dashboard.services import config_service
    db = SessionLocal()
    saved = config_service.get("k8s_control_plane_usd_per_hour_gcp", "")
    try:
        _clean(db)
        cell, _ = _cluster_cell(db, tfv={"node_count": 2})
        config_service.set("k8s_control_plane_usd_per_hour_gcp", "0.25")
        assert _close(_cost(db, "custom", {"id": cell.id})["rate_usd_h"], 2 * 0.08 + 0.25)
        config_service.set("k8s_control_plane_usd_per_hour_gcp", "0")
        assert _close(_cost(db, "custom", {"id": cell.id})["rate_usd_h"], 2 * 0.08), (
            "an explicit 0 must mean free, not the default")
    finally:
        config_service.set("k8s_control_plane_usd_per_hour_gcp", saved or "")
        db.close()


def test_a_blank_control_plane_setting_saves_as_list_price():
    from web_dashboard.api.setup import CostExplorerFeatureConfig
    cfg = CostExplorerFeatureConfig(k8s_control_plane_usd_per_hour_aws="",
                                    k8s_control_plane_usd_per_hour_gcp="",
                                    k8s_control_plane_usd_per_hour_azure="")
    assert (cfg.k8s_control_plane_usd_per_hour_aws, cfg.k8s_control_plane_usd_per_hour_gcp,
            cfg.k8s_control_plane_usd_per_hour_azure) == (0.10, 0.10, 0.0), (
        "a blank panel field must keep list price, not save a free control plane")
    html = open(os.path.join(_ROOT, "web_dashboard", "templates", "settings", "panels",
                             "cost_explorer.html"), encoding="utf-8").read()
    for cloud in ("aws", "gcp", "azure"):
        assert f"panelCfg.k8s_control_plane_usd_per_hour_{cloud}" in html, cloud


def test_a_member_records_when_it_was_destroyed():
    m = CustomCellMember(cell_id="c", kind="k8s", cloud="gcp", label="g", status="ready")
    assert m.ended_at is None
    m.status = "destroying"
    assert m.ended_at is None
    m.status = "destroyed"
    first = m.ended_at
    assert first is not None
    m.status = "destroyed"
    assert m.ended_at == first, "a second write moved the end"


def test_an_agent_cell_has_no_cost_and_says_why():
    db = SessionLocal()
    try:
        c = _cost(db, "agent", {"id": "whatever"})
        assert c["rate_usd_h"] is None and c["so_far_usd"] is None
        assert c["note"] == cost.AGENT_NOTE
    finally:
        db.close()


def test_an_unpriceable_vm_is_listed_not_counted_as_zero():
    db = SessionLocal()
    try:
        _clean(db)
        broker = _vm(db, meta={"ot_broker": True, "region": "nowhere-1"})
        cell = _vm(db, hours_up=2, meta={"ot_cell": True, "ot_broker_job_id": broker.id})
        db.commit()
        c = _cost(db, "ot", {"id": cell.id})
        assert c["priced"] == 1 and c["rate_usd_h"] == ON
        assert len(c["unpriced"]) == 1 and "gcp/nowhere-1" in c["unpriced"][0]["reason"]

        lone = _vm(db, meta={"netcell": True, "region": "nowhere-1"})
        db.commit()
        c = _cost(db, "net", {"id": lone.id})
        assert c["rate_usd_h"] is None and c["so_far_usd"] is None, (
            "a cell with nothing priced read as $0")
    finally:
        db.close()


# ── the endpoint ─────────────────────────────────────────────────────────────

def test_one_broken_cell_never_blanks_the_others():
    db = SessionLocal()
    try:
        _clean(db)
        good = _vm(db, hours_up=1, meta={"netcell": True})
        db.commit()

        def _boom(row, meta, *, running):
            if row.id != good.id:
                raise RuntimeError("price list exploded")
            return _rate(row, meta, running=running)
        bad = _vm(db, meta={"netcell": True})
        db.commit()
        out = cost.price_cells(db, {"net": [{"id": bad.id}, {"id": good.id}]},
                               now=NOW, rate_for=_boom)
        assert out["cells"]["net"][bad.id]["note"] == "could not be estimated"
        assert out["cells"]["net"][good.id]["rate_usd_h"] == ON
        assert out["totals"]["net"]["rate_usd_h"] == ON
        assert out["caveat"] == cost.CAVEAT
    finally:
        db.close()


def _presenter(workgroups=("demo",)):
    spec = next(r for r in role_service._BUILTIN_ROLES if r["slug"] == "demo-presenter")
    u = User(id=str(uuid.uuid4()), username="u-" + uuid.uuid4().hex[:8], is_active=True)
    u.permissions = json.dumps(spec["permissions"])
    u.workgroups = json.dumps(list(workgroups))
    return u


def test_costs_price_exactly_the_cells_the_summary_lists():
    from web_dashboard.api import demo
    db = SessionLocal()
    saved = spend_sweeper.rate_for
    try:
        _clean(db)
        mine = _vm(db, meta={"ot_cell": True})
        theirs = _vm(db, meta={"ot_cell": True}, workgroup="ops")
        db.commit()
        spend_sweeper.rate_for = _rate
        p = _presenter()
        listed = {c["id"] for k in demo.summary(db=db, current_user=p)["cells"]
                  for c in k["cells"]}
        out = asyncio.run(demo.costs(db=db, current_user=p))
        priced = {cid for per in out["cells"].values() for cid in per}
        assert mine.id in priced and theirs.id not in priced, (
            "a cost was shown for a cell in another workstream")
        assert priced == listed, (priced, listed)
    finally:
        spend_sweeper.rate_for = saved
        db.close()


def test_the_sweep_keeps_its_old_name():
    assert spend_sweeper._rate_for is spend_sweeper.rate_for


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
