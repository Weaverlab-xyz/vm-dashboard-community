"""Unit test for the PRA Web Jump HCL generator
(terraform_pra_service._generate_web_jump_hcl). Pure string generation — no
terraform/PRA. Skips if the module can't import (missing app deps).

Runs under pytest, or standalone: python tests/test_pra_web_jump.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub config_service + config so any _cfg fallback doesn't pull in pydantic.
_cfg_mod = types.ModuleType("web_dashboard.services.config_service")
_cfg_mod.get = lambda key, default="", workgroup=None: default
sys.modules["web_dashboard.services.config_service"] = _cfg_mod
_conf = types.ModuleType("web_dashboard.config")


class _Settings:
    def __getattr__(self, _k):
        return ""


_conf.settings = _Settings()
sys.modules["web_dashboard.config"] = _conf

try:
    from web_dashboard.services import terraform_pra_service as tps
except Exception as exc:  # pragma: no cover — skip if deps missing
    try:
        import pytest
        pytest.skip(f"terraform_pra_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

URL_SENTINEL = "RANCHER-URL-SENTINEL"   # non-URL literal: avoids CodeQL url-substring query


def test_web_jump_hcl():
    hcl = tps._generate_web_jump_hcl("rancher-ui", URL_SENTINEL, "jg-1", "jp-1",
                                     tag="rancher", verify_certificate=False)
    assert 'resource "sra_web_jump" "rancher_ui"' in hcl   # name sanitized
    assert URL_SENTINEL in hcl                              # url flows into the resource
    assert "jump_group_id      = tonumber(data.sra_jump_group_list.jg.items[0].id)" in hcl
    assert "jumpoint_id        = tonumber(data.sra_jumpoint_list.jp.items[0].id)" in hcl
    assert 'name = "jg-1"' in hcl                           # jump-group data source
    assert 'name = "jp-1"' in hcl                           # jumpoint data source
    assert "verify_certificate = false" in hcl             # self-signed Rancher CA
    assert 'output "web_jump_id"' in hcl
    # No vault account when none requested (default) — bare web jump only.
    assert "sra_vault_username_password_account" not in hcl
    assert "rancher_password" not in hcl


def test_web_jump_hcl_with_vault_account():
    hcl = tps._generate_web_jump_hcl(
        "rancher-ui", URL_SENTINEL, "jg-1", "jp-1", tag="rancher",
        vault_account_name="rancher-ui-admin", vault_username="admin",
        vault_account_group_id=7)
    # The web jump is still there …
    assert 'resource "sra_web_jump" "rancher_ui"' in hcl
    # … plus a Vault username/password account for injection.
    assert 'resource "sra_vault_username_password_account" "rancher_admin"' in hcl
    assert 'name        = "rancher-ui-admin"' in hcl
    assert 'username    = "admin"' in hcl
    assert "password    = var.rancher_password" in hcl
    assert 'variable "rancher_password"' in hcl            # sensitive TF_VAR (not in HCL)
    assert "account_group_id = 7" in hcl                   # scoped to the chosen group
    # Associated to the jump's Jump GROUP via shared_jump_groups (not jump_items[].type).
    assert "shared_jump_groups = [tonumber(data.sra_jump_group_list.jg.items[0].id)]" in hcl
    assert 'output "vault_account_id"' in hcl


def test_web_jump_hcl_vault_without_group_omits_account_group_id():
    hcl = tps._generate_web_jump_hcl(
        "rancher-ui", URL_SENTINEL, "jg-1", "jp-1",
        vault_account_name="rancher-ui-admin")
    assert 'resource "sra_vault_username_password_account" "rancher_admin"' in hcl
    assert "account_group_id" not in hcl                   # omitted → provider default group


if __name__ == "__main__":
    test_web_jump_hcl()
    test_web_jump_hcl_with_vault_account()
    test_web_jump_hcl_vault_without_group_omits_account_group_id()
    print("ok")
