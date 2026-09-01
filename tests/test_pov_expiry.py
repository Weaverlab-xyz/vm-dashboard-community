"""The POV auto-delete timer, and the warning ladder it needed.

Slice 8. A POV is the resource this feature was most obviously missing: it bills for every
VM in it, sits on `suspend_on_idle` once the evaluation ends, and nothing else in the
codebase ever notices it is finished.

The slice is deliberately NOT a second sweeper. It teaches the existing one a new kind, so
every gate stays in one place:

  * **`pov` is a reapable kind**, and the states it may be reaped in come FROM
    `pov_env_service._ACTIONABLE` rather than being retyped — the drift this codebase has
    been bitten by before.
  * **Teardown is the same `pov_env_destroy` job the DELETE endpoint creates**, so the
    share link, the Entitle integrations, the managed systems, the jump items, the Gateway
    and the broker agent are all reaped in the order that module already knows.
  * **The four gates still stand**: enabled, a stamped `expires_at` (NULL = never), the
    feature armed, and enforcement armed with report-only off.
  * **A POV warns on a LADDER**, not once. `expiry_warned_at` alone is warn-once, so with
    five rungs the first would burn it and the other four would never fire — which is what
    `warned_stage_minutes` exists for.
  * **A missed rung does not fire late.** Crossing two rungs between sweeps sends the
    tightest, not a stale one followed by a burst.
  * **An extend re-opens the whole ladder**, or every rung tighter than the one already
    sent would be permanently silenced against a deadline that no longer exists.

Uses a real SQLite database. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_expiry.py
"""
import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-expiry")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (expiry_policy as pol,  # noqa: E402
                                    expiry_reaper as reaper, inventory_service,
                                    lab_platforms, pov_env_service)

_MIN = 60.0
_HOUR = 3600.0
_DAY = 86400.0


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _env(db, **kw):
    kw.setdefault("platform_environment_id", "sky-1")
    kw.setdefault("status", pov_env_service.STATUS_ACTIVE)
    kw.setdefault("platform", "skytap")
    env = d.PovEnvironment(name=_name("poc"), **kw)
    db.add(env)
    db.commit()
    return env


def _item(**kw):
    base = {"id": "pov:x", "kind": "pov", "cloud": "skytap", "source": "provisioned",
            "name": "poc", "state": pov_env_service.STATUS_ACTIVE, "workgroup": None}
    base.update(kw)
    return base


# ── the kind ─────────────────────────────────────────────────────────────────

def test_pov_is_a_reapable_kind():
    assert "pov" in pol.REAPABLE_KINDS
    capable, why = pol.ttl_capable(_item())
    assert capable, why


def test_a_pov_skips_the_registered_and_the_cloud_tests():
    """Both questions simply do not apply: a POV row exists only because this dashboard
    created the environment, and its `cloud` is a lab platform, not one of the four VM
    clouds. Widening those tests instead would have let a registered database through."""
    capable, _ = pol.ttl_capable(_item(cloud="skytap"))
    assert capable, "a lab platform was treated as an unreapable VM cloud"
    # …while the tests it skips still hold for everything else.
    assert not pol.ttl_capable(_item(kind="vm", cloud="skytap"))[0]
    assert not pol.ttl_capable(_item(kind="database", source="registered"))[0]


def test_the_reapable_states_match_the_lifecycle_module():
    """`expiry_policy` must stay loadable by file path with no app deps, so it cannot
    import `pov_env_service` — every entry in `_REAPABLE_STATES` is a literal for that
    reason. THIS is what stops the copy drifting: `_ACTIONABLE` is the one place that
    decides which POV statuses a destroy may act on, and the reaper must not disagree."""
    assert pol._REAPABLE_STATES["pov"] == frozenset(pov_env_service._ACTIONABLE)
    # `failed` belongs in it for the reason that module gives — a POV that failed halfway
    # through provisioning is the one that most needs reaping.
    assert pov_env_service.STATUS_FAILED in pol._REAPABLE_STATES["pov"]


def test_a_pov_mid_provision_or_mid_destroy_is_never_reaped():
    """Fails safe by construction: an unrecognised status is refused, so a state added
    later can only ever make the reaper do less."""
    for state in (pov_env_service.STATUS_PROVISIONING, pov_env_service.STATUS_DESTROYING,
                  pov_env_service.STATUS_DESTROYED, "something-new"):
        assert not pol.state_is_idle(_item(state=state)), state


