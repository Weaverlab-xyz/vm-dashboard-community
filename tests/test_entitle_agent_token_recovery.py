"""Unit tests for Entitle agent-token recovery (_agent_token_from_state /
ensure_agent_token / mint_agent_token conflict message). config_service is stubbed;
no terraform / Entitle needed.

The premise: Entitle returns an agent token's value only at creation and offers no
data source, so a second mint under the same name is a hard 400. The only idempotent
path is recovering the value from the previous mint's terraform state, which is
stashed UNSCRUBBED for exactly this reason.

Runs under pytest, or standalone: python tests/test_entitle_agent_token_recovery.py
"""
import asyncio
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STORE = {}


def _install_stubs():
    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: STORE.get(key, default)
    cfg.get_bool = lambda key, default=False: bool(STORE.get(key, default))
    cfg.set = lambda key, value, **kw: STORE.__setitem__(key, value)
    cfg.is_reference = lambda ref: str(ref).startswith(("aws_sm://", "azure_kv://", "gcp_sm://"))
    cfg.resolve_reference = lambda ref: STORE.get("__external__", "")
    sys.modules["web_dashboard.services.config_service"] = cfg
    confmod = types.ModuleType("web_dashboard.config")

    class _Settings:
        def __getattr__(self, _k):
            return ""

    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod


_install_stubs()
try:
    from web_dashboard.services import entitle_registration_service as ers
