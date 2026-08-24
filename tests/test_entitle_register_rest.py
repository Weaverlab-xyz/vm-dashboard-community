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
    for ephemeral in (True, False):
        hcl = _hcl(ephemeral=ephemeral)
        for _field, path in ers._rest_routes(ephemeral):
            assert path in served, f"{path} is configured, the adapter does not serve it"
            assert f'"{path}"' in hcl, f"{path} missing from the generated HCL"


def test_the_adapter_serves_everything_entitle_will_call():
    """The other direction: a route the adapter has but Entitle is never told about
    is dead code, and one Entitle needs but is not configured is a silent gap."""
    from web_dashboard import functions  # noqa: F401
    from fnworkloads import db_grant

    configured = {path for _f, path in
                  ers._REST_STANDING_ROUTES + ers._REST_EPHEMERAL_ROUTES}
    served = {route for _method, route in db_grant._ROUTES}
    # check_config and get_asset_permissions are served but not configured by us:
    # Entitle probes the first itself and the second is an alternative to
    # get_all_permissions, so neither is a gap.
    assert configured <= served, f"configured but unserved: {configured - served}"
    assert served - configured <= {"/check_config", "/get_asset_permissions"}, \
        f"served but never configured: {served - configured}"


# ── The two modes are disjoint key sets, not a base plus extras ──────────────

def test_the_modes_never_share_one_payload():
    """Entitle validates connection_json per MODE with additionalProperties: false,
    so a union of the two key sets is rejected outright — and the Terraform provider
    does not run that validation, so it saves and then fails every resource sync.
    That is the exact failure this file previously encoded as correct."""
    standing = {f for f, _p in ers._REST_STANDING_ROUTES}
    ephemeral = {f for f, _p in ers._REST_EPHEMERAL_ROUTES}
    assert "get_actors_path" in standing and "get_actors_path" not in ephemeral
    for field in ("give_access_path", "revoke_access_path"):
        assert field in standing and field not in ephemeral, field
    for field in ("create_actor_path", "delete_actor_path"):
        assert field in ephemeral and field not in standing, field


def test_ephemeral_sends_only_the_four_keys_the_mode_accepts():
    hcl = _hcl(ephemeral=True)
    for absent in ("get_actors_path", "give_access_path", "revoke_access_path"):
        assert absent not in hcl, f"{absent} is not valid in Ephemeral mode"


def test_the_host_is_declared_once_rather_than_repeated_per_route():
    """The full-URL-per-field form is what a live tenant answered 'Missing host
    scope!' to; the split form also makes the host one value to eyeball."""
    hcl = _hcl()
    assert 'schema = "https"' in hcl
    assert 'host   = "abc123.lambda-url.us-east-1.on.aws"' in hcl
    assert BASE + "/get_assets" not in hcl, "paths must be relative to host"


def test_an_azure_base_path_is_kept_on_every_route():
    """Azure serves under /api and the base URL carries it. Dropping the prefix when
    switching to relative paths would 404 every call — this is the case the old
    full-URL shortcut existed to avoid."""
    hcl = ers._generate_rest_hcl(
        name="x", base_url="https://app.azurewebsites.net/api", private=False,
        ephemeral=True, auth_header="Authorization")
    assert 'host   = "app.azurewebsites.net"' in hcl
    assert '"/api/create_actor"' in hcl
    assert '"/create_actor"' not in hcl.replace('"/api/create_actor"', "")


def test_a_trailing_slash_on_the_base_does_not_double_up():
    hcl = _hcl(base_url=BASE + "/")
    assert "//get_assets" not in hcl
    assert '"/get_assets"' in hcl
    assert 'host   = "abc123.lambda-url.us-east-1.on.aws"' in hcl


def test_a_base_url_with_no_host_is_refused():
    for bad in ("https://", "/just/a/path"):
        try:
            ers._split_base_url(bad)
        except ers.EntitleRegistrationError:
            pass
        else:
            raise AssertionError(f"accepted base_url {bad!r}")


# ── Ephemeral is what unlocks MySQL ──────────────────────────────────────────

def test_ephemeral_adds_the_actor_routes_and_enables_account_creation():
    hcl = _hcl(ephemeral=True)
    assert "create_actor_path" in hcl and "delete_actor_path" in hcl
    assert "allow_creating_accounts = true" in hcl


def test_the_route_set_and_the_account_flag_cannot_disagree():
    """Ephemeral routes with allow_creating_accounts=false describes a lifecycle
    Entitle will not run; the reverse describes one it cannot. They come from the
    same argument so they can never drift."""
    assert "allow_creating_accounts = true" in _hcl(ephemeral=True)
    assert "create_actor_path" in _hcl(ephemeral=True)
    assert "allow_creating_accounts = false" in _hcl(ephemeral=False)
    assert "create_actor_path" not in _hcl(ephemeral=False)


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
    for ephemeral in (True, False):
        hcl = _hcl(ephemeral=ephemeral)
        for field, _path in ers._rest_routes(ephemeral):
            assert f"{field} = " in hcl, field
        assert "headers = {" in hcl
        assert "schema = " in hcl and "host   = " in hcl


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
