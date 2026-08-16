"""Unit tests for services/notify_policy.py — the pure half of outbound notifications.

The headline tests here are :func:`test_nothing_is_sent_until_two_separate_switches_are_flipped`
and :func:`test_two_long_keys_that_share_a_prefix_do_not_collide`. The first pins the
rollout property: enabling notifications against a live inventory must still send
nothing until dry-run is explicitly turned off. The second pins the dedupe property —
a key collision doesn't produce a duplicate, it makes a notification silently vanish,
which is the failure mode nobody notices.

Pure Python: ``config_service`` and ``config.settings`` are stubbed, no DB, no deps.
Runs under pytest, or standalone:
    python tests/test_notify_policy.py
"""
import importlib.util
import os
import sys
import types
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Simulated config store, keyed exactly as app_config is. Unset keys resolve to "" —
# what the real config_service.get returns for a key nobody wrote.
CONF = {}


def _install_stubs():
    """Register stub parent packages so notify_policy's relative imports resolve while
    it is loaded by file path (mirrors test_expiry_policy._install_stubs). Loading by
    path is what keeps this runnable on a bare checkout."""
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
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "notify_policy.py")
_spec = importlib.util.spec_from_file_location(
    "web_dashboard.services.notify_policy", _PATH)
pol = importlib.util.module_from_spec(_spec)
sys.modules["web_dashboard.services.notify_policy"] = pol
_spec.loader.exec_module(pol)


# ── helpers ──────────────────────────────────────────────────────────────────

def _on(**overrides):
    """Turn notifications on, plus any overrides."""
    CONF.clear()
    CONF["notifications_enabled"] = "1"
    CONF.update({k: str(v) for k, v in overrides.items()})


def _ev(**over):
    base = {
        "event_type": "resource.expiring",
        "title": "web-01 expires in 23 hours",
        "body": "It will be destroyed automatically unless you extend it.",
        "resource_id": "job:abc", "resource_kind": "vm", "resource_name": "web-01",
        "cloud": "aws", "region": "us-east-1", "url": "/inventory",
    }
    base.update(over)
    return pol.NotificationEvent(**base)


# ── the rollout guarantee ────────────────────────────────────────────────────

def test_nothing_is_sent_until_two_separate_switches_are_flipped():
    """Enabling the feature must not, by itself, send anything.

    An operator turning this on against a live estate can produce hundreds of
    messages in the first pass. dry_run defaults ON so the first thing they get is a
    delivery log to read, not an inbox to apologise for — the same observe-first
    rollout resource_expiry_dry_run gives the reaper.
    """
    CONF.clear()
    assert pol.enabled() is False
    assert pol.dry_run() is True          # already on before the feature is
    _on()
    assert pol.enabled() is True
    assert pol.dry_run() is True          # still on after enabling
    _on(notify_dry_run="0")
    assert pol.dry_run() is False         # only an explicit second act turns it off


def test_every_shipped_default_event_is_deliverable_at_the_default_floor():
    """A default event set that the default min_severity filters out would mean the
    feature ships silent — configured, enabled, and delivering nothing."""
    _on(notify_dry_run="0")
    floor = pol.severity_rank(pol.min_severity())
    for et in pol.parse_event_types(pol.DEFAULT_EVENT_TYPES):
        sev = pol.EVENT_SEVERITY[et]
        assert pol.severity_rank(sev) >= floor, f"{et} ({sev}) is below the default floor"


# ── gating ───────────────────────────────────────────────────────────────────

def test_should_notify_respects_each_gate_independently():
    _on()
    ev = _ev()
    assert pol.should_notify(ev) is True

    CONF["notifications_enabled"] = "0"
    assert pol.should_notify(ev) is False

    _on(notify_min_severity="critical")
    assert pol.should_notify(ev) is False              # warning < critical
    assert pol.should_notify(_ev(event_type="resource.reaped")) is True

    _on(notify_event_types="job.failed")
    assert pol.should_notify(ev) is False
    assert pol.should_notify(_ev(event_type="job.failed")) is True


