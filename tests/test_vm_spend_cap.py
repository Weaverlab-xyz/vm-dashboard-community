"""Per-VM spend caps: the accrual, the latches, and the refusal that makes it honest.

Phase 2 of the audit's Recommendation 4. `spend_policy` (promoted from `pov_spend`,
unchanged arithmetic) answers how much has accrued and what a row has newly reached;
`pov_cloud_cost` answers the rate; `spend_sweeper` walks the capped rows and enqueues the
same `*_power` job the Suspend button makes.

**The test this file exists for is the refusal.** `accrue()` treats a missing rate as *move
the clock on, bill nothing* — right for an interval where the price API was briefly
unreachable, and silence for a VM whose region has no price source at all. Stored anyway,
such a cap accrues zero forever: the operator reads `$0.00 of $500.00`, concludes they are
protected, and is not. The audit named this exactly — *"an estate VM in a region outside
that map gets no price, therefore no accrual, therefore a cap that lies"* — so a cap that
cannot fire must be refused at the door, and one that stops being priceable later must be
reported rather than quietly accrued at zero.

The rest is the arming and latching discipline this codebase applies to anything that acts
on its own: a never-measured row bills nothing, a warned row does not re-warn, a capped row
does not re-suspend, and raising the cap clears both latches.

Run: python tests/test_vm_spend_cap.py   (or under pytest)
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="vm-spend-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-vm-spend-tests")

try:
    from web_dashboard.database import Base, Job, SessionLocal, engine
    from web_dashboard.services import (job_service, pov_cloud_cost, spend_policy,
                                        spend_sweeper, vm_suspend_policy)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

AWS_META = {"instance_id": "i-1", "region": "us-east-1", "instance_type": "t3.medium",
            "private_ip": "10.0.0.4", "disk_size_gb": 30}


def _priceable_only(*allowed):
    """A stand-in for pov_cloud_cost.priceable that needs no credentials."""
    return lambda cloud, region: (cloud, region) in allowed


# ── The refusal: a cap that cannot fire is never stored ───────────────────────

def test_a_cap_is_refused_where_there_is_no_price_source():
    """The test this file exists for. Without it the operator is told nothing and is not
    protected — the audit's "a cap that lies"."""
    ok, reason = spend_policy.cappable(
        "aws", "eu-north-1", priceable=_priceable_only(("aws", "us-east-1")))
    assert not ok
    # The reason has to be actionable: which cloud, which region, and what would fix it.
    assert "eu-north-1" in reason and "AWS" in reason, reason
    assert "pricing:GetProducts" in reason, reason

    ok, _ = spend_policy.cappable(
        "aws", "us-east-1", priceable=_priceable_only(("aws", "us-east-1")))
    assert ok


def test_a_cap_is_refused_when_the_row_records_no_region():
    """A price is per-region. A row with no region cannot be priced in any of them, and
    guessing a default would accrue somebody else's prices against this VM."""
    ok, reason = spend_policy.cappable("aws", "", priceable=lambda c, r: True)
    assert not ok and "region" in reason.lower(), reason


def test_a_suspend_action_cap_is_refused_on_a_vm_that_cannot_be_suspended():
    """A cap set to suspend an unpinned Azure VM promises something the sweep cannot do.
    Under `warn` the same cap is fine — it still warns — so the check is conditional."""
    unsuspendable = vm_suspend_policy.schedulable(
        "azure_deploy", {"vm_name": "az-1", "private_ip": "10.0.0.6"})
    assert unsuspendable[0] is False, "fixture must be a VM suspend refuses"

    ok, reason = spend_policy.cappable("azure", "eastus", priceable=lambda c, r: True,
                                       suspendable=unsuspendable)
    assert not ok and "cannot act on this VM" in reason, reason

    # Same VM, default (warn) action: accepted, because a warning does work.
    ok, _ = spend_policy.cappable("azure", "eastus", priceable=lambda c, r: True)
    assert ok


