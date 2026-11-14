"""A demo presenter's monthly budget (services/demo_budget).

What this file holds in place:

  * **Whose limit:** an admin has none; a user's own budget wins (0 = none); else the
    instance default; a default of 0 means nothing changes.
  * **This month's spend:** floored at the 1st; a VM destroyed this month counts up to
    when it went, one destroyed last month does not.
  * **Committed:** a live resource's rate now, held to its cell's end or the month's end.
  * **The estimate:** an OT cell's broker is counted only when Entitle deploys one.
  * **Refused at booking:** an over-budget launch books nothing, and says every figure; an
    admin is never refused; what cannot be priced never refuses on its own.
  * **Every booking route checks it** before it creates a row.
  * **The setting and the user field round-trip.**

Prices are stubbed (``spend_sweeper.rate_for`` / ``rate_for_spec``); no cloud is touched.

Runs under pytest, or standalone:
    python tests/test_demo_budget.py
"""
import ast
import os
import sys
import uuid
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import test_custom_cell_lifecycle as life  # noqa: E402  (its fixtures and cloud stubs)

from web_dashboard.database import CustomCell, Job, User  # noqa: E402
from web_dashboard.services import config_service  # noqa: E402
from web_dashboard.services import custom_cell_service as svc  # noqa: E402
from web_dashboard.services import demo_budget as bud  # noqa: E402
from web_dashboard.services import spend_sweeper  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402

_ROOT = os.path.dirname(_HERE)
NOW = datetime(2026, 11, 14, 12, 0, 0)
VM = 0.50        # every VM, running, per hour
NODE = 0.10      # every cluster node, running, per hour


def _rate(row, meta, *, running):
    return None if meta.get("region") == "nowhere-1" else (VM if running else 0.0)


def _spec(cloud, meta, *, running):
    if meta.get("region") == "nowhere-1":
        return None
    kind_vm = meta.get("machine_type") or meta.get("vm_size")
    return (VM if kind_vm else NODE) if running else 0.0


DB_COMPUTE, DB_STORAGE = 0.05, 0.01


class _Prices:
    def __enter__(self):
        from web_dashboard.services import db_cloud_cost
        self._saved = (spend_sweeper.rate_for, spend_sweeper.rate_for_spec,
                       db_cloud_cost.rates)
        spend_sweeper.rate_for, spend_sweeper.rate_for_spec = _rate, _spec
        db_cloud_cost.rates = lambda cloud, region, spec: (DB_COMPUTE, DB_STORAGE)
        return self

    def __exit__(self, *exc):
        from web_dashboard.services import db_cloud_cost
        (spend_sweeper.rate_for, spend_sweeper.rate_for_spec,
         db_cloud_cost.rates) = self._saved


class _Default:
    def __init__(self, usd):
        self.usd = usd

    def __enter__(self):
        self._saved = config_service.get("demo_monthly_budget_usd", "")
        config_service.set("demo_monthly_budget_usd", str(self.usd))

    def __exit__(self, *exc):
        config_service.set("demo_monthly_budget_usd", self._saved or "0")


def _person(**kw):
    u = User(id=str(uuid.uuid4()), username="p-" + uuid.uuid4().hex[:6], is_active=True,
             hashed_password="x")
    for k, v in kw.items():
        setattr(u, k, v)
    return u


def _vm(db, username, *, up, meta=None, destroyed_at=None, end=None):
    j = Job(id=str(uuid.uuid4()), job_type="gce_deploy", status="completed",
            created_by=username, workgroup="demo", created_at=up, completed_at=up,
            updated_at=destroyed_at)
    m = {"ot_cell": True, "zone": "us-central1-a", "machine_type": "e2-medium", **(meta or {})}
    if destroyed_at:
        m["destroyed"] = True
    if end:
        m["demo_end_at"] = end.isoformat()
    j.metadata_dict = m
    db.add(j)
    db.commit()
    return j


# ── whose limit ──────────────────────────────────────────────────────────────

def test_whose_limit():
    with _Default(0):
        assert bud.limit_for(_person()) == (None, "none"), "a default of 0 changes nothing"
        assert bud.limit_for(_person(demo_budget_usd=25.0)) == (25.0, "personal")
    with _Default(100):
        assert bud.limit_for(_person()) == (100.0, "default")
        assert bud.limit_for(_person(demo_budget_usd=25.0)) == (25.0, "personal")
        assert bud.limit_for(_person(demo_budget_usd=0.0)) == (None, "none"), (
            "a personal 0 means no budget for that user")
        assert bud.limit_for(_person(is_admin=True)) == (None, "admin")


# ── this month ───────────────────────────────────────────────────────────────

def test_month_spend_floors_at_the_first_and_counts_what_went_this_month():
    db = life._db()
    me = "p-" + uuid.uuid4().hex[:6]
    first = datetime(2026, 11, 1)
    _vm(db, me, up=first - timedelta(hours=10))                     # 10h last month
    _vm(db, me, up=first + timedelta(hours=1), destroyed_at=first + timedelta(hours=5))
    _vm(db, me, up=first - timedelta(days=5), destroyed_at=first - timedelta(days=1))
    _vm(db, "someone-else", up=first)
    with _Prices():
        st = bud.standing(db, me, now=NOW)
    month_hours = (NOW - first).total_seconds() / 3600
    want = VM * month_hours + VM * 4      # the live one this month, the destroyed one's 4h
    assert abs(st["spent_usd"] - want) < 0.02, (st, want)


