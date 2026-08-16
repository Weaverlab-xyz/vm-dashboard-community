"""Guard: a redacted value never gets stored as if it were a real one.

``GET /api/setup/config`` replaces the four keys in ``config_service._SECRET_KEYS``
with bullets. Anything that reads config back and writes it somewhere else — this
migration tool, but also any future export/clone/backup helper — is holding those
bullets, and ``POST /api/setup/import`` stores what it is given.

The result of getting this wrong is worse than a failed import: the key *looks*
configured in the Settings panel, so nobody re-enters it, and the failure surfaces
much later as an authentication error from a cloud provider.

``_write_feature`` has skipped bulleted values for a long time; this asserts
``import_config`` does the same, and that the client refuses to send them anyway.
Both halves, because either one alone leaves a sharp edge for the next caller.

Runs under pytest, or standalone:  python tests/test_config_migrate_sentinel.py
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.api import setup as setup_api
from web_dashboard.scripts.config_migrate import classify
from web_dashboard.scripts.config_migrate.__main__ import _payload_from_bundle

BULLETS = "•" * 8


class _Tasks:
    """Stands in for FastAPI's BackgroundTasks."""

    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **kw):
        self.tasks.append(fn)


def _run_import(config: dict) -> dict:
    """Call ``import_config`` against a stubbed config store; return what it wrote.

    Stubs only the boundary — auth, persistence, cache invalidation — so the
    transformation under test runs for real.
    """
    from web_dashboard.services import config_service, region_config

    written: dict = {}
    merged: dict = {}
    saved = {
        "is_setup_complete": config_service.is_setup_complete,
        "set_many": config_service.set_many,
        "invalidate": config_service.invalidate,
        "merge_region_fields": region_config.merge_region_fields,
        "_require_admin": setup_api._require_admin,
    }
    config_service.is_setup_complete = lambda: True
    config_service.set_many = lambda pairs, **kw: written.update(pairs)
    config_service.invalidate = lambda: None
    region_config.merge_region_fields = (
        lambda cloud, updates: merged.setdefault(cloud, {}).update(updates))
    setup_api._require_admin = lambda request: None
    try:
        setup_api.import_config(setup_api.HeadlessImport(config=config),
                                request=None, background_tasks=_Tasks())
    finally:
        config_service.is_setup_complete = saved["is_setup_complete"]
        config_service.set_many = saved["set_many"]
        config_service.invalidate = saved["invalidate"]
        region_config.merge_region_fields = saved["merge_region_fields"]
        setup_api._require_admin = saved["_require_admin"]
    return written


def test_import_endpoint_skips_bulleted_values():
    written = _run_import({
        "azure_client_id": "real-client-id",
        "azure_client_secret": BULLETS,
        "aws_secret_access_key": BULLETS,
    })
    assert "azure_client_id" in written, "a real value must still be written"
    for key in ("azure_client_secret", "aws_secret_access_key"):
        assert key not in written, (
            f"{key} was stored as the redaction sentinel — it will look configured "
            f"in the UI and fail at cloud-call time")


def test_import_endpoint_matches_write_feature():
    """Same rule, same prefix test, so the two write paths cannot diverge.

    ``_write_feature`` is the older of the two and is the precedent being
    matched; if its sentinel ever changes, this fails rather than leaving
    ``import_config`` behind.
    """
    from web_dashboard.services import config_service

    written: dict = {}
    saved = config_service.set_many
    config_service.set_many = lambda pairs, **kw: written.update(pairs)
    try:
        setup_api._write_feature("vmware", {"enabled": True, "vmware_host": BULLETS})
    finally:
        config_service.set_many = saved
    assert "vmware_host" not in written, "_write_feature stopped skipping bullets"
    assert classify.MASK_SENTINEL_PREFIX == "••", (
        "the migration tool's sentinel prefix has drifted from the one both "
        "server-side writers test for")


def test_real_values_are_unaffected():
    """The guard must not eat a legitimate value that merely contains a bullet."""
    written = _run_import({"notify_event_types": "job.failed • job.succeeded"})
    assert written["notify_event_types"] == "job.failed • job.succeeded", (
        "a bullet mid-string is content, not redaction — only a leading sentinel counts")


