"""A change window must land at the wall-clock time the operator typed.

``services/change_window.py`` is pure — stdlib plus ``suspend_schedule``, no database and
no clock of its own — so every case here runs with nothing installed and no fixtures.

The cases that matter are the ones a naive implementation gets wrong:

  * **DST.** "02:00 Sunday, New York" is a different number of UTC hours before and after
    the clocks change. Adding 24 hours to a UTC instant gets this wrong twice a year, and
    the symptom is a change running an hour outside its approved window — which is the
    one thing this whole feature exists to prevent. The local-day walk borrowed from
    ``suspend_schedule`` is what makes it right.
  * **A window that is open right now.** "Run in the current maintenance period" is the
    common case, and a resolver that only ever looked forward would push it a week out.
  * **A window crossing local midnight.** 22:00 + 6h ends at 04:00 the NEXT day, and that
    occurrence is still the current one at 01:00.

Run: python tests/test_change_window_policy.py   (or under pytest)
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# No guard at all, deliberately. `change_window` is pure stdlib plus `suspend_schedule`
# (also pure), so there is no optional third-party dependency to probe for — and an
# ImportError here would mean the module under test is BROKEN, which must fail loudly
# rather than print SKIP and exit 0. See tests/test_import_guard_narrowness.py: a guard
# wide enough to swallow that turns a file into a permanent silent no-op.
from web_dashboard.services import change_window as cw  # noqa: E402
from web_dashboard.services.suspend_schedule import ScheduleError  # noqa: E402


class W:
    """A duck-typed change window. The policy module never sees the ORM class."""

    def __init__(self, start="02:00", minutes=240, tz="UTC", days=cw.DAYS_ALL,
                 name="Test Window", enabled=True):
        self.name = name
        self.start_at_local = start
        self.duration_minutes = minutes
        self.timezone = tz
        self.schedule_days = days
        self.enabled = enabled


def _dt(s):
    return datetime.fromisoformat(s)


# ── The module must stay pure ────────────────────────────────────────────────

def test_the_policy_module_imports_nothing_from_the_app():
    """Pinned so a convenience import of config_service or the database cannot creep in
    and make every case below need a fixture. Same property expiry_policy holds."""
    import ast
    src = open(os.path.join(_ROOT, "web_dashboard/services/change_window.py"),
               encoding="utf-8").read()
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # `.suspend_schedule` is itself stdlib-only and is the point of the module.
            if node.level and mod not in ("suspend_schedule",):
                bad.append(mod or ".")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("web_dashboard"):
                    bad.append(a.name)
    assert not bad, f"change_window must stay pure; found app imports: {bad}"


# ── next_occurrence ───────────────────────────────────────────────────────────

def test_it_finds_todays_window_when_it_has_not_started():
    start, end = cw.next_occurrence(W(start="02:00", minutes=240),
                                    _dt("2026-03-10T00:30"))
    assert start == _dt("2026-03-10T02:00")
    assert end == _dt("2026-03-10T06:00")


def test_a_window_open_right_now_is_the_one_returned():
    """"Run it in the current maintenance period" must not be pushed to tomorrow."""
    start, end = cw.next_occurrence(W(start="02:00", minutes=240),
                                    _dt("2026-03-10T03:00"))
    assert start == _dt("2026-03-10T02:00"), "skipped the window that is open now"
    assert end == _dt("2026-03-10T06:00")


def test_a_closed_window_rolls_to_the_next_day():
    start, _ = cw.next_occurrence(W(start="02:00", minutes=240),
                                  _dt("2026-03-10T07:00"))
    assert start == _dt("2026-03-11T02:00")


def test_it_honours_the_day_mask():
    """Saturday only. Monday's answer must be the coming Saturday."""
    saturday_only = "0000010"
    start, _ = cw.next_occurrence(W(start="02:00", days=saturday_only),
                                  _dt("2026-03-09T09:00"))   # a Monday
    assert start.weekday() == 5, f"expected a Saturday, got {start:%A %Y-%m-%d}"
    assert start == _dt("2026-03-14T02:00")


def test_a_window_crossing_local_midnight_is_still_current_after_midnight():
    """22:00 + 6h ends at 04:00 the next day. At 01:00 that occurrence is the live one,
    which is why the search starts a day early."""
    start, end = cw.next_occurrence(W(start="22:00", minutes=360),
                                    _dt("2026-03-10T01:00"))
    assert start == _dt("2026-03-09T22:00")
    assert end == _dt("2026-03-10T04:00")


# ── DST: the reason this module borrows suspend_schedule's day walk ──────────

def test_spring_forward_keeps_the_local_wall_clock_time():
    """US DST began 2026-03-08. New York is UTC-5 before and UTC-4 after.

    A 02:00 local window is therefore 07:00 UTC on the 7th and 06:00 UTC on the 9th. An
    implementation that added 24h to a UTC instant would report 07:00 UTC for both — an
    hour outside the window the operator approved.
    """
    w = W(start="02:00", minutes=240, tz="America/New_York")
    before, _ = cw.next_occurrence(w, _dt("2026-03-07T00:00"))
    after, _ = cw.next_occurrence(w, _dt("2026-03-09T00:00"))
    assert before == _dt("2026-03-07T07:00"), before      # EST, UTC-5
    assert after == _dt("2026-03-09T06:00"), after        # EDT, UTC-4
    assert (after - before) != timedelta(days=2), (
        "the UTC offset did not change across the DST boundary — this is the bug the "
        "local-day walk exists to prevent")