def test_the_api_asks_cappable_before_it_writes_the_cap():
    """Order is the whole point: validated, then checked against a price source, and only
    then stored. A cap written first and checked later is a cap that lies."""
    src = open(os.path.join(_ROOT, "web_dashboard/api/spend.py"), encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src))
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == "set_cap")
    checked = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "_cappable"]
    stored = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
              for t in n.targets
              if isinstance(t, ast.Attribute) and t.attr == "spend_cap_usd"]
    assert checked and stored, (checked, stored)
    assert checked[0].lineno < stored[0].lineno, "the cap is stored before it is checked"


# ── Coverage: the region map is no longer the boundary ────────────────────────

def test_aws_region_names_resolve_beyond_the_static_map():
    """Thirteen hardcoded regions was a fine bound for a cost ESTIMATE — a missing price
    showed a footprint without a number. It is not a fine bound for a cap. `_location_for`
    falls back to AWS's own public parameter store."""
    import inspect
    src = inspect.getsource(pov_cloud_cost._location_for)
    assert "global-infrastructure" in src and "longName" in src, src
    assert "get_ssm_parameter_sync" in src, "must use the public sync helper"
    # The static map still answers first, so the common path makes no call at all.
    assert pov_cloud_cost._location_for("us-east-1") == "US East (N. Virginia)"
    # And an unresolvable region is "" rather than a guess.
    import time
    pov_cloud_cost._resolved_locations["zz-nowhere-1"] = (time.time(), "")
    assert pov_cloud_cost._location_for("zz-nowhere-1") == ""
    assert pov_cloud_cost.priceable("aws", "zz-nowhere-1") is False


def test_a_transient_lookup_failure_is_not_cached_forever():
    """A resolved name is permanent reference data; a FAILURE is not. SSM briefly
    unreachable, or a credential rotating, must not turn into "this region can never be
    capped" until the process restarts."""
    import time
    calls = []

    def _resolver(region, name):
        calls.append(region)
        return "Resolved Name"

    from web_dashboard.services import aws_service
    saved = aws_service.get_ssm_parameter_sync
    aws_service.get_ssm_parameter_sync = _resolver
    try:
        # A stale failure is retried…
        pov_cloud_cost._resolved_locations["zz-stale"] = (
            time.time() - pov_cloud_cost._NEGATIVE_TTL_S - 1, "")
        assert pov_cloud_cost._location_for("zz-stale") == "Resolved Name"
        assert calls == ["us-east-1"], calls

        # …a fresh one is not, so a dead region is not re-asked every sweep.
        pov_cloud_cost._resolved_locations["zz-fresh"] = (time.time(), "")
        assert pov_cloud_cost._location_for("zz-fresh") == ""
        assert len(calls) == 1, calls

        # …and a resolved name never expires.
        pov_cloud_cost._resolved_locations["zz-old"] = (0, "Ancient Name")
        assert pov_cloud_cost._location_for("zz-old") == "Ancient Name"
        assert len(calls) == 1, "a known name must not be re-resolved"
    finally:
        aws_service.get_ssm_parameter_sync = saved
        for k in ("zz-stale", "zz-fresh", "zz-old"):
            pov_cloud_cost._resolved_locations.pop(k, None)


def test_the_price_lookups_and_the_gate_share_one_resolver():
    """If `priceable` said yes and the lookup then used a different map, a cap would be
    accepted and accrue nothing — the exact failure, arriving one layer down."""
    src = open(os.path.join(_ROOT, "web_dashboard/services/pov_cloud_cost.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    for name in ("instance_hourly", "storage_gb_month", "priceable"):
        fn = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                  and f.name == name)
        calls = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)}
        assert "_location_for" in calls, f"{name} must resolve through _location_for"
    # And no site reads the raw map any more.
    assert "_LOCATIONS.get(" not in src, "a direct map read bypasses the resolver"


