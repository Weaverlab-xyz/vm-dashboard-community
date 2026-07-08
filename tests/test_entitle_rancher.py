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


def _install_stubs():
    cfg = types.ModuleType("web_dashboard.services.config_service")
    store = {
        "entitle_owner_id": "owner-1",
        "entitle_workflow_id": "wf-1",
        "entitle_agent_token_name": "agent-1",
        "entitle_rancher_app_slug": "rancher",
        "entitle_api_key": "k",
    }
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


def test_generate_rancher_hcl_private():
    hcl = ers._generate_rancher_hcl(name="central-rancher", url=URL_SENTINEL, verify=False, private=True)
    assert 'application = { name = "rancher" }' in hcl
    assert "connection_json = jsonencode({" in hcl
    assert URL_SENTINEL in hcl
    assert "access_token = var.rancher_access_token" in hcl
    assert "secret_key   = var.rancher_secret_key" in hcl
    assert "verify       = false" in hcl
    assert 'variable "rancher_access_token" { sensitive = true }' in hcl
    assert 'variable "rancher_secret_key" { sensitive = true }' in hcl
    assert "agent_token" in hcl   # private → the shared Entitle agent brokers it


def test_register_rancher_rejects_non_pair_token():
    try:
        asyncio.run(ers.register_rancher(name="x", server_url=URL_SENTINEL, api_token="no-colon-here"))
    except ers.EntitleRegistrationError as e:
        assert "access:secret" in str(e)
    else:
        raise AssertionError("expected EntitleRegistrationError for a non-pair token")


if __name__ == "__main__":
    test_generate_rancher_hcl_private()
    test_register_rancher_rejects_non_pair_token()
    print("ok")
