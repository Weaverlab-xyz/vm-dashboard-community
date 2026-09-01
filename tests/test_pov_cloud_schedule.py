"""The POV suspend schedule: boundary crossings, DST, and what must NOT happen.

No public cloud has Skytap's idle timer, so the dashboard supplies one. The whole design
rests on a single distinction — the schedule acts on a **boundary crossed since the last
evaluation**, never on "should this be asleep right now?" — and every interesting bug in
this feature is a case where those two answers differ.

What is pinned here:

  * a manual start outside working hours survives, which a state check would undo four
    minutes later, forever;
  * the first evaluation acts on nothing, so enabling this on an existing estate suspends
    nothing by construction rather than by a guard;
  * an outage that swallowed both boundaries settles on the LATER one, not on a replay;
  * a stale latch is re-armed rather than replayed;
  * the day-of-week mask and the timezone are honoured, including across a DST change,
    where adding 24 hours to a UTC instant gives the wrong local time;
  * the sweep only ever ENQUEUES, and never calls a cloud SDK.

Pure policy: no database, no clock, no cloud.

Runs under pytest, or standalone:
    python tests/test_pov_cloud_schedule.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-schedule")

from web_dashboard.services import pov_schedule as sch  # noqa: E402

_UTC = timezone.utc


class Row:
    """The four schedule fields plus the latch, as the model carries them."""

    def __init__(self, suspend="19:00", resume="08:00", tz="UTC",
                 days=sch.DAYS_ALL, last=None):
        self.suspend_at_local = suspend
        self.resume_at_local = resume
        self.schedule_timezone = tz
        self.schedule_days = days
        # Stored naive-UTC, exactly as SQLAlchemy hands it back.
        self.schedule_last_checked_at = last


def _at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_UTC)


def _naive(dt):
    return dt.astimezone(_UTC).replace(tzinfo=None)


# ── the core distinction ─────────────────────────────────────────────────────

def test_crossing_the_suspend_time_asks_for_a_suspend():
    row = Row(last=_naive(_at(2026, 9, 1, 18, 55)))
    assert sch.due_action(row, _at(2026, 9, 1, 19, 5)) == sch.SUSPEND


def test_crossing_the_resume_time_asks_for_a_resume():
    row = Row(last=_naive(_at(2026, 9, 1, 7, 55)))
    assert sch.due_action(row, _at(2026, 9, 1, 8, 5)) == sch.RESUME


def test_a_pass_that_crosses_nothing_asks_for_nothing():
    """The common case, twice every ten minutes all day."""
    row = Row(last=_naive(_at(2026, 9, 1, 14, 0)))
    assert sch.due_action(row, _at(2026, 9, 1, 14, 10)) == ""


def test_a_manual_start_outside_hours_is_not_undone():
    """The case the whole design exists for.

    An SE starts a POV by hand at 20:00 for a call with a customer in another timezone.
    A "should it be asleep right now?" check would answer yes and suspend it again on the
    next sweep — and on every sweep after that, forever. A boundary check leaves it alone
    until tomorrow's 19:00.
    """
    row = Row(last=_naive(_at(2026, 9, 1, 20, 0)))
    for minutes in (10, 60, 180, 600):
        at = _at(2026, 9, 1, 20, 0) + timedelta(minutes=minutes)
        assert sch.due_action(row, at) == "" or at.hour < 20, (
            f"the schedule tried to act {minutes} minutes after a manual start")
    # …and tomorrow's boundary still fires.
    row.schedule_last_checked_at = _naive(_at(2026, 9, 2, 18, 55))
    assert sch.due_action(row, _at(2026, 9, 2, 19, 5)) == sch.SUSPEND


def test_the_first_evaluation_acts_on_nothing():
    """A NULL latch means "never checked", and an unbounded window has crossed every
    boundary there has ever been. Same rule the auto-delete timer's arming clock follows:
    turning a thing on must not make a backlog eligible at once."""
    assert sch.due_action(Row(last=None), _at(2026, 9, 1, 19, 5)) == ""


def test_an_outage_that_swallowed_both_boundaries_settles_on_the_later_one():
    """Down from 18:00 to 22:00, missing 19:00 suspend and 21:00 resume. The environment
    should end where the schedule says it should be NOW, not replay both in order."""
    row = Row(suspend="19:00", resume="21:00", last=_naive(_at(2026, 9, 1, 18, 0)))
    assert sch.due_action(row, _at(2026, 9, 1, 22, 0)) == sch.RESUME
    row = Row(suspend="21:00", resume="19:00", last=_naive(_at(2026, 9, 1, 18, 0)))
    assert sch.due_action(row, _at(2026, 9, 1, 22, 0)) == sch.SUSPEND


def test_a_stale_latch_is_not_replayed():
    """A week-old latch — the dashboard was off, or the column was just backfilled — must
    not power an environment because of something that "happened" while nobody was
    watching. The caller re-latches and moves on."""
    row = Row(last=_naive(_at(2026, 8, 25, 12, 0)))
    assert sch.due_action(row, _at(2026, 9, 1, 12, 0)) == ""


def test_a_row_with_no_schedule_is_never_acted_on():
    assert sch.due_action(Row(suspend="", resume=""), _at(2026, 9, 1, 19, 5)) == ""
    assert sch.has_schedule(Row(suspend="")) is False


def test_a_resume_alone_is_not_a_schedule():
    """It would wake a POV that nothing ever puts to sleep."""
    row = Row(suspend="", resume="08:00", last=_naive(_at(2026, 9, 1, 7, 55)))
    assert sch.due_action(row, _at(2026, 9, 1, 8, 5)) == ""


def test_no_resume_time_means_the_pov_stays_down_until_somebody_starts_it():
    row = Row(resume="", last=_naive(_at(2026, 9, 1, 7, 55)))
    assert sch.due_action(row, _at(2026, 9, 1, 8, 5)) == ""
    row.schedule_last_checked_at = _naive(_at(2026, 9, 1, 18, 55))
    assert sch.due_action(row, _at(2026, 9, 1, 19, 5)) == sch.SUSPEND


# ── days and timezones ───────────────────────────────────────────────────────

def test_a_weekday_only_schedule_does_nothing_at_the_weekend():
    # 2026-09-05 is a Saturday, 2026-09-07 a Monday.
    row = Row(days=sch.DAYS_WEEKDAYS, last=_naive(_at(2026, 9, 5, 18, 55)))
    assert sch.due_action(row, _at(2026, 9, 5, 19, 5)) == "", "Saturday suspended"
    row.schedule_last_checked_at = _naive(_at(2026, 9, 7, 18, 55))
    assert sch.due_action(row, _at(2026, 9, 7, 19, 5)) == sch.SUSPEND


def test_the_time_is_local_to_the_schedules_timezone():
    """19:00 New York is 23:00 UTC, so a UTC 19:00 pass must do nothing."""
    row = Row(tz="America/New_York", last=_naive(_at(2026, 9, 1, 18, 55)))
    assert sch.due_action(row, _at(2026, 9, 1, 19, 5)) == ""
    row.schedule_last_checked_at = _naive(_at(2026, 9, 1, 22, 55))
    assert sch.due_action(row, _at(2026, 9, 1, 23, 5)) == sch.SUSPEND


def test_the_local_time_holds_across_a_dst_change():
    """The bug this guards: computing the next boundary by adding 24 hours to a UTC
    instant. Across a DST change the local time drifts by an hour, so a POV starts
    suspending at 18:00 or 20:00 and nobody connects it to the clocks changing.

    UK clocks go back on 2026-10-25. 19:00 London is 18:00 UTC in BST and 19:00 UTC in
    GMT — so the same schedule must fire at a different UTC instant either side.
    """
    row = Row(tz="Europe/London")
    row.schedule_last_checked_at = _naive(_at(2026, 10, 24, 17, 55))
    assert sch.due_action(row, _at(2026, 10, 24, 18, 5)) == sch.SUSPEND, \
        "BST: 19:00 London is 18:00 UTC"
    row.schedule_last_checked_at = _naive(_at(2026, 10, 26, 18, 55))
    assert sch.due_action(row, _at(2026, 10, 26, 19, 5)) == sch.SUSPEND, \
        "GMT: 19:00 London is 19:00 UTC"
    # …and the BST instant must NOT fire once the clocks have gone back.
    row.schedule_last_checked_at = _naive(_at(2026, 10, 26, 17, 55))
    assert sch.due_action(row, _at(2026, 10, 26, 18, 5)) == "", \
        "fired an hour early after the clocks changed"


# ── validation, at the form ──────────────────────────────────────────────────

def _refused(**kw):
    base = {"suspend_at": "19:00", "resume_at": "08:00", "tz_name": "UTC",
            "days": sch.DAYS_ALL}
    base.update(kw)
    try:
        sch.validate(**base)
    except sch.ScheduleError as exc:
        return str(exc)
    raise AssertionError(f"expected a refusal for {kw}")


def test_a_valid_schedule_round_trips():
    out = sch.validate(suspend_at="19:00", resume_at="08:00",
                       tz_name="Europe/London", days=sch.DAYS_WEEKDAYS)
    assert out["suspend_at_local"] == "19:00"
    assert out["schedule_days"] == sch.DAYS_WEEKDAYS
    assert out["schedule_timezone"] == "Europe/London"


def test_clearing_every_field_clears_the_schedule():
    out = sch.validate(suspend_at="", resume_at="", tz_name="", days="")
    assert all(v is None for v in out.values()), out


def test_a_bad_time_is_refused_with_the_format():
    assert "HH:MM" in _refused(suspend_at="7pm")
    assert "HH:MM" in _refused(suspend_at="25:00")


def test_an_unknown_timezone_is_refused_by_name():
    assert "IANA" in _refused(tz_name="Pacific/Nowhere")


def test_identical_suspend_and_resume_times_are_refused():
    assert "same" in _refused(resume_at="19:00")


def test_a_resume_with_no_suspend_is_refused():
    assert "nothing ever puts" in _refused(suspend_at="", resume_at="08:00")


def test_a_day_mask_with_no_days_is_refused():
    assert "never do anything" in _refused(days="0000000")


def test_a_malformed_day_mask_is_refused():
    assert "seven characters" in _refused(days="MTWTF")


# ── what the sweep may and may not do ────────────────────────────────────────

def test_the_sweep_only_enqueues_and_never_calls_a_cloud():
    """The reconcile pass reads every POV every ten minutes. A multi-minute power call
    inside it would block every other row, and would make the action invisible — as a job
    it has a row, a Live Output, a cancel and a place in the failed-jobs panel."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_reconcile.py"),
               encoding="utf-8").read()
    body = src.split("def sweep_schedules(", 1)[1].split("\nasync def ", 1)[0]
    assert 'job_type="pov_env_power"' in body, "the sweep does not enqueue a power job"
    for banned in ("set_runstate", "boto3", "pov_cloud_aws", "_to_thread"):
        assert banned not in body, f"the schedule sweep reaches {banned} directly"