def test_autumn_back_keeps_the_local_wall_clock_time():
    """US DST ended 2026-11-01: UTC-4 before, UTC-5 after."""
    w = W(start="02:00", minutes=240, tz="America/New_York")
    before, _ = cw.next_occurrence(w, _dt("2026-10-31T00:00"))
    after, _ = cw.next_occurrence(w, _dt("2026-11-02T00:00"))
    assert before == _dt("2026-10-31T06:00"), before      # EDT
    assert after == _dt("2026-11-02T07:00"), after        # EST


def test_a_european_zone_is_handled_too():
    """Not a duplicate of the US cases: EU and US DST boundaries are on different dates,
    so a hardcoded US rule would pass those and fail this."""
    w = W(start="01:00", minutes=120, tz="Europe/London")
    winter, _ = cw.next_occurrence(w, _dt("2026-01-05T00:00"))
    summer, _ = cw.next_occurrence(w, _dt("2026-07-06T00:00"))
    assert winter == _dt("2026-01-05T01:00")              # GMT
    assert summer == _dt("2026-07-06T00:00")              # BST, UTC+1


def test_the_returned_instants_are_naive():
    """Every datetime column in this app is naive UTC. An aware value here would raise
    on the first comparison against one of them."""
    start, end = cw.next_occurrence(W(tz="America/New_York"), _dt("2026-03-10T00:00"))
    assert start.tzinfo is None and end.tzinfo is None


def test_an_aware_input_is_accepted():
    from datetime import timezone as _tz
    start, _ = cw.next_occurrence(
        W(start="02:00"), datetime(2026, 3, 10, 0, 30, tzinfo=_tz.utc))
    assert start == _dt("2026-03-10T02:00")


# ── occurrence_covering ───────────────────────────────────────────────────────

def test_a_time_inside_the_window_is_covered():
    assert cw.occurrence_covering(W(start="02:00", minutes=240),
                                  _dt("2026-03-10T03:30"))


def test_a_time_outside_the_window_is_not_covered():
    assert not cw.occurrence_covering(W(start="02:00", minutes=240),
                                      _dt("2026-03-10T09:00"))


def test_the_window_end_is_exclusive():
    """A change may not START at the instant the window closes."""
    w = W(start="02:00", minutes=240)
    assert cw.occurrence_covering(w, _dt("2026-03-10T05:59"))
    assert not cw.occurrence_covering(w, _dt("2026-03-10T06:00"))


def test_a_time_on_the_wrong_day_is_not_covered():
    """The check that stops "03:00 on a Tuesday, in the Saturday window"."""
    assert not cw.occurrence_covering(W(start="02:00", days="0000010"),
                                      _dt("2026-03-10T03:00"))   # a Tuesday


# ── validate ──────────────────────────────────────────────────────────────────

def test_a_valid_window_normalizes():
    out = cw.validate(name="  Prod Weekend  ", start_at_local="02:00",
                      duration_minutes="240", tz_name="America/New_York", days="0000010")
    assert out["name"] == "Prod Weekend"
    assert out["duration_minutes"] == 240
    assert out["schedule_days"] == "0000010"


def test_a_nameless_window_is_refused():
    _refuses(name="   ", start_at_local="02:00", duration_minutes=60,
             tz_name="UTC", days=cw.DAYS_ALL)


def test_a_zero_length_window_is_refused():
    """Every change booked into it would be missed the instant it was booked."""
    _refuses(name="w", start_at_local="02:00", duration_minutes=0,
             tz_name="UTC", days=cw.DAYS_ALL)


def test_an_absurdly_long_window_is_refused():
    _refuses(name="w", start_at_local="02:00", duration_minutes=60 * 24 * 30,
             tz_name="UTC", days=cw.DAYS_ALL)


def test_a_bad_time_is_refused():
    _refuses(name="w", start_at_local="7pm", duration_minutes=60,
             tz_name="UTC", days=cw.DAYS_ALL)


def test_an_unknown_timezone_is_refused():
    _refuses(name="w", start_at_local="02:00", duration_minutes=60,
             tz_name="Mars/Olympus", days=cw.DAYS_ALL)


def test_an_empty_day_mask_is_refused():
    """A window with no days selected would never open, and a change booked into it
    would be missed forever."""
    _refuses(name="w", start_at_local="02:00", duration_minutes=60,
             tz_name="UTC", days="0000000")


def _refuses(**kw):
    try:
        cw.validate(**kw)
    except ScheduleError:
        return
    raise AssertionError(f"accepted an invalid window: {kw}")


# ── describe ──────────────────────────────────────────────────────────────────

def test_describe_reads_as_a_sentence():
    out = cw.describe(W(start="02:00", minutes=240, tz="America/New_York",
                        days="0000010"))
    assert "Sat" in out["summary"], out["summary"]
    assert "02:00" in out["summary"] and "06:00" in out["summary"], out["summary"]
    assert "America/New_York" in out["summary"], out["summary"]


def test_describe_marks_a_window_that_ends_the_next_day():
    out = cw.describe(W(start="22:00", minutes=360))
    assert "+1d" in out["summary"], out["summary"]


def test_duration_formats():
    assert cw.format_duration(240) == "4h"
    assert cw.format_duration(90) == "1h 30m"
    assert cw.format_duration(45) == "45m"


def _run():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
