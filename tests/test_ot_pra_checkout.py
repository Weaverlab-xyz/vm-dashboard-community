"""Unit tests for the OT cell's PRA-checkout Vault account HCL (terraform_pra_service).

The OT cell's Password Safe onboarding gains a PRA-checkout pair: a standalone
``sra_vault_username_password_account`` associated to the cell's Jump Group, mirrored
by a Password Safe managed account on the "PRA Vault Username Password" plugin and
kept current via SyncedAccounts (services/ot_service._wire_ps_checkout). The HCL
below is the one artifact with sharp edges worth pinning:

- the password rides ``TF_VAR_vault_password`` (sensitive) and is NEVER in the HCL —
  it is a throwaway placeholder Password Safe overwrites on the first synced rotation;
- the association is ``criteria.shared_jump_groups`` resolved from the Jump Group
  NAME via a data source — per-jump-item association is rejected by the PRA backend
  for tunnel jump types (the wall the DB and k8s vault accounts both hit), and every
  criteria array must be present (empty) or the API 4xxes.

Imports terraform_pra_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_ot_pra_checkout.py
"""
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import terraform_pra_service as pra  # noqa: E402


def test_vault_hcl_takes_the_password_as_a_tf_var_only():
    hcl = pra._generate_vault_up_account_hcl("ot-cell-01-adminuser", "adminuser", "us-east")
    assert 'resource "sra_vault_username_password_account" "checkout"' in hcl
    assert 'variable "vault_password"' in hcl and "sensitive = true" in hcl
    assert "password    = var.vault_password" in hcl
    assert '"ot-cell-01-adminuser"' in hcl
    assert 'username    = "adminuser"' in hcl
    assert 'output "vault_account_id"' in hcl


def test_vault_hcl_associates_via_the_jump_groups_criteria():
    hcl = pra._generate_vault_up_account_hcl("x", "adminuser", "us-east")
    # By NAME through a data source (the caller holds no id), into
    # criteria.shared_jump_groups — never jump_items[].type, which PRA 422s
    # for tunnel jump types.
    assert 'data "sra_jump_group_list" "jg"' in hcl
    assert 'name = "us-east"' in hcl
    assert "shared_jump_groups = [tonumber(data.sra_jump_group_list.jg.items[0].id)]" in hcl
    # Every criteria array present (empty) or the API 4xxes.
    for arr in ("host", "name", "tag", "comment"):
        assert re.search(rf"^\s*{arr}\s+= \[\]", hcl, re.MULTILINE), \
            f"criteria array {arr!r} missing"
    assert "jump_items = []" in hcl


def test_vault_hcl_account_group_optional():
    assert "account_group_id" not in pra._generate_vault_up_account_hcl("x", "u", "jg")
    assert "account_group_id = 9" in pra._generate_vault_up_account_hcl(
        "x", "u", "jg", vault_account_group_id=9)


def test_the_scrub_covers_this_resources_password():
    # remove_vault_account destroys from stashed state; the stash must be scrubbed.
    state = ('{"resources":[{"type":"sra_vault_username_password_account","instances":'
             '[{"attributes":{"password":"placeholder-pw","name":"ot-cell-01-adminuser"}}]}]}')
    scrubbed = pra._scrub_tf_state(state)
    assert "placeholder-pw" not in scrubbed
    assert "ot-cell-01-adminuser" in scrubbed


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