def _ago(**kw) -> str:
    """An `expires_at` string built the way a row's actually is.

    Both `expiry_reaper._utcnow().timestamp()` and `parse_ts` treat a naive UTC value as
    local time, so they shift by the same offset and compare correctly. Mixing in
    `utcfromtimestamp` here would apply that shift twice and put a past deadline in the
    future — a test-only trap, but one that reads exactly like a broken reaper.
    """
    return (datetime.utcnow() - timedelta(**kw)).isoformat()


def test_an_expired_pov_becomes_a_reap_target():
    now = datetime.utcnow().timestamp()
    target = pol.reap_target(
        _item(expires_at=_ago(hours=2)),
        now_ts=now, grace_min=30, armed_at_ts=now - 10 * _HOUR)
    assert target is not None and target["kind"] == "pov"
    assert target["overdue_s"] >= 2 * _HOUR - 5


def test_an_unarmed_feature_reaps_no_pov():
    """A freshly flipped toggle cannot act on an estate nobody has reviewed."""
    now = datetime.utcnow().timestamp()
    assert pol.reap_target(
        _item(expires_at=_ago(hours=5)),
        now_ts=now, grace_min=30, armed_at_ts=now - 60) is None


def test_a_pov_inside_the_grace_window_is_not_yet_reaped():
    """Never act at the instant of expiry: it absorbs clock skew between the app and the
    worker, and gives a mis-stamped environment a window of its own."""
    now = datetime.utcnow().timestamp()
    assert pol.reap_target(
        _item(expires_at=_ago(minutes=5)),
        now_ts=now, grace_min=30, armed_at_ts=now - 10 * _HOUR) is None


# ── the inventory row ────────────────────────────────────────────────────────

def test_a_pov_appears_in_the_inventory_with_its_timer():
    db = d.SessionLocal()
    when = datetime.utcnow() + timedelta(days=5)
    env = _env(db, expires_at=when, workgroup="wg1", created_by="someone")
    item = inventory_service._pov_item(env)
    assert item["id"] == f"pov:{env.id}"
    assert item["kind"] == "pov" and item["cloud"] == "skytap"
    assert item["source"] == "provisioned"
    assert item["expires_at"].startswith(when.isoformat()[:19])
    assert item["detail_href"] == "/pov"
    db.close()


def test_a_destroyed_pov_is_not_collected():
    """It describes something that no longer exists; reaping it would enqueue a destroy
    for an environment already gone."""
    db = d.SessionLocal()
    live = _env(db)
    gone = _env(db, status=pov_env_service.STATUS_DESTROYED)
    going = _env(db, status=pov_env_service.STATUS_DESTROYING)
    ids = {i["id"] for i in inventory_service.collect(db) if i.get("kind") == "pov"}
    assert f"pov:{live.id}" in ids
    assert f"pov:{gone.id}" not in ids and f"pov:{going.id}" not in ids
    db.close()


def test_the_reaper_can_resolve_a_pov_row():
    db = d.SessionLocal()
    env = _env(db, expires_at=datetime.utcnow() + timedelta(days=1))
    resolved = reaper._resolve_row(db, f"pov:{env.id}")
    assert resolved is not None and resolved[0].id == env.id
    assert reaper._resolve_row(db, "pov:nope") is None
    db.close()


# ── the teardown ─────────────────────────────────────────────────────────────

def test_reaping_a_pov_enqueues_the_same_job_the_delete_endpoint_creates():
    """This module never learns the teardown ORDER — the share link before the jump items
    before the Gateway before the broker. Going through the queue is what keeps that in
    one place."""
    db = d.SessionLocal()
    env = _env(db, expires_at=datetime.utcnow() - timedelta(hours=2), workgroup="wg1")
    job_id = reaper._reap_row(db, {"kind": "pov", "id": f"pov:{env.id}"})
    job = db.query(d.Job).filter(d.Job.id == job_id).first()
    assert job is not None
    assert job.job_type == "pov_env_destroy"
    assert job.metadata_dict.get("environment_id") == env.id
    assert job.workgroup == "wg1"
    assert job.created_by == reaper.REAPER_ACTOR
    db.close()


def test_reaping_clears_the_timer_so_it_cannot_happen_twice():
    """At-most-once is structural: clearing `expires_at` in the same commit removes the
    row from `expired()`'s view entirely."""
    db = d.SessionLocal()
    env = _env(db, expires_at=datetime.utcnow() - timedelta(hours=2))
    reaper._reap_row(db, {"kind": "pov", "id": f"pov:{env.id}"})
    db.refresh(env)
    assert env.expires_at is None
    db.close()