def test_committed_stops_at_the_cells_end_or_the_months():
    db = life._db()
    me = "p-" + uuid.uuid4().hex[:6]
    _vm(db, me, up=NOW, end=NOW + timedelta(hours=6))
    _vm(db, me, up=NOW)                                               # no end: to 1 Dec
    with _Prices():
        st = bud.standing(db, me, now=NOW)
    to_month_end = (datetime(2026, 12, 1) - NOW).total_seconds() / 3600
    assert abs(st["committed_usd"] - (VM * 6 + VM * to_month_end)) < 0.02, st


def _cell_with_member(db, me, *, kind, ended=None, created, tfv=None):
    from web_dashboard.database import CustomCellMember
    cell = CustomCell(id=str(uuid.uuid4()), name="m" + uuid.uuid4().hex[:5], status="active",
                      workgroup="demo", created_by=me)
    db.add(cell)
    prov = Job(id=str(uuid.uuid4()), job_type="k8s_provision" if kind == "k8s"
               else "clouddb_provision", status="completed", created_by=me)
    prov.metadata_dict = {"engine": "postgres", "cloud": "gcp",
                          "tf_variables": tfv or {"node_count": 2, "region": "us-central1"}}
    db.add(prov)
    m = CustomCellMember(cell_id=cell.id, kind=kind, cloud="gcp", label=kind,
                         ref_id=str(uuid.uuid4()), job_id=prov.id, status="ready",
                         created_at=created)
    db.add(m)
    db.commit()
    m.status = "destroyed"
    m.ended_at = ended
    db.commit()
    return m


def test_a_cluster_destroyed_this_month_still_counts():
    db = life._db()
    me = "p-" + uuid.uuid4().hex[:6]
    first = datetime(2026, 11, 1)
    _cell_with_member(db, me, kind="k8s", created=first + timedelta(hours=2),
                      ended=first + timedelta(hours=12))
    _cell_with_member(db, me, kind="k8s", created=first - timedelta(days=9),
                      ended=first - timedelta(days=2))
    with _Prices():
        st = bud.standing(db, me, now=NOW)
    plane = 0.10
    want = 10 * (2 * NODE + plane)        # ten hours, two nodes and a control plane
    assert abs(st["spent_usd"] - want) < 0.02, (st, want)
    assert st["committed_usd"] == 0.0, "a destroyed cluster committed money"


def test_a_destroyed_database_counts_from_its_provisioning_request():
    db = life._db()
    me = "p-" + uuid.uuid4().hex[:6]
    first = datetime(2026, 11, 1)
    _cell_with_member(db, me, kind="database", created=first + timedelta(hours=1),
                      ended=first + timedelta(hours=5),
                      tfv={"tier": "db-f1-micro", "region": "us-central1"})
    with _Prices():
        st = bud.standing(db, me, now=NOW)
    assert abs(st["spent_usd"] - 4 * (DB_COMPUTE + DB_STORAGE)) < 0.02, st


def test_a_booking_counts_its_database_and_control_plane():
    specs, _ = bud.specs_for_items([life.GKE, life.MYSQL])
    with _Prices():
        est, unpriced = bud.estimate(specs, start=NOW, end=NOW + timedelta(hours=10), now=NOW)
    want = 10 * (2 * NODE + 0.10 + DB_COMPUTE + DB_STORAGE)
    assert abs(est - want) < 0.02 and unpriced == [], (est, want, unpriced)


# ── a booking ────────────────────────────────────────────────────────────────

def test_a_booking_over_budget_is_refused_with_every_figure():
    db = life._db()
    u = _person(demo_budget_usd=9.0)
    _vm(db, u.username, up=NOW - timedelta(hours=8), end=NOW + timedelta(hours=4))
    with _Prices():
        try:
            bud.check_booking(db, u, [("gcp", {"zone": "us-central1-a",
                                               "machine_type": "e2-medium"}, 1)],
                              start=None, end=NOW + timedelta(hours=8), now=NOW)
            raise AssertionError("an over-budget booking was let through")
        except bud.BudgetExceeded as exc:
            msg = str(exc)
    for figure in ("$4.00 spent", "$2.00 committed", "$4.00 for this cell", "$9.00", "$10.00"):
        assert figure in msg, (figure, msg)
    with _Prices():   # the same booking, half as long, fits
        bud.check_booking(db, u, [("gcp", {"machine_type": "e2-medium"}, 1)],
                          end=NOW + timedelta(hours=3), now=NOW)


def test_an_admin_and_an_unpriceable_booking_are_never_refused():
    db = life._db()
    with _Prices():
        bud.check_booking(db, _person(is_admin=True, demo_budget_usd=0.01),
                          [("gcp", {"machine_type": "huge"}, 50)],
                          end=NOW + timedelta(days=20), now=NOW)
        bud.check_booking(db, _person(demo_budget_usd=1.0),
                          [("gcp", {"region": "nowhere-1", "machine_type": "x"}, 9)],
                          end=NOW + timedelta(days=20), now=NOW)


