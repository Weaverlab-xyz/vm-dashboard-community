"""A workgroup can require that changes only START inside its change window.

The per-run picker is a choice; this is a constraint. It rides
``admission_service.enforce`` — the gate every mutating endpoint already calls
immediately before its first ``create_job`` — so it applies at 27 seams without any of
them knowing about it.

The properties, in the order they matter:

  * **The offer must be acceptable.** The refusal names the next occurrence and the UI
    turns that into one click. If the gate only asked "is it Saturday yet", the booked
    change would come straight back through and be refused too, and the operator would
    have no way to comply with what they were just told. That is the one bug that would
    make the whole feature useless, so it is the first test here.
  * **An unconstrained workgroup is untouched.** Every workgroup that exists today is
    unconstrained, so this is the regression that matters.
  * **Fail closed once required, fail OPEN before that.** A workgroup that requires a
    window it cannot resolve refuses. But a lookup that cannot even determine whether a
    window is required admits — most plausibly a skipped migration, and #946 showed
    those happen silently on PostgreSQL. Failing closed there would 403 every gated
    deploy on an estate that never opted in.

Run: python tests/test_workgroup_change_window.py   (or under pytest)
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-wg-change-window")
os.environ["DATABASE_URL"] = "sqlite://"

try:
    import sqlalchemy  # noqa: F401
    import fastapi  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"dependency unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from web_dashboard.database import Base, ChangeWindow, Workgroup  # noqa: E402
from web_dashboard.services import admission_service, change_window  # noqa: E402

ACTION = "aws:ec2:deploy"


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# Stub the two config reads this module makes, so the tests exercise the
# change-window gate and nothing else. `gated_actions` is the operator's list of what
# counts as a change; `_enabled` is the OPA switch, held OFF so an ADMITTED request
# returns from `enforce` instead of falling through to a policy engine that is not the
# subject here (and whose config table this in-memory database does not have).
_REAL_GATED = admission_service.gated_actions
_REAL_ENABLED = admission_service._enabled
admission_service.gated_actions = lambda: {ACTION}
admission_service._enabled = lambda: False


def _wg(db, *, window=None, required=False, name="prod"):
    row = Workgroup(name=name, display_name=name.title(),
                    change_window_id=(window.id if window else None),
                    require_change_window=(True if required else None))
    db.add(row)
    db.commit()
    return row


def _window(db, *, days="0000010", start="02:00", minutes=240, name="Prod Weekend"):
    """Saturday 02:00–06:00 UTC by default — closed on most days, which is what makes
    the refusal tests deterministic without freezing a clock."""
    w = ChangeWindow(name=name, start_at_local=start, duration_minutes=minutes,
                     timezone="UTC", schedule_days=days, enabled=True,
                     created_by="admin")
    db.add(w)
    db.commit()
    return w


def _enforce(db, *, workgroup="prod", scheduled=None, now=None):
    admission_service.enforce(ACTION, request={"workgroup": workgroup},
                              actor=None, db=db, now=now, scheduled=scheduled)


def _refuses(db, **kw):
    try:
        _enforce(db, **kw)
    except HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
        return exc.detail
    raise AssertionError("the change was admitted")


# ── The one that makes the feature usable ─────────────────────────────────────

def test_the_refusal_carries_an_acceptable_offer():
    db = _session()
    w = _window(db)
    _wg(db, window=w, required=True)
    detail = _refuses(db)

    assert detail["error"] == "change_window", detail
    offer = detail.get("schedule")
    assert offer, "no offer — the operator is refused with nowhere to go"
    assert offer["change_window_id"] == w.id
    assert offer["window_name"] == "Prod Weekend"
    assert offer["next_start"] and offer["next_end"]


def test_accepting_the_offer_is_admitted():
    """THE property. Booking into the window the gate just named must pass — otherwise
    the offer is a dead end and the change can never be made at all."""
    db = _session()
    w = _window(db)
    _wg(db, window=w, required=True)
    offer = _refuses(db)["schedule"]

    _enforce(db, scheduled={"change_window_id": offer["change_window_id"]})


def test_a_time_inside_the_window_is_admitted_without_naming_it():
    """"At a time" that happens to fall inside the window satisfies it too — the
    requirement is about WHEN the change runs, not about which control was used."""
    db = _session()
    w = _window(db)
    _wg(db, window=w, required=True)
    start, _end = change_window.next_occurrence(w, datetime.utcnow())
    _enforce(db, scheduled={"scheduled_for": start + timedelta(minutes=30)})


def test_a_time_outside_the_window_is_still_refused():
    """The other half — a booking is not a bypass."""
    db = _session()
    w = _window(db)
    _wg(db, window=w, required=True)
    start, _end = change_window.next_occurrence(w, datetime.utcnow())
    _refuses(db, scheduled={"scheduled_for": start + timedelta(days=1)})


def test_a_booking_into_a_different_window_is_refused():
    db = _session()
    required = _window(db)
    other = _window(db, days="1111111", name="Anytime")
    _wg(db, window=required, required=True)
    _refuses(db, scheduled={"change_window_id": other.id})


# ── Inert unless opted into ───────────────────────────────────────────────────

def test_an_unconstrained_workgroup_is_untouched():
    """Every workgroup that exists today is in this state."""
    db = _session()
    _wg(db)
    _enforce(db)


def test_a_workgroup_with_a_window_but_no_requirement_is_untouched():
    """Picking a window is not the same as requiring one — the requirement is a
    separate, deliberate tick."""
    db = _session()
    _wg(db, window=_window(db), required=False)
    _enforce(db)


def test_a_request_with_no_workgroup_is_untouched():
    db = _session()
    _wg(db, window=_window(db), required=True)
    _enforce(db, workgroup="")


def test_an_unknown_workgroup_is_untouched():
    """Nothing to look a window up on. Refusing here would block deploys for a
    workgroup the dashboard does not manage."""
    db = _session()
    _wg(db, window=_window(db), required=True)
    _enforce(db, workgroup="somewhere-else")


def test_an_ungated_action_is_untouched():
    """The gate shares `admission_gated_actions` — the operator's own list of what
    counts as a change. Power operations are deliberately absent from it."""
    db = _session()
    _wg(db, window=_window(db), required=True)
    admission_service.enforce("aws:ec2:power", request={"workgroup": "prod"},
                              actor=None, db=db)


def test_an_open_window_admits_a_plain_run_now():
    """Inside the window, Run now is just Run now."""
    db = _session()
    w = _window(db, days="1111111", start="00:00", minutes=24 * 60 - 1)
    _wg(db, window=w, required=True)
    _enforce(db)


# ── Failure directions ────────────────────────────────────────────────────────

def test_a_required_window_that_was_deleted_fails_closed():
    """An administrator asked for these changes to be gated; a missing window is a
    broken gate, not an open one."""
    db = _session()
    row = _wg(db, window=_window(db), required=True)
    db.query(ChangeWindow).filter(ChangeWindow.id == row.change_window_id).delete()
    db.commit()
    detail = _refuses(db)
    assert "no longer exists" in detail["reasons"][0], detail


class _BrokenDB:
    """A session whose every query raises, standing in for the missing column.

    A real `Session.close()` does NOT do this — it returns the connection and the
    session silently reopens on next use, so the first version of this test passed
    against a working query and proved nothing.
    """

    def query(self, *a, **k):
        raise RuntimeError("no such column: workgroups.require_change_window")


def test_an_unreadable_workgroup_table_fails_OPEN():
    """The opposite direction, and the reason it is opposite.

    If the lookup that decides *whether* a window is required cannot run — most
    plausibly because the `require_change_window` migration was skipped, which #946
    showed happens silently on PostgreSQL — then this install has almost certainly
    never used the feature. Failing closed would 403 every gated deploy on an estate
    that never opted in, which is far worse than not enforcing a constraint nobody
    configured.
    """
    admission_service.enforce(ACTION, request={"workgroup": "prod"},
                              actor=None, db=_BrokenDB())


def test_no_db_is_untouched():
    """`enforce` is called with db=None in places; it must not explode."""
    admission_service.enforce(ACTION, request={"workgroup": "prod"}, actor=None,
                              db=None)


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
    admission_service.gated_actions = _REAL_GATED
    admission_service._enabled = _REAL_ENABLED
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