def test_reaping_a_vanished_pov_refuses_rather_than_crashing():
    db = d.SessionLocal()
    try:
        reaper._reap_row(db, {"kind": "pov", "id": "pov:" + uuid.uuid4().hex})
        raise AssertionError("a missing POV was 'reaped'")
    except reaper.ReapRefused as exc:
        assert "gone" in str(exc)
    db.close()


# ── the ladder ───────────────────────────────────────────────────────────────

def test_the_ladder_descends_and_ends_close_enough_to_act_on():
    rungs = pol.WARN_LADDER_MINUTES
    assert list(rungs) == sorted(rungs, reverse=True), "the ladder is not descending"
    assert rungs[-1] <= 60, "the last rung is too far out for anyone to act on"
    assert rungs[0] >= 24 * 60, "the first rung gives less than a day's notice"


def test_each_rung_fires_once():
    """`expiry_warned_at` alone is warn-once, so with five stages the first warning would
    burn it and the other four would never fire."""
    latch = None
    fired = []
    # Walk the deadline in from well outside the ladder to just inside the last rung.
    for remaining_min in (10 * 24 * 60, 8 * 24 * 60, 6 * 24 * 60, 4 * 24 * 60,
                          2 * 24 * 60, 20 * 60, 3 * 60, 30):
        stage = pol.next_warn_stage(remaining_min * 60, latch)
        if stage is not None:
            fired.append(stage)
            latch = stage
    assert fired == list(pol.WARN_LADDER_MINUTES), fired


def test_nothing_fires_before_the_first_rung():
    assert pol.next_warn_stage(30 * _DAY, None) is None


def test_a_missed_rung_does_not_fire_late():
    """A sweep runs every 30 minutes and a POV can cross two rungs between passes.
    Returning the TIGHTEST crossed rung sends 'expires in 4 hours', not a stale
    'expires in 3 days' followed by four more mails in quick succession."""
    # Nothing warned yet, and we are already inside the last rung.
    assert pol.next_warn_stage(45 * _MIN, None) == pol.WARN_LADDER_MINUTES[-1]
    # …and having sent it, nothing further fires.
    assert pol.next_warn_stage(40 * _MIN, pol.WARN_LADDER_MINUTES[-1]) is None


def test_the_same_rung_does_not_repeat_across_sweeps():
    stage = pol.next_warn_stage(20 * _HOUR, None)
    assert stage == 24 * 60
    for again in (19 * _HOUR, 18 * _HOUR, 5 * _HOUR + 1):
        assert pol.next_warn_stage(again, stage) is None or again < 4 * _HOUR


def test_past_expiry_warns_nothing():
    """Something already overdue is the reaper's business — 'expires soon' about a
    resource being destroyed right now is noise."""
    assert pol.next_warn_stage(0, None) is None
    assert pol.next_warn_stage(-_HOUR, None) is None


def test_an_extend_re_opens_the_whole_ladder():
    """The caller clears the latch, and that is the whole reason `warned_stage_minutes` is
    a rung rather than a bool: without it every tighter rung would be permanently silenced
    against a deadline that no longer exists."""
    # An hour out, the last rung has fired.
    assert pol.next_warn_stage(50 * _MIN, pol.WARN_LADDER_MINUTES[-1]) is None
    # Extended to a week out and the latch cleared: the ladder starts again.
    assert pol.next_warn_stage(6 * _DAY, None) == pol.WARN_LADDER_MINUTES[0]


def test_set_expiry_clears_both_latches():
    db = d.SessionLocal()
    env = _env(db, expires_at=datetime.utcnow() + timedelta(hours=1),
               expiry_warned_at=datetime.utcnow(),
               warned_stage_minutes=pol.WARN_LADDER_MINUTES[-1])
    item = inventory_service._pov_item(env)
    out = reaper.set_expiry(db, [item], extend_hours=48, is_admin=True, actor="t")
    assert not out["failed"], out["failed"]
    db.refresh(env)
    assert env.expiry_warned_at is None
    assert env.warned_stage_minutes is None, "the ladder stayed silenced after an extend"
    db.close()


def test_the_warner_picks_the_ladder_only_for_rows_that_have_the_latch():
    """Which discipline a row follows is a property of the ROW, not a list of kinds here
    that could fall out of step with the schema."""
    assert hasattr(d.PovEnvironment, "warned_stage_minutes")
    # The kinds that predate this slice keep warn-once.
    for model in (d.Job, d.CloudDatabase, d.K8sCluster):
        assert not hasattr(model, "warned_stage_minutes"), model.__name__


