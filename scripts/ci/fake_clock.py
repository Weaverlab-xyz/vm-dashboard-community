#!/usr/bin/env python3
"""Run a test file as if the clock read a chosen time: find tests that pass only by the hour.

A test that steps from `utcnow()` by a fixed amount and lands in a window, or holds a
fixed future date and compares it with the real clock, passes on most runs and fails on
the others. tests/test_job_schedules.py failed CI only in the minute 22:59-23:00 UTC;
tests/test_directory_health_dc.py would have failed for everyone from May 2027. Both were
found by running the file at the time that breaks it, which is what this does.

  scripts/ci/fake_clock.py "2026-10-17 22:59:30" tests/test_x.py   # one run at that time
  scripts/ci/fake_clock.py --sweep tests/test_x.py [more...]       # every boundary below

`--sweep` runs each file for real, then at each time in SWEEP, and reports each time whose
exit status differs from the real run's. Use the interpreter the suite uses (a venv with
web_dashboard/requirements.txt).

What moves: datetime.datetime.utcnow()/now() and time.time(), from the given instant
onward. What does not: a database's own CURRENT_TIMESTAMP, and code that type-checks
datetimes natively -- `cryptography` rejects the stand-in class, so a file that builds
certificates fails under every fake time (that is this tool, not the test).
"""
import os
import subprocess
import sys

SWEEP = (
    "2026-10-17 12:00:00",   # an ordinary midday
    "2026-10-17 22:59:30",   # +1h lands in the last minute of the day
    "2026-10-17 23:00:30",
    "2026-10-17 23:59:30",   # the minute before midnight
    "2026-10-18 00:00:30",
    "2026-10-18 23:59:30",   # Sunday into Monday
    "2026-10-19 00:00:30",
    "2026-10-31 23:59:30",   # month end
    "2026-12-31 23:59:30",   # year end
    "2027-01-01 00:00:30",
    "2028-02-29 12:00:00",   # a leap day
    "2026-10-25 00:59:30",   # Europe leaves summer time at 01:00 UTC
    "2026-11-01 06:30:00",   # the US does at 06:00 UTC
    "2026-03-08 07:30:00",   # and enters it
    "2027-07-01 12:00:00",   # a fixed future date in a fixture, compared with today
    "2029-01-01 12:00:00",
    "2031-06-01 12:00:00",
)


def _run_at(when, path):
    import datetime as _dt
    import runpy
    import time as _time
    # Imported BEFORE the patch so it binds the real class.
    try:
        import sqlalchemy.dialects.sqlite  # noqa: F401
    except ImportError:
        pass

    real = _dt.datetime
    start_real = real.now()
    base = real.strptime(when, "%Y-%m-%d %H:%M:%S")
    offset = (base - real.now(_dt.timezone.utc).replace(tzinfo=None)).total_seconds()

    def plain(d):
        # A real datetime, never the stand-in: SQLAlchemy stores any other class as a date.
        return real(d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond, d.tzinfo)

    class _Meta(type):
        # A real datetime still passes isinstance(x, datetime.datetime): libraries look the
        # class up at call time, and every value they see is a real one.
        def __instancecheck__(cls, obj):
            return isinstance(obj, real)

        def __subclasscheck__(cls, sub):
            return issubclass(sub, real)

    class FakeDatetime(real, metaclass=_Meta):
        @classmethod
        def utcnow(cls):
            return plain(base + (real.now() - start_real))

        @classmethod
        def now(cls, tz=None):
            n = plain(base + (real.now() - start_real))
            return n.replace(tzinfo=_dt.timezone.utc).astimezone(tz) if tz else n

    _dt.datetime = FakeDatetime
    real_time = _time.time
    _time.time = lambda: real_time() + offset

    sys.path[:0] = [os.path.dirname(os.path.abspath(path)), os.getcwd()]
    sys.argv = [path]
    runpy.run_path(path, run_name="__main__")


def _sweep(paths):
    differs = 0
    for path in paths:
        quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        real = subprocess.run([sys.executable, path], **quiet).returncode
        bad = [t for t in SWEEP
               if subprocess.run([sys.executable, __file__, t, path], **quiet).returncode != real]
        if bad:
            differs += 1
            print(f"{path}: exit {real} for real, different at: " + ", ".join(bad))
        else:
            print(f"{path}: same at all {len(SWEEP)} times (exit {real})")
    return 1 if differs else 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--sweep":
        sys.exit(_sweep(sys.argv[2:]))
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    _run_at(sys.argv[1], sys.argv[2])