def test_the_sweep_stamps_the_latch_even_when_it_acts():
    """Leaving the latch behind on the acting path would re-fire the same boundary on
    every pass until the power job finished."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_reconcile.py"),
               encoding="utf-8").read()
    body = src.split("def sweep_schedules(", 1)[1].split("\nasync def ", 1)[0]
    stamp = body.index("schedule_last_checked_at")
    enqueue = body.index('job_type="pov_env_power"')
    assert stamp < enqueue, "the latch is stamped after the enqueue, so it can be skipped"


def test_a_platform_with_its_own_idle_timer_is_left_alone():
    """Skytap's suspend_on_idle and this schedule answer the same question. A POV carrying
    both is one where neither is clearly in charge."""
    from web_dashboard.services import lab_platforms as lp
    assert lp.supports("skytap", "idle_suspend") is True
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_reconcile.py"),
               encoding="utf-8").read()
    body = src.split("def sweep_schedules(", 1)[1].split("\nasync def ", 1)[0]
    assert 'supports(env.platform, "idle_suspend")' in body, (
        "the sweep does not skip platforms with their own idle timer, so a Skytap POV "
        "would be driven by both")


def test_the_summary_says_when_and_where():
    row = Row(tz="Europe/London", days=sch.DAYS_WEEKDAYS)
    out = sch.describe(row)
    assert out["scheduled"] is True
    for fragment in ("19:00", "Mon", "Europe/London", "08:00"):
        assert fragment in out["summary"], out["summary"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