def test_an_endpoint_filter_narrows_but_an_empty_one_inherits():
    _on()
    ev = _ev()
    assert pol.should_notify(ev, endpoint_event_types=None) is True
    assert pol.should_notify(ev, endpoint_event_types=frozenset()) is True
    assert pol.should_notify(ev, endpoint_event_types={"resource.expiring"}) is True
    assert pol.should_notify(ev, endpoint_event_types={"job.failed"}) is False


def test_an_endpoint_filter_cannot_widen_past_the_global_list():
    """A per-endpoint list is a narrowing filter, not an override — otherwise one
    endpoint could resurrect an event type the operator globally turned off."""
    _on(notify_event_types="job.failed")
    assert pol.should_notify(_ev(), endpoint_event_types={"resource.expiring"}) is False


def test_an_unknown_event_type_is_ignored_rather_than_fatal():
    """A downgrade that no longer knows an event type should quietly stop matching
    it, not raise on every emit."""
    _on(notify_event_types="job.failed,not.a.real.event")
    assert "not.a.real.event" in pol.event_types()
    assert pol.should_notify(_ev(event_type="not.a.real.event")) is False  # severity floor
    assert pol.parse_event_types("  a , ,b ,") == frozenset({"a", "b"})


def test_severity_falls_back_to_the_catalogue_then_to_info():
    assert _ev(severity="").effective_severity() == "warning"
    assert _ev(severity="CRITICAL").effective_severity() == "critical"
    assert _ev(severity="nonsense").effective_severity() == "warning"   # catalogue
    assert _ev(event_type="who.knows", severity="").effective_severity() == "info"


# ── dedupe ───────────────────────────────────────────────────────────────────

def test_the_same_event_to_the_same_endpoint_keys_identically():
    a, b = pol.dedupe_key(_ev(), "ep-1"), pol.dedupe_key(_ev(), "ep-1")
    assert a == b


def test_the_key_separates_endpoints_events_and_resources():
    """Each of these must produce its own row, or one endpoint's delivery suppresses
    another's."""
    keys = {
        pol.dedupe_key(_ev(), "ep-1"),
        pol.dedupe_key(_ev(), "ep-2"),
        pol.dedupe_key(_ev(event_type="resource.reaped"), "ep-1"),
        pol.dedupe_key(_ev(resource_id="job:xyz"), "ep-1"),
    }
    assert len(keys) == 4


def test_a_bucket_is_what_lets_a_condition_repeat():
    """A budget breach is still true tomorrow. Without a bucket it would notify once
    and never again; with one it notifies once per day."""
    plain = pol.dedupe_key(_ev(event_type="cost.budget_exceeded"), "ep-1")
    mon = pol.dedupe_key(_ev(event_type="cost.budget_exceeded", dedupe_bucket="2026-07-29"), "ep-1")
    tue = pol.dedupe_key(_ev(event_type="cost.budget_exceeded", dedupe_bucket="2026-07-30"), "ep-1")
    assert plain != mon != tue
    assert mon != tue
    assert pol.day_bucket(datetime(2026, 7, 29, 23, 59)) == "2026-07-29"


def test_an_event_with_no_resource_id_still_keys_off_its_title():
    """cost/secret/drift events name no single resource; they must not all collapse
    onto one key."""
    a = pol.dedupe_key(_ev(event_type="secret.stale", resource_id="", title="3 stale secrets"), "e")
    b = pol.dedupe_key(_ev(event_type="secret.stale", resource_id="", title="4 stale secrets"), "e")
    assert a != b


def test_two_long_keys_that_share_a_prefix_do_not_collide():
    """Truncating a long key would make two distinct events share one row — and a
    dedupe collision doesn't duplicate a message, it silently drops one."""
    long_a = _ev(resource_id="clouddb:" + "a" * 300)
    long_b = _ev(resource_id="clouddb:" + "a" * 299 + "b")
    ka, kb = pol.dedupe_key(long_a, "ep-1"), pol.dedupe_key(long_b, "ep-1")
    assert ka != kb
    assert len(ka) <= 200 and len(kb) <= 200


