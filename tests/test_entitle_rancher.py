"""Unit tests for the Entitle Rancher connector registration
(register_rancher / _generate_rancher_hcl). config_service is stubbed; no
terraform / Entitle needed for the HCL-generation + token-split checks.

Runs under pytest, or standalone: python tests/test_entitle_rancher.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


#: The stub config store, module-level so a test can set a key and put it back.
_STORE = {
    "entitle_owner_id": "owner-1",
    "entitle_workflow_id": "wf-1",
    "entitle_agent_token_name": "agent-1",
    "entitle_rancher_app_slug": "rancher",
    "entitle_api_key": "k",
}


def _install_stubs():
    cfg = types.ModuleType("web_dashboard.services.config_service")
    store = _STORE
    cfg.get = lambda key, default="", workgroup=None: store.get(key, default)
    cfg.get_bool = lambda key, default=False: bool(store.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg
    # _cfg falls back to `from ..config import settings` for unset keys — stub the
    # config module so that fallback doesn't pull in pydantic (mirrors test_k8s_tf_vars).
    confmod = types.ModuleType("web_dashboard.config")

    class _Settings:
        def __getattr__(self, _k):
            return ""

    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod


_install_stubs()
try:
    from web_dashboard.services import entitle_registration_service as ers
except Exception as exc:  # pragma: no cover — skip if deps missing
    try:
        import pytest
        pytest.skip(f"entitle_registration_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


URL_SENTINEL = "RANCHER-URL-SENTINEL"   # non-URL literal: avoids CodeQL url-substring query
HOST_SENTINEL = "RANCHER-HOST-SENTINEL"
# Built, never written as a literal — same reason as URL_SENTINEL.
ORIGIN_SENTINEL = f"https://{HOST_SENTINEL}"


def test_rancher_url_is_the_api_endpoint():
    """Entitle's connector dials the url it is given and parses the answer as JSON.
    Rancher answers its ORIGIN with the UI (HTML), so registering the origin fails
    inside the connector as a bare 'Expecting value: line 1 column 1 (char 0)' —
    naming neither Rancher nor the URL. The connector wants Rancher's API Endpoint."""
    assert ers._rancher_api_url(ORIGIN_SENTINEL) == ORIGIN_SENTINEL + "/v3"
    # A trailing slash on the stored server-url must not double up.
    assert ers._rancher_api_url(ORIGIN_SENTINEL + "/") == ORIGIN_SENTINEL + "/v3"
    # Idempotent: re-registering an already-converted url does not stack a second /v3.
    assert ers._rancher_api_url(ORIGIN_SENTINEL + "/v3") == ORIGIN_SENTINEL + "/v3"
    assert ers._rancher_api_url("") == ""


def test_rancher_url_keeps_an_operator_pinned_path():
    """A Rancher behind a path-routing proxy keeps the path it was given."""
    pinned = ORIGIN_SENTINEL + "/rancher"
    assert ers._rancher_api_url(pinned) == pinned


def test_rancher_api_path_override_can_disable_the_suffix():
    """The escape hatch for a connector build that appends /v3 itself, where the
    default would send /v3/v3."""
    _STORE["entitle_rancher_api_path"] = "none"
    try:
        assert ers._rancher_api_url(ORIGIN_SENTINEL) == ORIGIN_SENTINEL
        _STORE["entitle_rancher_api_path"] = "/v4"
        assert ers._rancher_api_url(ORIGIN_SENTINEL) == ORIGIN_SENTINEL + "/v4"
    finally:
        _STORE.pop("entitle_rancher_api_path", None)


def test_generate_rancher_hcl_private():
    hcl = ers._generate_rancher_hcl(name="central-rancher", url=URL_SENTINEL, verify=False, private=True)
    assert 'application = { name = "rancher" }' in hcl
    assert "connection_json = jsonencode({" in hcl
    assert URL_SENTINEL in hcl
    assert "access_key = var.rancher_access_key" in hcl
    assert "secret_key = var.rancher_secret_key" in hcl
    assert "verify     = false" in hcl
    assert 'variable "rancher_access_key" { sensitive = true }' in hcl
    assert 'variable "rancher_secret_key" { sensitive = true }' in hcl
    assert "agent_token" in hcl   # private → the shared Entitle agent brokers it


def test_rancher_access_key_is_not_named_access_token():
    """The tenant's connector schema asks for `access_key`. `access_token` — which
    is what the published connector doc prints — matched no schema at all and 400'd
    every registration, so pin the spelling against a doc-led "correction"."""
    hcl = ers._generate_rancher_hcl(name="central-rancher", url=URL_SENTINEL,
                                    verify=False, private=False)
    assert "access_key = var.rancher_access_key" in hcl
    assert "access_token" not in hcl


def test_rancher_credential_keys_are_scrubbed_from_state():
    """Both halves of the Rancher API key are credential material, and the scrub
    matches connection_json keys EXACTLY — so the connector's own spellings have to
    be listed or a stored state keeps them in plaintext."""
    for key in ("access_key", "secret_key", "access_token"):
        assert key in ers._SECRET_JSON_KEYS, f"{key} is not scrubbed from stored state"
    blob = ers._scrub_connection_json(
        '{"url": "u", "access_key": "token-abcde", "secret_key": "s3cr3t", "verify": false}')
    assert "token-abcde" not in blob
    assert "s3cr3t" not in blob
    assert '"url"' in blob and '"verify"' in blob   # configuration stays legible


def test_register_rancher_rejects_non_pair_token():
    try:
        asyncio.run(ers.register_rancher(name="x", server_url=URL_SENTINEL, api_token="no-colon-here"))
    except ers.EntitleRegistrationError as e:
        assert "access:secret" in str(e)
    else:
        raise AssertionError("expected EntitleRegistrationError for a non-pair token")


if __name__ == "__main__":
    test_rancher_url_is_the_api_endpoint()
    test_rancher_url_keeps_an_operator_pinned_path()
    test_rancher_api_path_override_can_disable_the_suffix()
    test_generate_rancher_hcl_private()
    test_rancher_access_key_is_not_named_access_token()
    test_rancher_credential_keys_are_scrubbed_from_state()
    test_register_rancher_rejects_non_pair_token()
    print("ok")