def test_a_custom_launch_over_budget_books_nothing():
    db = life._db()
    me = life._user(db, "demo-presenter")
    me.demo_budget_usd = 1.0
    db.commit()
    before = db.query(CustomCell).count()
    with _Prices():
        try:
            life._launch(db, me, life._bp(db, items=(life.UBUNTU, life.GKE)), ttl=8)
            raise AssertionError("an over-budget custom cell was booked")
        except svc.CellError as exc:
            assert "over your monthly budget" in str(exc), exc
    db.rollback()
    assert db.query(CustomCell).count() == before
    assert not life.CALLS["created"], "a refused booking built a member"


def test_a_custom_launch_prices_clusters_as_nodes():
    specs, unpriced = bud.specs_for_items([life.GKE, life.MYSQL, life.UBUNTU])
    kinds = sorted((m.get("_kind") or "vm", n) for _c, m, n in specs)
    # 2 nodes and 2 VMs (both "vm"-priced), one control plane, one database.
    assert kinds == [("control_plane", 1), ("database", 1), ("vm", 2), ("vm", 2)], specs
    assert unpriced == [], unpriced


def test_a_custom_launch_under_budget_books():
    db = life._db()
    me = life._user(db, "demo-presenter")
    me.demo_budget_usd = 500.0
    db.commit()
    with _Prices():
        cell = life._launch(db, me, life._bp(db, items=(life.UBUNTU,)), ttl=4)
    assert cell.id


# ── every booking route checks, before any row ───────────────────────────────

def _route_checks_before_creating(path, fn_name):
    src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name)
    body = ast.get_source_segment(src, fn) or ""
    assert "demo_budget.enforce_" in body, f"{path}:{fn_name} never checks the budget"
    assert body.index("demo_schedule.plan(") < body.index("demo_budget.enforce_") < \
        body.index("job_service.create_job("), (
        f"{path}:{fn_name} must check the budget after planning and before any row")


def test_every_cell_route_checks_the_budget_first():
    for fn in ("deploy_cell", "deploy_cell_aws", "deploy_cell_azure"):
        _route_checks_before_creating("web_dashboard/api/ot.py", fn)
    _route_checks_before_creating("web_dashboard/api/netcell.py", "deploy_cell")
    src = open(os.path.join(_ROOT, "web_dashboard/services/custom_cell_service.py"),
               encoding="utf-8").read()
    body = src[src.index("async def launch("):src.index("\ndef ", src.index("async def launch("))]
    assert body.index("check_booking_async") < body.index("cell = CustomCell("), (
        "launch must check the budget before the cell row exists")


def test_the_ot_estimate_counts_the_broker_only_with_entitle():
    src = open(os.path.join(_ROOT, "web_dashboard/api/ot.py"), encoding="utf-8").read()
    for broker in ("broker_machine_type", "broker_instance_type", "broker_vm_size"):
        i = src.index(f"payload.{broker}", src.index("demo_budget"))
        assert "if payload.register_in_entitle:" in src[i - 300:i], broker


# ── the hub, the setting, the user field ─────────────────────────────────────

def test_the_hub_shows_the_callers_budget():
    db = life._db()
    me = life._user(db, "demo-presenter")
    me.demo_budget_usd = 50.0
    db.commit()
    with _Prices():
        line = bud.summary(db, me, now=utcnow())
    assert line["limit_usd"] == 50.0 and line["source"] == "personal"
    assert bud.summary(db, _person(is_admin=True)) is None


def test_the_setting_is_a_float_the_panel_can_save_blank():
    from web_dashboard.api.setup import CostExplorerFeatureConfig
    assert CostExplorerFeatureConfig(demo_monthly_budget_usd="").demo_monthly_budget_usd == 0.0
    html = open(os.path.join(_ROOT, "web_dashboard/templates/settings/panels/cost_explorer.html"),
                encoding="utf-8").read()
    assert "panelCfg.demo_monthly_budget_usd" in html


def test_an_admin_sets_and_clears_a_users_budget():
    from web_dashboard.api import users as api
    db = life._db()
    admin = life._user(db, "administrator")
    admin.is_admin = True
    target = life._user(db, "demo-presenter")
    db.commit()
    out = api.update_user(target.id, api.UserUpdateRequest(demo_budget_usd=30), db=db,
                          admin=admin)
    assert out.demo_budget_usd == 30.0
    out = api.update_user(target.id, api.UserUpdateRequest(full_name="x"), db=db, admin=admin)
    assert out.demo_budget_usd == 30.0, "a request that leaves it out must not clear it"
    out = api.update_user(target.id, api.UserUpdateRequest(demo_budget_usd=None), db=db,
                          admin=admin)
    assert out.demo_budget_usd is None, "an explicit null hands them back to the default"
    try:
        api.UserUpdateRequest(demo_budget_usd=-1)
        raise AssertionError("a negative budget was accepted")
    except ValueError:
        pass


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