def test_the_remaining_label_reads_in_days_when_it_is_days():
    """A POV's first rung is a week out, and 'about 168h' is a number nobody converts in
    their head."""
    assert reaper._remaining_label(7 * _DAY) == "about 7 days"
    assert reaper._remaining_label(3 * _DAY) == "about 3 days"
    # Hours below a day and a half, where "about 2 days" would round away the difference
    # between the 24h rung and the 4h one.
    assert reaper._remaining_label(_DAY) == "about 24h"
    assert reaper._remaining_label(4 * _HOUR) == "about 4h"


# ── the default ──────────────────────────────────────────────────────────────

def test_a_pov_has_its_own_default_knob():
    """Not `default_hours`. A cloud VM's default is a working-day number and a POV is an
    evaluation that runs for weeks with a customer inside it; sharing one number forces a
    wrong default on one of them, and reaping a customer's lab is the worse half."""
    assert pol.pov_default_hours() != pol.default_hours() or pol.pov_default_hours() == 0
    assert "pov_expiry_default_hours" in pathlib.Path(
        _ROOT, "web_dashboard", "config.py").read_text(encoding="utf-8")


def test_stamping_is_opt_in_like_every_other_kind():
    """`resource_expiry_default_hours=0` is one of the four brakes: flipping only the
    master switch must still change nothing. A POV default that ignored that would stamp
    timers on an estate the operator has not opted in."""
    assert pol._DEFAULT_POV_HOURS == 0
    original = pol.pov_default_hours, pol.enabled
    pol.enabled = lambda: True
    pol.pov_default_hours = lambda: 0
    try:
        assert pol.default_expiry_for_kind("pov") is None
    finally:
        pol.pov_default_hours, pol.enabled = original


def test_the_default_is_none_while_the_feature_is_off():
    """NULL means never, which is what makes enabling the feature later act on nothing
    that already exists — so the POV default must be read AFTER the master switch, not
    before it."""
    original = pol.enabled, pol.pov_default_hours
    pol.enabled = lambda: False
    pol.pov_default_hours = lambda: 720
    try:
        assert pol.default_expiry_for_kind("pov") is None
    finally:
        pol.enabled, pol.pov_default_hours = original


def test_the_default_is_stamped_once_an_operator_opts_in():
    original = pol.enabled, pol.pov_default_hours
    pol.enabled = lambda: True
    pol.pov_default_hours = lambda: 30 * 24
    try:
        when = pol.default_expiry_for_kind("pov")
        assert when is not None and when > datetime.utcnow() + timedelta(days=1)
    finally:
        pol.enabled, pol.pov_default_hours = original


def test_a_pov_row_defaults_to_no_timer():
    """Every pre-existing POV backfills to NULL. Turning the feature on cannot select a
    single environment that already existed."""
    db = d.SessionLocal()
    env = _env(db)
    assert env.expires_at is None
    assert env.warned_stage_minutes is None
    db.close()

def test_a_pov_on_a_CLOUD_is_reapable_exactly_like_one_on_skytap():
    """The auto-delete timer needed no change for a cloud lab platform, and this is the
    claim under that.

    `inventory_service._pov_item` reports `cloud = row.platform`, so a cloud POV arrives
    at the policy with `cloud="aws"` — a value that IS in `REAPABLE_VM_CLOUDS`, unlike
    "skytap". If the pov branch had been written by widening the VM tests instead of
    short-circuiting ahead of them, a cloud POV would take the VM path and be looked up as
    a deploy job that does not exist.
    """
    now = datetime.utcnow().timestamp()
    for cloud in ("skytap",) + tuple(lab_platforms.CLOUD_PLATFORMS):
        item = _item(cloud=cloud, expires_at=_ago(hours=2))
        capable, why = pol.ttl_capable(item)
        assert capable, f"{cloud}: {why}"
        target = pol.reap_target(item, now_ts=now, grace_min=30,
                                 armed_at_ts=now - 10 * _HOUR)
        assert target is not None and target["kind"] == "pov", \
            f"a {cloud} POV past its expiry was not selected for reaping"


def test_a_cloud_pov_reap_enqueues_the_same_destroy_job():
    """One teardown path, whatever the platform. The reaper must never learn the order —
    which for a cloud POV means terminating instances before unpicking the VPC they sit
    in, and that lives in the adapter."""
    db = d.SessionLocal()
    env = _env(db, platform=lab_platforms.CLOUD_PLATFORMS[0])
    job_id = reaper._reap_row(db, {"kind": "pov", "id": f"pov:{env.id}"})
    job = db.query(d.Job).filter(d.Job.id == job_id).first()
    assert job is not None and job.job_type == "pov_env_destroy", \
        f"expected pov_env_destroy, got {job and job.job_type}"
    assert job.metadata_dict.get("environment_id") == env.id


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
