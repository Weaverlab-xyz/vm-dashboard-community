"""Unit tests for services/ansible_run_gate.py (pure refusal + runner-selection logic).

Loaded by file path (stdlib only) — no config / FastAPI / ps-cli needed.
Runs under pytest, or standalone:  python tests/test_ansible_run_gate.py

This file is also the FIRST coverage the endpoint-level managed-account gate has ever
had: `managed_accounts.requires_ephemeral_store` had five unit tests, but the branch that
consumes its answer — and `ansible_cloud_ephemeral_secrets_enabled` with it — appeared
nowhere under tests/ before the logic moved here.
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "ansible_run_gate.py")
_spec = importlib.util.spec_from_file_location("ansible_run_gate", _PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _cfg_from(mapping):
    """The injected config reader, as a dict lookup returning '' for anything absent —
    the same contract `ansible_local_service._cfg` has."""
    return lambda key: mapping.get(key, "")


# ── effective_runner ────────────────────────────────────────────────────────────

def test_effective_runner_defaults_to_local():
    assert gate.effective_runner("", cfg=_cfg_from({})) == "local"
    assert gate.effective_runner("azure", cfg=_cfg_from({})) == "local"


def test_effective_runner_uses_the_global_when_there_is_no_override():
    cfg = _cfg_from({"ansible_runner": "ecs"})
    assert gate.effective_runner("aws", cfg=cfg) == "ecs"
    assert gate.effective_runner("", cfg=cfg) == "ecs"


def test_a_per_cloud_override_beats_the_global():
    cfg = _cfg_from({"ansible_runner": "ecs", "ansible_runner_azure": "aci"})
    assert gate.effective_runner("azure", cfg=cfg) == "aci"
    # ...and only for its own cloud.
    assert gate.effective_runner("aws", cfg=cfg) == "ecs"


def test_a_blank_override_falls_back_rather_than_blanking_the_runner():
    # A blank per-cloud key is "inherit", never "no runner" — the settings panel writes
    # "" for an unset override, so treating it as a value would route to nothing.
    cfg = _cfg_from({"ansible_runner": "aci", "ansible_runner_azure": ""})
    assert gate.effective_runner("azure", cfg=cfg) == "aci"


def test_an_unknown_cloud_ignores_the_override_key():
    # Only the three clouds have per-cloud runners. A non-cloud target must not be able
    # to pick one up by naming itself after a config key.
    cfg = _cfg_from({"ansible_runner": "local", "ansible_runner_oci": "ecs"})
    assert gate.effective_runner("oci", cfg=cfg) == "local"


# ── check_credentials: the permission gate ──────────────────────────────────────

def test_a_plain_run_is_never_gated():
    # wants_secret False → no refusal, whatever else is off. A run with no credential
    # must not need the permission that using one needs.
    assert gate.check_credentials(
        wants_secret=False, can_use_secrets=False,
        has_managed=False, password_safe_enabled=False,
        needs_ephemeral_store=False, ephemeral_enabled=False,
        runner="gcp", gcp_runner_service_account="") is None


def test_no_permission_is_a_403():
    r = gate.check_credentials(
        wants_secret=True, can_use_secrets=False,
        has_managed=False, password_safe_enabled=True)
    assert r is not None and r.status == 403
    assert "secrets:use" in r.detail


def test_permission_granted_and_nothing_else_wanted_passes():
    assert gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=False, password_safe_enabled=False) is None


# ── check_credentials: Password Safe enablement ─────────────────────────────────

def test_a_managed_account_needs_password_safe_enabled():
    r = gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=False)
    assert r is not None and r.status == 400
    assert "Password Safe" in r.detail


def test_password_safe_off_does_not_refuse_a_run_with_no_managed_account():
    # An SSH-key secret is not a Password Safe checkout. Gating it on a PAM feature flag
    # would refuse a run that never touches Password Safe.
    assert gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=False, password_safe_enabled=False) is None


def test_the_permission_refusal_wins_over_the_password_safe_one():
    """Order is load-bearing. A caller who may not use secrets AND has Password Safe
    disabled gets the 403: telling them to enable a feature they are not permitted to
    use would send them to a Settings page that cannot help them."""
    r = gate.check_credentials(
        wants_secret=True, can_use_secrets=False,
        has_managed=True, password_safe_enabled=False)
    assert r is not None and r.status == 403


# ── check_credentials: the ephemeral-store gate (previously untested) ───────────

def test_a_managed_account_on_a_store_runner_needs_the_ephemeral_opt_in():
    r = gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
        needs_ephemeral_store=True, ephemeral_enabled=False, runner="ecs")
    assert r is not None and r.status == 400
    assert "Ephemeral cloud secrets" in r.detail
    # The message must name the way out, not just the obstacle.
    assert "local or Azure (ACI) runner" in r.detail


def test_the_ephemeral_gate_is_skipped_when_the_caller_says_it_does_not_apply():
    # local / ACI inject inline, so requires_ephemeral_store() is False for them and the
    # opt-in is irrelevant. This is the Azure SPIRE lab's path.
    assert gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
        needs_ephemeral_store=False, ephemeral_enabled=False, runner="aci") is None


def test_ephemeral_enabled_lets_an_ecs_managed_run_through():
    assert gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
        needs_ephemeral_store=True, ephemeral_enabled=True, runner="ecs") is None


def test_gcp_ephemeral_also_needs_the_runner_service_account():
    r = gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
        needs_ephemeral_store=True, ephemeral_enabled=True,
        runner="gcp", gcp_runner_service_account="")
    assert r is not None and r.status == 400
    assert "gcp_ansible_runner_service_account" in r.detail


def test_gcp_with_a_runner_service_account_passes():
    assert gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
        needs_ephemeral_store=True, ephemeral_enabled=True,
        runner="gcp", gcp_runner_service_account="runner@proj.iam.gserviceaccount.com") is None


def test_the_missing_service_account_check_is_gcp_only():
    # ECS has no equivalent requirement; demanding one would refuse a working AWS run.
    assert gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
        needs_ephemeral_store=True, ephemeral_enabled=True,
        runner="ecs", gcp_runner_service_account="") is None


def test_the_ephemeral_opt_in_refusal_precedes_the_service_account_one():
    # Both wrong → name the opt-in first, because enabling the SA without the opt-in
    # still would not run.
    r = gate.check_credentials(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
        needs_ephemeral_store=True, ephemeral_enabled=False,
        runner="gcp", gcp_runner_service_account="")
    assert r is not None and "Ephemeral cloud secrets" in r.detail


# ── the messages themselves ─────────────────────────────────────────────────────

def test_every_refusal_names_something_the_operator_can_act_on():
    """A refusal an operator cannot act on is the failure mode these sentences exist to
    prevent. Each one must name a permission, a Settings item or a config key."""
    cases = [
        dict(wants_secret=True, can_use_secrets=False, has_managed=False,
             password_safe_enabled=True),
        dict(wants_secret=True, can_use_secrets=True, has_managed=True,
             password_safe_enabled=False),
        dict(wants_secret=True, can_use_secrets=True, has_managed=True,
             password_safe_enabled=True, needs_ephemeral_store=True,
             ephemeral_enabled=False, runner="ecs"),
        dict(wants_secret=True, can_use_secrets=True, has_managed=True,
             password_safe_enabled=True, needs_ephemeral_store=True,
             ephemeral_enabled=True, runner="gcp", gcp_runner_service_account=""),
    ]
    actionable = ("secrets:use", "Settings", "gcp_ansible_runner_service_account")
    for kwargs in cases:
        r = gate.check_credentials(**kwargs)
        assert r is not None, kwargs
        assert r.status in (400, 403)
        assert r.detail and r.detail.strip() == r.detail
        assert any(a in r.detail for a in actionable), r.detail


def test_the_halves_compose_into_check_credentials():
    """`check_credentials` must be exactly the two halves in order, so a caller that
    interleaves a third check (config_mgmt's store-residency step) and a caller that does
    not cannot diverge on either the reason or its status."""
    kwargs = dict(wants_secret=True, can_use_secrets=True, has_managed=True,
                  password_safe_enabled=True, needs_ephemeral_store=True,
                  ephemeral_enabled=False, runner="ecs",
                  gcp_runner_service_account="")
    whole = gate.check_credentials(**kwargs)
    halves = gate.check_permission(
        wants_secret=True, can_use_secrets=True,
        has_managed=True, password_safe_enabled=True,
    ) or gate.check_runner_capability(
        needs_ephemeral_store=True, ephemeral_enabled=False, runner="ecs",
        gcp_runner_service_account="")
    assert whole == halves

    # ...and the permission half still wins when both halves would refuse.
    both = gate.check_credentials(**{**kwargs, "can_use_secrets": False})
    assert both is not None and both.status == 403


def test_check_runner_capability_alone_ignores_permission_state():
    # It takes no user or feature argument at all — the split is by concern, so a
    # capability check cannot accidentally re-litigate permission.
    assert gate.check_runner_capability() is None
    assert gate.check_runner_capability(needs_ephemeral_store=False,
                                        ephemeral_enabled=False) is None


def test_a_refusal_is_immutable():
    # Frozen so a caller cannot rewrite a status or a message on its way to the client.
    r = gate.check_credentials(wants_secret=True, can_use_secrets=False,
                               has_managed=False, password_safe_enabled=True)
    try:
        r.status = 200
    except Exception:
        return
    raise AssertionError("Refusal should be frozen")


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
    sys.exit(1 if failures else 0)
