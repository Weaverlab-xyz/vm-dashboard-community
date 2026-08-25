"""Unit tests for the Entitle agent-token teardown lifecycle (destroy_agent_token +
the k8s_service wiring that invokes it). config_service is stubbed; no terraform /
Entitle needed.

The premise: Entitle refuses to re-mint an existing agent-token name and the value can
never be read back, so a minted token that outlives the agent it bootstrapped wedges
every future install on an unrecoverable "already exists". The teardown paths (the
``remove`` action and the decommission of the hosting cluster) must therefore destroy
the auto-minted token — and a FAILED destroy must keep the stash, because the stored
terraform state is the only remaining handle on the tenant-side token.

Runs under pytest, or standalone: python tests/test_entitle_agent_token_lifecycle.py
"""
import ast
import asyncio
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_K8S = os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py")

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


def _minted_stash():
    """The four rows ensure_agent_token writes after a successful mint."""
    return {
        "entitle_agent_token_tf_state": _state(),
        "entitle/agent-token": TOKEN_SENTINEL,
        "entitle_agent_token_ref": "config://entitle/agent-token",
        "entitle_agent_token_name": "vm-dashboard-agent",
    }


def _reset(**overrides):
    STORE.clear()
    STORE["entitle_api_key"] = "k"
    STORE.update(overrides)


class _DestroyRecorder:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, tf_state_json):
        self.calls.append(tf_state_json)
        if self.fail:
            raise ers.EntitleRegistrationError("terraform destroy failed: 401 unauthorized")


def _run_destroy(recorder):
    orig, ers._destroy_sync = ers._destroy_sync, recorder
    try:
        return asyncio.run(ers.destroy_agent_token())
    finally:
        ers._destroy_sync = orig


# -- destroy_agent_token --------------------------------------------------------

def test_destroy_clears_the_whole_stash_and_returns_the_name():
    _reset(**_minted_stash())
    rec = _DestroyRecorder()
    assert _run_destroy(rec) == "vm-dashboard-agent"
    assert len(rec.calls) == 1
    assert STORE["entitle_agent_token_tf_state"] == ""
    assert STORE["entitle/agent-token"] == ""
    assert STORE["entitle_agent_token_name"] == ""
    assert STORE["entitle_agent_token_ref"] == ""


def test_destroy_preserves_an_external_ref():
    """An operator-pointed ref (aws_sm://…) stays theirs even after our mint is gone."""
    _reset(**_minted_stash())
    STORE["entitle_agent_token_ref"] = "aws_sm://entitle/agent-token"
    _run_destroy(_DestroyRecorder())
    assert STORE["entitle_agent_token_ref"] == "aws_sm://entitle/agent-token"
    assert STORE["entitle_agent_token_tf_state"] == ""


def test_no_stored_state_means_nothing_of_ours_to_destroy():
    """An operator-supplied token (ref set, no mint state) must never be touched."""
    _reset(entitle_agent_token_ref="config://entitle/agent-token")
    STORE["entitle/agent-token"] = TOKEN_SENTINEL
    rec = _DestroyRecorder()
    assert _run_destroy(rec) == ""
    assert rec.calls == []
    assert STORE["entitle/agent-token"] == TOKEN_SENTINEL
    assert STORE["entitle_agent_token_ref"] == "config://entitle/agent-token"


def test_failed_destroy_keeps_the_stash():
    """The state is the only handle on the tenant-side token — a failed destroy must
    not clear it, or the token is orphaned exactly like the bug this fixes."""
    _reset(**_minted_stash())
    try:
        _run_destroy(_DestroyRecorder(fail=True))
    except ers.EntitleRegistrationError:
        pass
    else:
        raise AssertionError("expected EntitleRegistrationError")
    for key, val in _minted_stash().items():
        assert STORE[key] == val, key


def test_destroy_without_a_recorded_name_still_reports_success():
    _reset(entitle_agent_token_tf_state=_state(with_resource=False))
    assert _run_destroy(_DestroyRecorder()) == "unknown"
    assert STORE["entitle_agent_token_tf_state"] == ""


def test_remove_then_reinstall_mints_a_fresh_token_under_the_same_default_name():
    """The full cycle: destroy frees the name, so the next ensure() mints instead of
    recovering — and reuses the default name so private-target agent_token references
    stay valid."""
    _reset(**_minted_stash())
    _run_destroy(_DestroyRecorder())
    calls = []

    async def _fake_mint(name):
        calls.append(name)
        return {"token": "FRESH-TOKEN-SENTINEL", "tf_state_json": _state(name=name)}

    orig, ers.mint_agent_token = ers.mint_agent_token, _fake_mint
    try:
        assert asyncio.run(ers.ensure_agent_token()) == "FRESH-TOKEN-SENTINEL"
    finally:
        ers.mint_agent_token = orig
    assert calls == ["vm-dashboard-agent"]


# -- k8s_service wiring (source text only — the module is too heavy to import) ---

def _src():
    with open(_K8S, encoding="utf-8") as fh:
        return fh.read()


def _fn_code(name):
    src = _src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name}() not found in k8s_service.py")


def test_remove_action_destroys_the_token_only_for_the_hosting_cluster():
    code = _fn_code("setup_entitle_agent")
    assert "destroy_agent_token" in code, \
        "the remove action no longer destroys the minted agent token — the token " \
        "outlives the agent and wedges the next install on 'already exists'"
    guard = 'config_service.get("entitle_agent_cluster_id") == cluster_id'
    assert guard in code, \
        "the destroy must be guarded by the host check, or removing a NON-host " \
        "cluster kills the live agent's token"
    assert code.index(guard) < code.index("destroy_agent_token"), \
        "the host check must gate the destroy"


def test_remove_action_clears_the_host_marker_only_after_the_destroy():
    """Clear-before-destroy would make a retry skip the destroy (the guard no longer
    matches), permanently orphaning the token after one transient failure."""
    code = _fn_code("setup_entitle_agent")
    clear = 'config_service.set("entitle_agent_cluster_id", "")'
    assert clear in code
    assert code.index("destroy_agent_token") < code.index(clear)


def test_decommission_tears_down_entitle_before_the_cluster():
    code = _fn_code("run_decommission")
    assert 'action="deregister"' in code and "register_cluster_in_entitle" in code, \
        "decommission must deregister the cluster's Entitle k8s integration"
    assert "destroy_agent_token" in code, \
        "decommissioning the agent-hosting cluster must destroy the minted token"
    assert code.index("destroy_agent_token") < code.index("terraform.destroy"), \
        "Entitle teardown must run before the cluster is destroyed"


def test_decommission_records_a_failed_token_destroy_and_keeps_the_marker():
    code = _fn_code("run_decommission")
    assert 'errors.append(f"Entitle agent token destroy' in code, \
        "a failed token destroy must fail the decommission loudly, not vanish"
    clear = 'config_service.set("entitle_agent_cluster_id", "")'
    assert code.index("destroy_agent_token") < code.index(clear), \
        "the host marker must only clear after a successful destroy so a retry converges"


def test_integration_deregister_no_longer_swallows_a_failed_destroy():
    code = _fn_code("register_cluster_in_entitle")
    assert "non-fatal" not in code, \
        "a failed integration destroy was silently swallowed AND the state handle " \
        "cleared — orphaning the tenant-side object with no way to remove it"
    assert code.index("ent.deregister(state)") < code.index('entitle_k8s_tfstate_{cluster_id}", ""'), \
        "the state handle must only clear after the destroy succeeds"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("all passed")