# ── rendering ────────────────────────────────────────────────────────────────

def test_a_subject_is_one_bounded_line():
    _on()
    subj, _ = pol.render(_ev(title="line one\nline two\r\nline three"))
    assert "\n" not in subj and "\r" not in subj
    assert "line one line two line three" in subj

    long_subj, _ = pol.render(_ev(title="x" * 500))
    assert len(long_subj) <= 180


def test_the_subject_carries_the_severity_so_a_channel_can_be_skimmed():
    _on()
    subj, _ = pol.render(_ev(event_type="resource.reaped"))
    assert subj.startswith("[CRITICAL]")


def test_the_body_lists_the_facts_an_operator_asks_for_first():
    _on()
    _, body = pol.render(_ev(fields={"Expires": "2026-07-30 14:00 UTC"}))
    assert "aws / us-east-1" in body
    assert "Expires" in body and "2026-07-30 14:00 UTC" in body
    assert "vm" in body


def test_a_link_is_omitted_rather_than_emitted_broken():
    """The worker has no request context. With no base_url configured, a relative
    path would render as a dead link in Slack and reject the Teams card outright."""
    _on()
    assert pol.absolute_url("/inventory") == ""
    _, body = pol.render(_ev())
    assert "/inventory" not in body

    _on(notify_base_url="https://dash.corp.example/")
    assert pol.absolute_url("/inventory") == "https://dash.corp.example/inventory"
    _, body = pol.render(_ev())
    assert "https://dash.corp.example/inventory" in body


def test_an_already_absolute_url_is_not_prefixed_twice():
    _on(notify_base_url="https://dash.corp.example")
    assert pol.absolute_url("https://elsewhere/x") == "https://elsewhere/x"


# ── retry policy ─────────────────────────────────────────────────────────────

def test_backoff_grows_and_then_holds():
    seq = [pol.backoff_seconds(i) for i in range(6)]
    assert seq[:4] == list(pol.BACKOFF_SECONDS)
    assert seq[4] == seq[5] == pol.BACKOFF_SECONDS[-1]   # clamped, not IndexError
    assert all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))


def test_max_attempts_cannot_exceed_the_backoff_schedule():
    """A configured attempt count longer than the schedule would retry forever at the
    last delay with no terminal state."""
    _on(notify_max_attempts="99")
    assert pol.max_attempts() <= len(pol.BACKOFF_SECONDS) + 1
    _on(notify_max_attempts="0")
    assert pol.max_attempts() >= 1


def test_retry_after_is_honoured_but_capped():
    assert pol.retry_after_seconds("45", default=30) == 45
    assert pol.retry_after_seconds("99999", default=30) == pol.RETRY_AFTER_CAP_SECONDS
    assert pol.retry_after_seconds(None, default=30) == 30
    assert pol.retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT", default=30) == 30


# ── config accessors ─────────────────────────────────────────────────────────

def test_intervals_and_ceilings_have_floors_a_typo_cannot_defeat():
    _on(notify_flush_interval_s="0", notify_scan_interval_s="1",
        notify_http_timeout_s="0", notify_max_per_flush="0", notify_max_queue="0")
    assert pol.flush_interval_seconds() >= 5
    assert pol.scan_interval_seconds() >= 300
    assert pol.http_timeout_s() >= 1
    assert pol.max_per_flush() >= 1
    assert pol.max_queue() >= 1


def test_a_non_numeric_setting_falls_back_instead_of_raising():
    _on(notify_max_queue="lots")
    assert pol.max_queue() == 500
    _on(notify_retention_days="")
    assert pol.retention_days() == 30


def test_retention_can_be_switched_off_but_not_negative():
    _on(notify_retention_days="0")
    assert pol.retention_days() == 0
    _on(notify_retention_days="-5")
    assert pol.retention_days() == 0


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
