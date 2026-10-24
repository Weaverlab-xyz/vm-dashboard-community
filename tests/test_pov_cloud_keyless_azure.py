"""The POV cloud provider on Azure with no stored secret: SPIFFE everyday, WLC for jobs.

docs/profiles/pov/public-cloud.md → Keyless Azure. The Azure twin of
test_pov_cloud_keyless_aws.py + test_pov_cloud_wlc_split.py. The credential ladder itself
(azure_service._ensure_creds, cloud_federation.azure_credential) is pinned elsewhere; what
is pinned here is the POV half:

  * the panel's model declares what its Azure SPIFFE choices write — an undeclared bound
    field is DISCARDED on save;
  * Test connection names the credential and the app it acted as;
  * an Entra refusal is reported by its AADSTS code and what FIXES it — a federated
    credential mismatch and an unreachable issuer are fixed in different places — and
    the SDK's own text, which can carry correlation ids and the assertion's claims, does
    not reach the response;
  * in the split, the jobs' half is the unmetered session check and nothing is minted;
  * the panel never clears the client secret on a radio change, and its explicit removal
    keeps the client id (the federated app) and marks the secret touched.

No cloud is reachable: the clients and the thread hop are stubbed.

Run: python tests/test_pov_cloud_keyless_azure.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-keyless-azure-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-pov-keyless-azure")

try:
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard.api.setup import PovCloudFeatureConfig  # noqa: E402
from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import (azure_service, cloud_federation,  # noqa: E402
                                    config_service, dashboard_identity, pov_cloud_azure)
from web_dashboard.services import workload_credential_lease as leases  # noqa: E402
from web_dashboard.services import workload_credentials_service as wlc  # noqa: E402
from _settings_src import settings_source

Base.metadata.create_all(bind=engine)

_APP = "11111111-2222-3333-4444-555555555555"
_SUB = "99999999-8888-7777-6666-555555555555"


class _Patch:
    def __init__(self):
        self._undo = []

    def set(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._undo):
            setattr(obj, name, value)


class _Groups:
    def __init__(self, raises=None):
        self._raises = raises

    def list(self):
        if self._raises:
            raise self._raises
        return iter([object()])


class _Resource:
    def __init__(self, raises=None):
        self.resource_groups = _Groups(raises)


def _verify(patch, *, source=cloud_federation.SOURCE, active=True, raises=None,
            split=False, session=None):
    patch.set(pov_cloud_azure, "configured", lambda: True)
    patch.set(pov_cloud_azure, "default_region", lambda: "eastus2")
    patch.set(cloud_federation, "source", lambda cloud: source)
    patch.set(cloud_federation, "active", lambda cloud: active)
    patch.set(leases, "dynamic_enabled", lambda cloud: split)
    patch.set(leases, "everyday_from_federation", lambda cloud: split)
    patch.set(leases, "secret_name_for", lambda cloud, purpose: "dashboard-azure-provision")
    minted = []
    patch.set(leases, "credentials", lambda *a, **kw: minted.append(a) or None)
    patch.set(leases, "azure_credentials", lambda *a, **kw: minted.append(a) or None)
    real_get = config_service.get
    patch.set(config_service, "get",
              lambda k, *a, **kw: _APP if k == "azure_client_id" else real_get(k, *a, **kw))

    async def _clients():
        return _Resource(raises), None, None, _SUB
    patch.set(pov_cloud_azure, "_clients", _clients)

    async def _to_thread(fn, *args):
        return fn(*args)
    patch.set(azure_service, "_to_thread", _to_thread)
    patch.set(wlc, "configured", lambda: True)

    def _session():
        if session:
            raise session
        return {"ok": True}
    patch.set(wlc, "test_connection", _session)
    ok, msg = asyncio.run(pov_cloud_azure.verify())
    return ok, msg, minted


# ── the panel model ───────────────────────────────────────────────────────────

def test_the_panel_model_declares_what_the_azure_spiffe_choices_write():
    fields = PovCloudFeatureConfig.model_fields
    for name in ("dashboard_spiffe_identity_enabled", "dashboard_spiffe_aud_azure",
                 "wlc_azure_everyday_source", "azure_client_id", "azure_tenant_id",
                 "azure_subscription_id"):
        assert name in fields, f"PovCloudFeatureConfig does not declare {name}"
    assert fields["dashboard_spiffe_aud_azure"].annotation is bool
    assert fields["wlc_azure_everyday_source"].default == "lease"


def test_the_fields_are_the_keys_federation_reads():
    assert dashboard_identity.AUD_AZURE == "dashboard_spiffe_aud_azure"


# ── Test connection ───────────────────────────────────────────────────────────

def test_a_federated_pass_names_the_identity_and_the_app():
    patch = _Patch()
    try:
        ok, msg, _ = _verify(patch)
    finally:
        patch.undo()
    assert ok, msg
    assert "SPIFFE identity" in msg and _APP in msg and _SUB in msg, msg


def test_a_stored_secret_pass_says_so():
    patch = _Patch()
    try:
        ok, msg, _ = _verify(patch, source="stored key", active=False)
    finally:
        patch.undo()
    assert ok and "stored client secret" in msg, msg


def test_nothing_configured_gives_federations_reason():
    patch = _Patch()
    try:
        patch.set(pov_cloud_azure, "configured", lambda: False)
        patch.set(cloud_federation, "reason",
                  lambda cloud: "the Azure audience is not ticked")
        ok, msg = asyncio.run(pov_cloud_azure.verify())
    finally:
        patch.undo()
    assert not ok
    assert "SPIFFE identity" in msg and "the Azure audience is not ticked" in msg, msg


def test_a_federated_credential_mismatch_is_named_by_its_code_not_the_sdk_text():
    sdk_text = ("AADSTS70021: No matching federated identity record found for presented "
                "assertion. Trace ID: 0f0f-secret-trace Correlation ID: abcd-correlation")
    patch = _Patch()
    try:
        ok, msg, _ = _verify(patch, raises=Exception(sdk_text))
    finally:
        patch.undo()
    assert not ok
    assert "AADSTS70021" in msg and "federated identity credential" in msg, msg
    assert "Trace ID" not in msg and "abcd-correlation" not in msg, msg


def test_an_unvalidatable_token_points_at_the_issuers_keys():
    patch = _Patch()
    try:
        patch.set(dashboard_identity, "issuer", lambda: "https://pov-agents.example.com/spiffe")
        ok, msg, _ = _verify(patch, raises=Exception("AADSTS50027: JWT token is invalid"))
    finally:
        patch.undo()
    assert not ok and "https://pov-agents.example.com/spiffe/keys" in msg, msg


def test_a_failure_with_no_code_stays_generic():
    patch = _Patch()
    try:
        ok, msg, _ = _verify(patch, raises=RuntimeError("socket blew up at 10.0.0.4"))
    finally:
        patch.undo()
    assert not ok and "did not answer" in msg and "10.0.0.4" not in msg, msg


def test_the_split_proves_both_halves_and_mints_nothing():
    patch = _Patch()
    try:
        ok, msg, minted = _verify(patch, split=True)
    finally:
        patch.undo()
    assert ok, msg
    assert "SPIFFE identity" in msg and "dashboard-azure-provision" in msg, msg
    assert "not minted" in msg, msg
    assert not minted, f"Test connection minted a billed lease: {minted}"


def test_a_refused_session_fails_the_split():
    patch = _Patch()
    try:
        ok, msg, _ = _verify(patch, split=True,
                             session=wlc.WorkloadCredentialsError("HTTP 401: Access denied for this site"))
    finally:
        patch.undo()
    assert not ok and "Access denied for this site" in msg, msg


# ── the panel ─────────────────────────────────────────────────────────────────

def _method(src, name):
    start = src.index(f"    {name}(")
    return src[start:src.index("\n    },", start)]


def test_choosing_an_azure_source_never_touches_the_app_or_its_secret():
    body = _method(settings_source(), "choosePovAzureSource")
    assert "dashboard_spiffe_aud_azure = true" in body
    assert "wlc_azure_everyday_source = src === 'spiffe_wlc' ? 'federation' : 'lease'" in body
    for key in ("azure_client_secret", "azure_client_id", "wlc_azure_enabled",
                "wlc_azure_secret_name"):
        assert key not in body, f"the Azure radio writes {key}"


def test_removing_the_stored_secret_keeps_the_app_and_marks_it_touched():
    body = _method(settings_source(), "removePovAzureStoredSecret")
    assert "panelSecretsTouched.push('azure_client_secret')" in body, body
    assert "azure_client_secret = ''" in body, body
    assert "azure_client_id" not in body, (
        "the client id is the federated app and must survive removing the secret")


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