except Exception as exc:  # pragma: no cover - skip if deps missing
    try:
        import pytest
        pytest.skip(f"entitle_registration_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


TOKEN_SENTINEL = "AGENT-TOKEN-SENTINEL"   # non-credential literal: keeps secret scanners quiet


def _state(with_output=True, with_resource=True, name="vm-dashboard-agent"):
    """A terraform.tfstate as the mint stashes it (sensitive values in plaintext)."""
    state = {"version": 4, "terraform_version": "1.9.5", "outputs": {}, "resources": []}
    if with_output:
        state["outputs"]["token"] = {"value": TOKEN_SENTINEL, "type": "string", "sensitive": True}
    if with_resource:
        state["resources"].append({
            "mode": "managed", "type": "entitle_agent_token", "name": "vm_dashboard_agent",
            "instances": [{"schema_version": 0, "attributes": {
                "id": "tok-1", "name": name, "token": TOKEN_SENTINEL}}],
        })
    return json.dumps(state)


def _reset(**overrides):
    STORE.clear()
    STORE["entitle_api_key"] = "k"
    STORE.update(overrides)


# -- _agent_token_from_state ---------------------------------------------------

def test_from_state_reads_the_output():
    token, name = ers._agent_token_from_state(_state())
    assert token == TOKEN_SENTINEL
    assert name == "vm-dashboard-agent"   # the name lives only on the resource


def test_from_state_falls_back_to_resource_attributes():
    """State captured without the output block still carries the token attribute."""
    token, name = ers._agent_token_from_state(_state(with_output=False))
    assert token == TOKEN_SENTINEL
    assert name == "vm-dashboard-agent"


def test_from_state_survives_garbage():
    for bad in ("", None, "not json", "[]", '{"outputs": {}}', '{"outputs": {"token": "bare"}}'):
        assert ers._agent_token_from_state(bad) == ("", ""), bad


# -- ensure_agent_token -------------------------------------------------------

def _no_mint(*_a, **_kw):
    raise AssertionError("mint_agent_token must not be called when the token is recoverable")


def test_resolved_ref_short_circuits_before_any_state_read():
    _reset(entitle_agent_token_ref="config://entitle/agent-token",
           entitle_agent_token_tf_state="not json")
    STORE["entitle/agent-token"] = TOKEN_SENTINEL
    orig, ers.mint_agent_token = ers.mint_agent_token, _no_mint
    try:
        assert asyncio.run(ers.ensure_agent_token()) == TOKEN_SENTINEL
    finally:
        ers.mint_agent_token = orig


def test_recovers_from_state_and_restores_ref_and_name():
    """The value is gone but the state survived - recover instead of minting a 400."""
    _reset(entitle_agent_token_ref="", entitle_agent_token_tf_state=_state())
    orig, ers.mint_agent_token = ers.mint_agent_token, _no_mint
    try:
        assert asyncio.run(ers.ensure_agent_token()) == TOKEN_SENTINEL
    finally:
        ers.mint_agent_token = orig
    assert STORE["entitle/agent-token"] == TOKEN_SENTINEL
    assert STORE["entitle_agent_token_ref"] == "config://entitle/agent-token"
    assert STORE["entitle_agent_token_name"] == "vm-dashboard-agent"


def test_recovery_covers_an_emptied_external_ref():
    """An aws_sm:// ref whose secret was deleted resolves empty - still recoverable."""
    _reset(entitle_agent_token_ref="aws_sm://entitle/agent-token",
           entitle_agent_token_tf_state=_state())
    STORE["__external__"] = ""
    orig, ers.mint_agent_token = ers.mint_agent_token, _no_mint
    try:
        assert asyncio.run(ers.ensure_agent_token()) == TOKEN_SENTINEL
    finally:
        ers.mint_agent_token = orig


def test_recovery_proceeds_when_the_stored_name_matches():
    _reset(entitle_agent_token_tf_state=_state(name="operator-set"),
           entitle_agent_token_name="operator-set")
    orig, ers.mint_agent_token = ers.mint_agent_token, _no_mint
    try:
        assert asyncio.run(ers.ensure_agent_token()) == TOKEN_SENTINEL
    finally:
        ers.mint_agent_token = orig
    assert STORE["entitle_agent_token_name"] == "operator-set"


def test_a_different_requested_name_skips_recovery_and_mints():
    """Renaming is the documented way out of an unrecoverable conflict, so stale state
    must not silently hand back the old token instead."""
    _reset(entitle_agent_token_tf_state=_state(name="vm-dashboard-agent"),
           entitle_agent_token_name="second-agent")
    calls = []

    async def _fake_mint(name):
        calls.append(name)
        return {"token": "FRESH-TOKEN-SENTINEL", "tf_state_json": _state(name=name)}

    orig, ers.mint_agent_token = ers.mint_agent_token, _fake_mint
    try:
        assert asyncio.run(ers.ensure_agent_token()) == "FRESH-TOKEN-SENTINEL"
    finally:
        ers.mint_agent_token = orig
    assert calls == ["second-agent"]
    assert STORE["entitle/agent-token"] == "FRESH-TOKEN-SENTINEL"


def test_an_explicit_name_argument_also_skips_stale_recovery():
    _reset(entitle_agent_token_tf_state=_state(name="vm-dashboard-agent"))
    calls = []

    async def _fake_mint(name):
        calls.append(name)
        return {"token": "FRESH-TOKEN-SENTINEL", "tf_state_json": _state(name=name)}

    orig, ers.mint_agent_token = ers.mint_agent_token, _fake_mint
    try:
        asyncio.run(ers.ensure_agent_token(name="explicit"))
    finally:
        ers.mint_agent_token = orig
    assert calls == ["explicit"]


def test_unrecoverable_state_still_mints():
    """No state to recover from - fall through to the mint (the migrated-config case)."""
    _reset(entitle_agent_token_tf_state="")
    calls = []

    async def _fake_mint(name):
        calls.append(name)
        return {"token": TOKEN_SENTINEL, "tf_state_json": _state()}

    orig, ers.mint_agent_token = ers.mint_agent_token, _fake_mint
    try:
        assert asyncio.run(ers.ensure_agent_token()) == TOKEN_SENTINEL
    finally:
        ers.mint_agent_token = orig
    assert calls == ["vm-dashboard-agent"]


# -- mint_agent_token conflict message ----------------------------------------

def test_conflict_message_carries_the_remedies():
    """A 400 'Resource already exists' must name the fixes: the job page renders
    error_message and nothing else."""
    _reset()

    def _conflict(hcl, tf_vars):
        raise ers.EntitleRegistrationError(
            "terraform apply failed: Error: API Response Error\n\nFailed to create the Agent "
            "Token, status code: 400, getting error from entitle API error message: "
            "Resource already exists")

    orig, ers._apply_hcl_sync = ers._apply_hcl_sync, _conflict
    try:
        asyncio.run(ers.mint_agent_token("vm-dashboard-agent"))
    except ers.EntitleRegistrationError as e:
        msg = str(e)
        assert "vm-dashboard-agent" in msg
        assert "ENTITLE_AGENT_TOKEN_NAME" in msg
        assert "ENTITLE_AGENT_TOKEN_REF" in msg
        assert "delete that token in Entitle" in msg
    else:
        raise AssertionError("expected EntitleRegistrationError")
    finally:
        ers._apply_hcl_sync = orig


def test_unrelated_apply_failure_is_not_rewritten():
    _reset()

    def _boom(hcl, tf_vars):
        raise ers.EntitleRegistrationError("terraform apply failed: connection refused")

    orig, ers._apply_hcl_sync = ers._apply_hcl_sync, _boom
    try:
        asyncio.run(ers.mint_agent_token("x"))
    except ers.EntitleRegistrationError as e:
        assert "connection refused" in str(e)
        assert "ENTITLE_AGENT_TOKEN_NAME" not in str(e)
    else:
        raise AssertionError("expected EntitleRegistrationError")
    finally:
        ers._apply_hcl_sync = orig


def test_missing_provider_key_is_reported_before_any_apply():
    STORE.clear()
    try:
        asyncio.run(ers.mint_agent_token("x"))
    except ers.EntitleRegistrationError as e:
        assert "entitle_api_key" in str(e)
    else:
        raise AssertionError("expected EntitleRegistrationError")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("all passed")
