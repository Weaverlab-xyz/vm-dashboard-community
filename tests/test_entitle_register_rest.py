"""Registering a Cloud Functions adapter as an Entitle REST integration.

The HCL generation is pure string-building, so the whole thing is provable offline.
What matters here is that the generated integration actually matches the routes the
adapter serves — a mismatch produces an integration that saves cleanly in Entitle
and 404s on every real grant.
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

CONF = {
    "entitle_owner_id": "owner-1",
    "entitle_workflow_id": "wf-1",
    "entitle_api_key": "key-1",
}


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# config_service is stubbed UNCONDITIONALLY, not just when its dependencies are
# missing: the module under test reads owner/workflow ids through it, and the real
# one reads the encrypted app_config table — which is empty on a CI runner, so every
# test would fail with "entitle_owner_id is not configured". Gating this on whether
# pydantic happens to be installed made the suite pass locally and fail in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)))

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace())

from web_dashboard.services import entitle_registration_service as ers

BASE = "https://abc123.lambda-url.us-east-1.on.aws"


def _hcl(**overrides):
    kwargs = {"name": "appdb-grant", "base_url": BASE, "private": False,
              "ephemeral": True, "auth_header": "Authorization"}
    kwargs.update(overrides)
    return ers._generate_rest_hcl(**kwargs)


# ── The routes must match what the adapter serves ────────────────────────────

def test_every_generated_path_is_a_route_the_adapter_actually_serves():
    """The one mismatch that produces an integration which saves cleanly in Entitle
    and then 404s on every real grant."""
    from web_dashboard import functions  # noqa: F401
    from fnworkloads import db_grant

    served = {route for _method, route in db_grant._ROUTES}
    hcl = _hcl()
    for _field, path in ers._REST_ROUTES + ers._REST_EPHEMERAL_ROUTES:
        assert path in served, f"{path} is configured but the adapter does not serve it"
        assert BASE + path in hcl, f"{path} missing from the generated HCL"


def test_the_adapter_serves_everything_entitle_will_call():
    """The other direction: a route the adapter has but Entitle is never told about
    is dead code, and one Entitle needs but is not configured is a silent gap."""
    from web_dashboard import functions  # noqa: F401
    from fnworkloads import db_grant

    configured = {path for _f, path in ers._REST_ROUTES + ers._REST_EPHEMERAL_ROUTES}
    served = {route for _method, route in db_grant._ROUTES}
    # check_config and get_asset_permissions are served but not configured by us:
    # Entitle probes the first itself and the second is an alternative to
    # get_all_permissions, so neither is a gap.
    assert configured <= served, f"configured but unserved: {configured - served}"
    assert served - configured <= {"/check_config", "/get_asset_permissions"}, \
        f"served but never configured: {served - configured}"


def test_full_urls_are_used_so_a_base_path_survives():
    """Azure's base carries an /api prefix. Relative paths under schema+host would
    drop it; full URLs cannot."""
    hcl = ers._generate_rest_hcl(
        name="x", base_url="https://app.azurewebsites.net/api", private=False,
        ephemeral=True, auth_header="Authorization")
    assert '"https://app.azurewebsites.net/api/give_access"' in hcl


def test_a_trailing_slash_on_the_base_does_not_double_up():
    hcl = _hcl(base_url=BASE + "/")
    assert BASE + "//" not in hcl
    assert BASE + "/give_access" in hcl


# ── Ephemeral is what unlocks MySQL ──────────────────────────────────────────

def test_ephemeral_adds_the_actor_routes_and_enables_account_creation():
    hcl = _hcl(ephemeral=True)
    assert "create_actor_path" in hcl and "delete_actor_path" in hcl
    assert "allow_creating_accounts = true" in hcl


def test_non_ephemeral_omits_both_actor_routes_together():
    """Entitle needs BOTH to manage a temporary account, so they are
    all-or-nothing — shipping one would be a half-configured lifecycle."""
    hcl = _hcl(ephemeral=False)
    assert "create_actor_path" not in hcl and "delete_actor_path" not in hcl
    assert "allow_creating_accounts = false" in hcl


def test_mysql_is_no_longer_limited_to_persistent_roles():
    """The constraint was never MySQL's — it was Entitle's MySQL CONNECTOR's, and a
    REST adapter does not use that connector. This is the point of the whole
    exercise, so it gets its own assertion."""
    assert "allow_creating_accounts = true" in _hcl(ephemeral=True)


# ── Secret handling ──────────────────────────────────────────────────────────

def test_the_shared_secret_is_a_terraform_variable_not_a_literal():
    """The HCL is written to a workdir on disk; a literal secret there would
    outlive the apply."""
    hcl = _hcl()
    assert "var.rest_secret" in hcl
    assert 'variable "rest_secret" { sensitive = true }' in hcl
    assert "supersecret" not in hcl


def test_registration_without_a_secret_is_refused():
    import asyncio
    try:
        asyncio.run(ers.register_rest(name="x", base_url=BASE, shared_secret=""))
    except ers.EntitleRegistrationError as exc:
        assert "shared secret" in str(exc)
    else:
        raise AssertionError("registered with no shared secret")


def test_the_auth_header_is_configurable_but_defaults_to_authorization():
    assert '"Authorization" = "Bearer ${var.rest_secret}"' in _hcl()
    assert '"X-Dashboard-Secret" = "Bearer ${var.rest_secret}"' in \
        _hcl(auth_header="X-Dashboard-Secret")


# ── Shape ────────────────────────────────────────────────────────────────────

def test_the_application_slug_is_lowercase():
    """The provider validates this client-side at plan time; any uppercase letter
    fails instantly with 'Lowercase Validation Failed'."""
    slug = ers._rest_app_slug()
    assert slug == slug.lower() and slug


def test_public_registration_attaches_no_agent():
    """The FUNCTION is VPC-attached; the ENDPOINT is public, which is the whole
    point of the adapter — requiring an agent would defeat it."""
    assert "agent_token" not in _hcl(private=False)


def test_private_registration_requires_an_agent_token():
    try:
        _hcl(private=True)
    except ers.EntitleRegistrationError as exc:
        assert "agent" in str(exc).lower()
    else:
        raise AssertionError("private registration accepted with no agent token")


def test_the_hcl_declares_an_integration_and_outputs_its_id():
    hcl = _hcl()
    assert "resource \"entitle_integration\"" in hcl
    assert "output \"integration_id\"" in hcl
    assert "owner" in hcl and "workflow" in hcl


def test_a_missing_base_url_is_refused():
    for bad in ("", "   ", None):
        try:
            ers._rest_connection_json_hcl(base_url=bad, ephemeral=True,
                                          auth_header="Authorization")
        except ers.EntitleRegistrationError:
            pass
        else:
            raise AssertionError(f"accepted base_url {bad!r}")


def test_the_connection_json_is_valid_json_once_the_variable_is_bound():
    """It is emitted as HCL jsonencode({...}); the field names must still be exactly
    what the integration config expects."""
    hcl = _hcl()
    for field, _path in ers._REST_ROUTES + ers._REST_EPHEMERAL_ROUTES:
        assert f"{field} = " in hcl, field
    assert "headers = {" in hcl


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
