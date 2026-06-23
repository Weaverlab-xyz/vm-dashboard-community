"""Unit tests for the PRA k8s-tunnel Vault token-account HCL + state scrubbing.

Covers B (PRA-only K8s access via a Vault-injected ServiceAccount bearer token):
- the k8s tunnel HCL emits a `sra_vault_token_account` (token via TF_VAR, never in
  the HCL) associated to the jump only when a vault account name is given;
- without it the HCL has no vault resource (the byte-identical pre-vault path);
- `_scrub_tf_state` redacts the `token` attribute (not just `password`) so a SA
  token never lands in the stashed state.

Imports terraform_pra_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_pra_k8s_vault.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import terraform_pra_service as pra  # noqa: E402

_COMMON = dict(name="k8s-demo", hostname="api.example", api_url="https://api.example:443",
               ca_certificates="-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----",
               jump_group_name="JG", jumpoint_name="JP")


def test_no_vault_hcl_has_tunnel_only():
    hcl = pra._generate_k8s_tunnel_hcl(**_COMMON)
    assert 'tunnel_type     = "k8s"' in hcl
    assert "sra_vault_token_account" not in hcl
    assert "k8s_sa_token" not in hcl
    assert 'output "vault_account_id"' not in hcl


def test_vault_hcl_emits_token_account_associated_to_jump():
    hcl = pra._generate_k8s_tunnel_hcl(vault_account_name="k8s-demo-sa", **_COMMON)
    assert 'resource "sra_vault_token_account" "k8s_access"' in hcl
    # Token is supplied via the sensitive TF var, never written into the HCL.
    assert 'variable "k8s_sa_token" { sensitive = true }' in hcl
    assert "token       = var.k8s_sa_token" in hcl
    # Associated to the protocol-tunnel jump for credential injection.
    assert 'type = "protocol_tunnel_jump"' in hcl
    assert "id   = tonumber(sra_protocol_tunnel_jump.k8s_demo.id)" in hcl
    assert 'output "vault_account_id"' in hcl


def test_vault_hcl_account_group_optional():
    without = pra._generate_k8s_tunnel_hcl(vault_account_name="x", **_COMMON)
    assert "account_group_id" not in without
    withgrp = pra._generate_k8s_tunnel_hcl(vault_account_name="x", vault_account_group_id=7, **_COMMON)
    assert "account_group_id = 7" in withgrp


def test_scrub_redacts_token_and_password():
    state = (
        '{"resources":[{"type":"sra_vault_token_account","instances":'
        '[{"attributes":{"token":"super-secret-bearer","name":"k8s-demo-sa"}}]},'
        '{"type":"sra_vault_username_password_account","instances":'
        '[{"attributes":{"password":"pw"}}]}]}'
    )
    scrubbed = pra._scrub_tf_state(state)
    assert "super-secret-bearer" not in scrubbed
    assert "\"pw\"" not in scrubbed
    assert pra._REDACTED in scrubbed
    # Non-secret attributes survive.
    assert "k8s-demo-sa" in scrubbed


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