# ── Arming and latching ───────────────────────────────────────────────────────

def test_the_first_pass_measures_but_never_bills():
    """A NULL accrued_at means "never measured". Billing an unbounded interval would charge
    this VM for every hour since the epoch — the same arming rule the schedule latch and
    the auto-delete timer both use."""
    now = datetime.now(timezone.utc)
    total, at, added = spend_policy.accrue(None, None, 1.0, now)
    assert (total, added) == (0.0, 0.0) and at == now

    total, at, added = spend_policy.accrue(0.0, now - timedelta(hours=2), 1.0, now)
    assert added == 2.0 and total == 2.0


def test_a_long_outage_cannot_invent_a_bill_big_enough_to_trip_every_cap():
    now = datetime.now(timezone.utc)
    _total, _at, added = spend_policy.accrue(
        0.0, now - timedelta(days=30), 1.0, now)
    assert added == spend_policy.MAX_ACCRUAL_HOURS


def test_an_unpriceable_interval_moves_the_clock_without_billing():
    """So a rate that appears later does not then charge for the blind period."""
    now = datetime.now(timezone.utc)
    total, at, added = spend_policy.accrue(5.0, now - timedelta(hours=3), None, now)
    assert (total, added) == (5.0, 0.0)
    assert at == now, "the clock must still move on"


def test_the_latches_fire_once_and_a_raised_cap_clears_them():
    class _Row:
        spend_cap_usd = 100.0
        spend_estimate_usd = 85.0
        spend_warned_at = None
        spend_capped_at = None

    row = _Row()
    assert spend_policy.state(row) == "warn"
    row.spend_warned_at = datetime.utcnow()
    assert spend_policy.state(row) == "", "a warned row must not re-warn"

    row.spend_estimate_usd = 120.0
    assert spend_policy.state(row) == "cap"
    row.spend_capped_at = datetime.utcnow()
    assert spend_policy.state(row) == "", "a capped row must not re-suspend every pass"


# ── The sweep ─────────────────────────────────────────────────────────────────

def _vm(job_id, job_type="ec2_deploy", meta=None, **cols):
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.id == job_id).delete()
        job = Job(id=job_id, job_type=job_type, status="completed", created_by="alice",
                  workgroup="team-a", extra_data=json.dumps(meta or AWS_META),
                  created_at=datetime.utcnow(), completed_at=datetime.utcnow())
        for k, v in cols.items():
            setattr(job, k, v)
        db.add(job)
        db.commit()
    finally:
        db.close()


def _reset():
    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.commit()
    finally:
        db.close()


def _sweep(rate=1.0, action="warn"):
    """Run one pass with the price source and config stubbed."""
    db = SessionLocal()
    saved = (spend_sweeper._rate_for, spend_sweeper.action, spend_sweeper.warn_percent)
    spend_sweeper._rate_for = lambda row, meta, *, running: rate
    spend_sweeper.action = lambda: action
    spend_sweeper.warn_percent = lambda: 80
    try:
        job = job_service.create_job(db, job_type="spend_sweep", created_by="system")
        asyncio.run(spend_sweeper.run(db, job_id=job.id, meta={}))
        db.refresh(job)
        return job.metadata_dict
    finally:
        (spend_sweeper._rate_for, spend_sweeper.action,
         spend_sweeper.warn_percent) = saved
        db.close()


def test_the_sweep_selects_only_capped_rows():
    """The hot-table guard. `jobs` is polled by _claim_one every two seconds, so an estate
    that has set no caps must do no writes at all."""
    _reset()
    _vm("uncapped")
    _vm("capped", spend_cap_usd=100.0)
    db = SessionLocal()
    try:
        assert [r.id for r in spend_sweeper.capped_vms(db)] == ["capped"]
    finally:
        db.close()

    src = open(os.path.join(_ROOT, "web_dashboard/services/spend_sweeper.py"),
               encoding="utf-8").read()
    assert "spend_cap_usd.isnot(None)" in src, "the filter is what keeps this off the table"