def test_client_refuses_to_send_bulleted_values():
    """Belt and braces: the tool drops them before the request is built.

    The endpoint guard protects every caller; this one means an operator reading
    the diff sees the key called out rather than silently absent.
    """
    doc = {
        "schema": 1,
        "config": {"azure_client_id": "real", "azure_client_secret": BULLETS},
        "regions": {},
        "notification_endpoints": [],
    }
    args = argparse.Namespace(include_on_prem=False, regions="merge", only=[])
    payload, refused, _dropped = _payload_from_bundle(doc, args)
    assert payload == {"azure_client_id": "real"}
    assert refused == {"azure_client_secret": classify.MASKED}


def test_diff_output_never_contains_a_config_value():
    """No config value reaches stderr, secret-shaped or not.

    The earlier version printed values and redacted whatever
    ``classify.is_secret`` recognised, which is the wrong way round: choosing
    what to *migrate* should fail open, because a credential that silently does
    not cross breaks the target, while choosing what to *display* must fail
    closed, because a name heuristic that is wrong once puts a live credential
    on a terminal or in a CI log. CodeQL flagged exactly this
    (py/clear-text-logging-sensitive-data) and was right to.
    """
    from web_dashboard.scripts.config_migrate.__main__ import _show

    cases = [
        # A key the heuristic knows is a secret…
        ("bt_client_secret", "s3cr3t-value-aaaaaaaa"),
        # …and one it does not. This is the case that used to leak.
        ("entitle_agent_existing_secret_helm_key", "another-live-credential"),
        ("some_key_invented_next_month", "hunter2"),
        # Vault references are only pointers, but they still name a vault and a
        # secret, so they are not free to print either.
        ("epml_pat", "azure_kv://primary/epml-pat"),
        ("azure_location", "eastus2"),
    ]
    for key, value in cases:
        rendered = _show(key, value)
        assert value not in rendered, f"{key}: value leaked into diff output"
        assert str(len(value)) in rendered, f"{key}: length should still be reported"

    # Different values must be distinguishable, or the diff cannot be read.
    assert _show("k", "alpha") != _show("k", "bravo")
    assert _show("k", "alpha") == _show("k", "alpha")
    assert _show("k", "") == "(empty)"


def test_region_values_reach_the_payload_unfiltered():
    """Region fields bypass the exclusion check, so the second guard is load-bearing.

    ``_payload_from_bundle`` runs ``exclusion_reason`` over the flat config only;
    region keys are built by ``regions.to_import_keys`` and merged in afterwards.
    A bulleted value in a region set therefore survives that function, and is
    caught later by the sweep in ``cmd_diff``. Asserting the gap exists keeps
    anyone from "simplifying" that second sweep away.
    """
    doc = {
        "schema": 1,
        "config": {},
        "regions": {"azure": {"eastus2": {"resource_group": BULLETS}}},
        "notification_endpoints": [],
    }
    args = argparse.Namespace(include_on_prem=False, regions="merge", only=[])
    payload, _refused, _dropped = _payload_from_bundle(doc, args)
    assert payload == {"azure_region.eastus2.resource_group": BULLETS}, (
        "if this now filters region values, the sweep in cmd_diff can go — but "
        "check both before removing either")
    # …and the sweep that actually stops it.
    assert [k for k, v in payload.items() if classify.is_masked(v)] == [
        "azure_region.eastus2.resource_group"]


def test_hand_edited_bundle_cannot_reintroduce_a_denied_key():
    """A bundle is a file someone may edit. Re-check on import, not just export."""
    doc = {
        "schema": 1,
        "config": {"aws_region": "us-east-2",
                   "public_base_url": "http://localhost:8001",
                   "rancher_api_token": "token-abc"},
        "regions": {},
        "notification_endpoints": [],
    }
    args = argparse.Namespace(include_on_prem=False, regions="merge", only=[])
    payload, refused, _dropped = _payload_from_bundle(doc, args)
    assert payload == {"aws_region": "us-east-2"}
    assert refused == {"public_base_url": classify.INSTANCE_LOCAL,
                       "rancher_api_token": classify.RUNTIME_HANDLE}


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
    sys.exit(1 if failures else 0)
