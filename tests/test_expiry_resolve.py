"""Unit tests for expiry_policy.resolve_expiry — what an operator's Extend / Set /
Never request actually resolves to.

Separate from test_expiry_policy.py because this is the other half of the feature: that
file asks "may the sweeper delete this", this one asks "may this person keep it, and for
how long". Both are pure, and CI runs each file in its own process anyway.

The load-bearing cases are the two that are easy to get wrong and invisible when wrong:
an extend must add to the resource's CURRENT expiry rather than to now (or extending
early silently shortens the lifetime), and the ceiling must count from created_at (or
clicking Extend twice defeats it).

Pure Python: ``config_service`` and ``config.settings`` are stubbed. Runs under pytest,
or standalone:
    python tests/test_expiry_resolve.py
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _install_stubs():
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = []
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services
    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.get_bool = lambda key, default=False: (
        str(CONF.get(key, default)).strip().lower() in ("1", "true", "yes", "on")
    )
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg
    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = conf_mod


_install_stubs()
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "expiry_policy.py")
_spec = importlib.util.spec_from_file_location(
    "web_dashboard.services.expiry_policy", _PATH)
pol = importlib.util.module_from_spec(_spec)
sys.modules["web_dashboard.services.expiry_policy"] = pol
_spec.loader.exec_module(pol)


NOW = datetime(2026, 7, 28, 12, 0, 0)
CREATED = NOW - timedelta(hours=10)


def _cfg(**over):
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    CONF["resource_expiry_max_total_hours"] = "720"
    CONF.update({k: str(v) for k, v in over.items()})


def _resolve(**kw):
    kw.setdefault("created_at", CREATED)
    kw.setdefault("now", NOW)
    return pol.resolve_expiry(**kw)


# ── extend ───────────────────────────────────────────────────────────────────

def test_extend_adds_to_the_current_expiry_not_to_now():
    """A resource with 12h left, extended by 24h, has 36h left — not 24. Adding to
    `now` would silently PUNISH extending early, which is exactly when a careful
    operator does it."""
    _cfg()
    current = NOW + timedelta(hours=12)
    got, clamped = _resolve(current=current, extend_hours_req=24)
    assert got == NOW + timedelta(hours=36)
    assert clamped is False


def test_extend_from_no_timer_starts_at_now():
    _cfg()
    got, _ = _resolve(current=None, extend_hours_req=24)
    assert got == NOW + timedelta(hours=24)


def test_extend_from_an_already_expired_timer_starts_at_now():
    """An overdue resource extended by 24h gets 24h from now — not 24h from a deadline
    that has already gone by, which could still be in the past."""
    _cfg()
    got, _ = _resolve(current=NOW - timedelta(hours=5), extend_hours_req=24)
    assert got == NOW + timedelta(hours=24)


# ── the ceiling ──────────────────────────────────────────────────────────────

def test_the_ceiling_counts_total_lifetime_from_creation():
    """Two clicks must not beat a cap that only looked at one click."""
    _cfg(resource_expiry_max_total_hours=100)
    current = NOW + timedelta(hours=10)          # created 10h ago, 10h left
    got, clamped = _resolve(current=current, extend_hours_req=500)
    assert clamped is True
    # 100h ceiling - 10h already elapsed = 90h from now, not 510h.
    assert got == NOW + timedelta(hours=90)
    # Extending again from the clamped value lands on the same wall, not past it.
    got2, clamped2 = _resolve(current=got, extend_hours_req=500)
    assert clamped2 is True and got2 == got


def test_an_admin_bypasses_the_ceiling():
    _cfg(resource_expiry_max_total_hours=100)
    got, clamped = _resolve(current=NOW, extend_hours_req=500, is_admin=True)
    assert clamped is False and got == NOW + timedelta(hours=500)


def test_no_ceiling_configured_means_no_clamp():
    _cfg(resource_expiry_max_total_hours=0)
    got, clamped = _resolve(current=NOW, extend_hours_req=5000)
    assert clamped is False and got == NOW + timedelta(hours=5000)


def test_clamped_is_false_for_sub_minute_float_noise():
    """The clamp flag drives an operator-facing toast, so it must not fire on the
    rounding dust of timestamp arithmetic."""
    _cfg(resource_expiry_max_total_hours=720)
    _, clamped = _resolve(current=NOW + timedelta(hours=1), extend_hours_req=1)
    assert clamped is False


# ── absolute set ─────────────────────────────────────────────────────────────

def test_an_absolute_expiry_is_honoured_and_clamped():
    _cfg(resource_expiry_max_total_hours=100)
    got, clamped = _resolve(current=None, absolute=NOW + timedelta(hours=20))
    assert clamped is False and got == NOW + timedelta(hours=20)
    got, clamped = _resolve(current=None, absolute=NOW + timedelta(hours=500))
    assert clamped is True and got == NOW + timedelta(hours=90)   # 100 - 10 elapsed


def test_an_absolute_expiry_in_the_past_is_raised_to_the_floor():
    """Setting a deadline in the past must not mean "delete on the next sweep" — it
    collapses to the soonest legal expiry, so there is always time to notice."""
    _cfg()
    got, _ = _resolve(current=None, absolute=NOW - timedelta(days=3))
    assert got == NOW + timedelta(minutes=pol.MIN_TTL_MINUTES_FLOOR)


def test_a_nearby_absolute_expiry_is_raised_to_the_floor():
    _cfg()
    got, _ = _resolve(current=None, absolute=NOW + timedelta(minutes=5))
    assert got == NOW + timedelta(minutes=pol.MIN_TTL_MINUTES_FLOOR)


def test_a_timezone_aware_absolute_is_normalised_to_naive_utc():
    """The DB column is naive UTC; a browser may well send an offset."""
    from datetime import timezone
    _cfg()
    aware = (NOW + timedelta(hours=24)).replace(tzinfo=timezone.utc)
    got, _ = _resolve(current=None, absolute=aware)
    assert got.tzinfo is None and got == NOW + timedelta(hours=24)


# ── never / pin ──────────────────────────────────────────────────────────────

def test_never_is_refused_unless_policy_allows_it():
    """Clearing a timer defeats the whole feature, so it needs a deliberate opt-in."""
    _cfg(resource_expiry_allow_never=0)
    for admin in (True, False):
        try:
            _resolve(current=NOW, never=True, is_admin=admin)
        except ValueError as exc:
            assert "policy" in str(exc).lower() or "disabled" in str(exc).lower()
        else:
            raise AssertionError(f"never should be refused (is_admin={admin})")


def test_never_is_refused_for_a_non_admin_even_when_allowed():
    _cfg(resource_expiry_allow_never=1)
    try:
        _resolve(current=NOW, never=True, is_admin=False)
    except ValueError as exc:
        assert "administrator" in str(exc).lower()
    else:
        raise AssertionError("never should be admin-only")


def test_never_clears_the_timer_for_an_allowed_admin():
    """None is how "no expiry" is stored, so pin and clear are one write — there is no
    second state that could disagree."""
    _cfg(resource_expiry_allow_never=1)
    got, clamped = _resolve(current=NOW, never=True, is_admin=True)
    assert got is None and clamped is False


# ── nothing requested ────────────────────────────────────────────────────────

def test_an_empty_request_is_refused():
    _cfg()
    try:
        _resolve(current=NOW)
    except ValueError:
        pass
    else:
        raise AssertionError("a request with no change should raise")


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
