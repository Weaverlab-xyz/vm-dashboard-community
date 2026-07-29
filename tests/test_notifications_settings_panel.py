"""Wiring tests for the Notifications Settings panel and its API.

The panel is driven by the generic feature-config machinery in api/setup.py, so what's
worth pinning is the wiring rather than the form markup:

  * the feature is registered, so ``/api/setup/feature/notifications`` exists at all;
  * every numeric field is a plain ``int``. ``_read_feature`` keys off
    ``info.annotation is int``, so an ``Optional[int]`` reads back as ``""`` for an
    unset key and 422s the whole panel on save — a bug that only appears on a fresh
    install, which is the worst time to find it;
  * its fields match the keys ``notify_policy`` actually reads — drift here produces a
    panel that saves values nothing consumes, or knobs nobody can reach;
  * it ships off AND in dry-run, so enabling it can't page a channel by surprise;
  * the endpoints API never hands back a URL or a secret.

Skips cleanly without fastapi. Standalone:
    python tests/test_notifications_settings_panel.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-notifications-panel")

try:
    from web_dashboard.api import setup as setup_api
    from web_dashboard.api import notifications as notifications_api
    from web_dashboard.services import notify_policy, notify_transports
except Exception as exc:  # fastapi/sqlalchemy absent outside CI
    setup_api = None
    _IMPORT_ERR = exc


def test_notifications_feature_is_registered():
    assert "notifications" in setup_api._FEATURE_MODELS, \
        "/api/setup/feature/notifications would 404 without a registry entry"


def test_notifications_has_a_real_toggle():
    """Not config-only: unlike SSO there is no "it's live once configured" signal —
    an endpoint can exist for a long time before anyone wants it fed."""
    assert "notifications" not in setup_api._CONFIG_ONLY_FEATURES
    assert "enabled" in setup_api.NotificationsFeatureConfig.model_fields


def test_it_ships_off_and_in_dry_run():
    """Two brakes. Enabling this against a live estate could otherwise produce hundreds
    of messages in the first pass."""
    cfg = setup_api.NotificationsFeatureConfig()
    assert cfg.enabled is False
    assert cfg.notify_dry_run is True


def test_every_numeric_field_is_a_plain_int():
    """_read_feature branches on `info.annotation is int` (api/setup.py). An
    Optional[int] reads back "" for an unset key and 422s the entire panel on save."""
    for name, info in setup_api.NotificationsFeatureConfig.model_fields.items():
        if not name.startswith("notify_"):
            continue
        ann = info.annotation
        assert ann in (int, bool, str), (
            f"{name} is annotated {ann!r}; panel fields must be plain int/bool/str")
        if "interval" in name or "timeout" in name or name.endswith(("_s", "_days")) \
                or "max_" in name:
            assert ann is int, f"{name} looks numeric but is annotated {ann!r}"


def test_panel_fields_match_what_the_policy_reads():
    """Guard against drift between the panel and services/notify_policy.py. A knob in
    the panel that the policy never reads is a lie to the operator; a knob the policy
    reads that the panel omits is unreachable."""
    model_fields = set(setup_api.NotificationsFeatureConfig.model_fields) - {"enabled"}
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "notify_policy.py"),
               encoding="utf-8").read()
    unread = {f for f in model_fields if f'"{f}"' not in src}
    assert not unread, (
        f"the panel offers keys notify_policy never reads: {unread}")


def test_the_enable_key_is_the_one_the_policy_checks():
    """_feature_to_cfg_key derives `notifications_enabled` from the feature name. If
    the policy checked anything else, the toggle would be inert."""
    assert setup_api._feature_to_cfg_key("notifications") == "notifications_enabled"
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "notify_policy.py"),
               encoding="utf-8").read()
    assert '"notifications_enabled"' in src


def test_no_endpoint_credentials_live_in_the_panel():
    """URLs and HMAC secrets are rows in notification_endpoints, not config keys —
    there can be several, and they need their own CRUD. A stray key here would be an
    unredacted second home for a credential."""
    for name in setup_api.NotificationsFeatureConfig.model_fields:
        assert "secret" not in name, f"{name} looks like a credential in the panel model"
        assert not name.endswith("_url") or name == "notify_base_url", \
            f"{name} looks like a webhook URL in the panel model"


def test_the_default_event_list_is_all_known_events():
    """The panel default and the policy default must agree, or a fresh install silently
    drops events depending on which one wins."""
    panel = setup_api.NotificationsFeatureConfig().notify_event_types
    assert notify_policy.parse_event_types(panel) == \
        notify_policy.parse_event_types(notify_policy.DEFAULT_EVENT_TYPES)


# ── the endpoints API ────────────────────────────────────────────────────────

def test_the_endpoint_routes_are_exposed():
    paths = {r.path for r in notifications_api.router.routes}
    for expected in ("/api/notifications/endpoints",
                     "/api/notifications/endpoints/{endpoint_id}",
                     "/api/notifications/endpoints/{endpoint_id}/test",
                     "/api/notifications/deliveries",
                     "/api/notifications/summary"):
        assert expected in paths, f"{expected} is not routed"


def test_every_route_is_admin_only():
    """The delivery log holds the rendered body of every alert, naming resources the
    reader may not otherwise be able to see."""
    from web_dashboard.api.auth import require_admin
    for route in notifications_api.router.routes:
        deps = getattr(route, "dependant", None)
        assert deps is not None, f"{route.path} has no dependant"
        calls = [d.call for d in deps.dependencies]
        assert require_admin in calls, f"{route.path} is not admin-gated"


def test_a_patch_can_omit_every_field():
    """A toggle-only PATCH must not blank the URL or the secret. exclude_unset is what
    makes that work, so the model has to allow every field to be absent."""
    patch = notifications_api.EndpointPatch()
    assert patch.model_dump(exclude_unset=True) == {}
    partial = notifications_api.EndpointPatch(enabled=False)
    assert partial.model_dump(exclude_unset=True) == {"enabled": False}


def test_the_known_formats_all_have_builders():
    for fmt in notify_transports.FORMATS:
        assert fmt in notify_transports.BUILDERS


def test_a_url_without_a_scheme_is_refused():
    """An operator pasting a bare host would otherwise produce an endpoint that fails
    on every send with an opaque httpx error."""
    from fastapi import HTTPException
    for bad in ("", "   ", "hooks.slack.com/services/x", "ftp://x/y"):
        try:
            notifications_api._validate(bad, "custom")
        except HTTPException:
            continue
        raise AssertionError(f"{bad!r} should have been refused")
    notifications_api._validate("https://hooks.slack.com/services/x", "slack")


def test_an_unknown_format_is_refused():
    from fastapi import HTTPException
    try:
        notifications_api._validate("https://x/y", "carrier-pigeon")
    except HTTPException as exc:
        assert "carrier-pigeon" in str(exc.detail)
    else:
        raise AssertionError("an unknown format should be refused")


if __name__ == "__main__":
    if setup_api is None:
        print(f"SKIP all: dependencies unavailable ({_IMPORT_ERR})")
        sys.exit(0)
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
