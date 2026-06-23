"""Unit tests for the PRA k8s-tunnel Vault token-account HCL + state scrubbing.

Covers B (PRA-only K8s access via a Vault-injected ServiceAccount bearer token).
The tunnel *jump* itself is created over REST (the sra provider blocks
tunnel_type=k8s — see docs/notes/sra-provider-k8s-tunnel-bug.md), so the Terraform
HCL here is the Vault token account ALONE, associated to the REST-created jump via
TF_VAR_k8s_jump_id:
- the HCL emits a `sra_vault_token_account` with token via TF_VAR (never in HCL),
  associated to the jump by id, and declares no `sra_protocol_tunnel_jump`;
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


def test_vault_hcl_emits_token_account_associated_to_rest_jump():
    hcl = pra._generate_k8s_vault_account_hcl("k8s-demo-sa")
    assert 'resource "sra_vault_token_account" "k8s_access"' in hcl
    # Token + jump id are supplied via TF vars, never written into the HCL.
    assert 'variable "k8s_sa_token"' in hcl and "sensitive = true" in hcl
    assert 'variable "k8s_jump_id"' in hcl
    assert "token       = var.k8s_sa_token" in hcl
    assert "tonumber(var.k8s_jump_id)" in hcl
    assert 'type = "protocol_tunnel_jump"' in hcl
    assert 'output "vault_account_id"' in hcl
    # The blocked tunnel resource is NOT in the Terraform (it's created via REST).
    assert "sra_protocol_tunnel_jump" not in hcl
    assert "tunnel_type" not in hcl


def test_vault_hcl_account_group_optional():
    assert "account_group_id" not in pra._generate_k8s_vault_account_hcl("x")
    assert "account_group_id = 7" in pra._generate_k8s_vault_account_hcl("x", vault_account_group_id=7)


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