def test_a_capped_vm_accrues_and_then_warns_once():
    _reset()
    _vm("v1", spend_cap_usd=100.0, spend_estimate_usd=0.0,
        spend_accrued_at=datetime.utcnow() - timedelta(hours=85))
    out = _sweep(rate=1.0)
    assert out["accrued"] == 1
    # MAX_ACCRUAL_HOURS bounds the step at 24, which is 24% of the cap — under the 80%
    # threshold, so nothing is reached yet.
    assert out["acted"] == [], out

    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.id == "v1").first()
        assert row.spend_estimate_usd == 24.0
        assert row.spend_accrued_at is not None, "the pass must stamp the clock"
    finally:
        db.close()

    # Push it over the warning threshold; it warns exactly once.
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.id == "v1").first()
        row.spend_estimate_usd = 85.0
        row.spend_accrued_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    assert [a["event"] for a in _sweep(rate=0.0)["acted"]] == ["warn"]
    assert _sweep(rate=0.0)["acted"] == [], "a warned row must not warn again"


def test_warn_is_the_default_and_suspends_nothing():
    _reset()
    _vm("v1", spend_cap_usd=10.0, spend_estimate_usd=99.0,
        spend_accrued_at=datetime.utcnow())
    out = _sweep(rate=0.0, action="warn")
    assert [a["event"] for a in out["acted"]] == ["cap"]

    db = SessionLocal()
    try:
        assert db.query(Job).filter(Job.job_type == "ec2_power").count() == 0, \
            "warn must not enqueue a power job"
        assert db.query(Job).filter(Job.id == "v1").first().spend_capped_at is not None, \
            "the cap latches even under warn, or it repeats every pass"
    finally:
        db.close()


def test_suspend_enqueues_the_same_power_job_the_button_makes():
    _reset()
    _vm("v1", spend_cap_usd=10.0, spend_estimate_usd=99.0,
        spend_accrued_at=datetime.utcnow())
    _sweep(rate=0.0, action="suspend")

    db = SessionLocal()
    try:
        power = db.query(Job).filter(Job.job_type == "ec2_power").all()
        assert len(power) == 1, [j.job_type for j in db.query(Job).all()]
        m = power[0].metadata_dict
        # The keys api/aws's endpoint persists — a capped suspend and a button press must
        # produce identical rows, or the runner needs two code paths.
        assert m["action"] == "stop" and m["instance_id"] == "i-1"
        assert m["region"] == "us-east-1" and m["deploy_job_id"] == "v1"
        assert power[0].workgroup == "team-a"
    finally:
        db.close()


def test_a_capped_vm_that_cannot_be_suspended_is_reported_not_silently_skipped():
    """The cap was valid when set and the VM has since become unsuspendable. Saying nothing
    would look identical to a suspend that worked."""
    _reset()
    _vm("az1", job_type="azure_deploy",
        meta={"vm_name": "az-1", "region": "eastus", "private_ip": "10.0.0.6"},
        spend_cap_usd=10.0, spend_estimate_usd=99.0,
        spend_accrued_at=datetime.utcnow())
    _sweep(rate=0.0, action="suspend")

    db = SessionLocal()
    try:
        assert db.query(Job).filter(Job.job_type == "azure_power").count() == 0
        from web_dashboard.database import JobLog
        sweep = (db.query(Job).filter(Job.job_type == "spend_sweep")
                 .order_by(Job.created_at.desc()).first())
        lines = " ".join(l.line or "" for l in
                         db.query(JobLog).filter(JobLog.job_id == sweep.id).all())
        assert "cannot be suspended" in lines, lines
    finally:
        db.close()


def test_an_unpriceable_capped_vm_is_reported_every_pass():
    """The other half of the honesty requirement. A cap whose VM stops being priceable
    after it was set must not accrue zero in silence."""
    _reset()
    _vm("v1", spend_cap_usd=100.0, spend_estimate_usd=0.0,
        spend_accrued_at=datetime.utcnow() - timedelta(hours=5))
    out = _sweep(rate=None)
    assert len(out["unpriced"]) == 1, out
    assert out["unpriced"][0]["job_id"] == "v1"
    db = SessionLocal()
    try:
        assert db.query(Job).filter(Job.id == "v1").first().spend_estimate_usd == 0.0
    finally:
        db.close()


def test_a_destroyed_vm_is_not_accrued():
    _reset()
    _vm("gone", meta={**AWS_META, "destroyed": True}, spend_cap_usd=100.0,
        spend_accrued_at=datetime.utcnow() - timedelta(hours=5))
    assert _sweep(rate=1.0)["accrued"] == 0


def test_a_suspended_vm_stops_accruing_compute():
    """Compute stops billing when a VM is deallocated. A cap that kept counting it would
    defeat the suspend schedule it sits beside — suspend to save money, watch the estimate
    climb anyway."""
    _reset()
    _vm("v1", spend_cap_usd=100.0)
    db = SessionLocal()
    try:
        for jid, action in (("p1", "start"), ("p2", "stop")):
            db.add(Job(id=jid, job_type="ec2_power", status="completed",
                       created_by="system", created_at=datetime.utcnow(),
                       extra_data=json.dumps({"action": action, "deploy_job_id": "v1"})))
        db.commit()
        assert spend_sweeper.last_power_action(db)["v1"] == "stop", "newest action wins"
    finally:
        db.close()

    # With no power job at all a deployed VM counts as running — errs high, which is the
    # safe direction and the one the whole estimate already errs in.
    _reset()
    _vm("v2", spend_cap_usd=100.0)
    db = SessionLocal()
    try:
        assert spend_sweeper.last_power_action(db).get("v2") is None
    finally:
        db.close()


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_the_sweep_is_its_own_job_type_and_a_singleton():
    """Two concurrent passes would both accrue the same interval onto the same rows,
    double-billing every capped VM."""
    src = open(os.path.join(_ROOT, "web_dashboard/jobs_worker.py"), encoding="utf-8").read()
    assert '"spend_sweep"' in src
    singles = src.split("SINGLETON_TYPES = frozenset((")[1].split("))")[0]
    assert '"spend_sweep"' in singles, singles


def test_the_flag_ships_off_and_reaches_the_ui():
    from web_dashboard.config import settings
    from web_dashboard.services import feature_flags, notify_policy

    assert settings.vm_spend_cap_enabled is False, "must ship off"
    assert settings.vm_spend_cap_action == "warn", "and warn-only"
    assert "vm_spend_cap_enabled" in feature_flags.flags()
    # A flag missing here is a toggle that is permanently off however it is set.
    assert "vm_spend_cap" in feature_flags.feature_map()
    # And the events are in the default set, or a cap fires and nobody hears it.
    events = notify_policy.parse_event_types(notify_policy.DEFAULT_EVENT_TYPES)
    for e in ("vm.spend_warn", "vm.spend_capped"):
        assert e in events, e
        assert e in notify_policy.EVENT_SEVERITY, f"{e} has no severity"


def test_the_spend_api_reuses_the_suspend_apis_ownership_rule():
    """Both govern the same VMs. Two copies of a permission rule drift invisibly, and this
    one decides who may suspend somebody's infrastructure."""
    src = open(os.path.join(_ROOT, "web_dashboard/api/spend.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and (n.module or "").endswith("suspend") for a in n.names}
    assert {"_PERM_SCOPE", "_guard"} <= imported, imported
    # Asked of the AST, not the text: this module's own docstring names the rule while
    # explaining that it delegates it, and a substring scan cannot tell the two apart.
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name)}
    called |= {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)}
    assert "_assert_can_act" not in called, "ownership must be delegated, not restated"
    assert "_guard" in called, "the delegation has to actually happen"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
