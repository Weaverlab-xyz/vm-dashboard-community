"""Unit tests for the managed-database PRA tunnel Vault-account HCL.

Regression guard for the bug where the DB-tunnel Vault account was associated via
``jump_items[].type`` (e.g. "postgresql_tunnel_jump"). The sra provider accepts
that string client-side, but the PRA backend 422s it
("jump_items.0.type: The selected value is invalid") for the DB protocol-tunnel
jump-item types — so every managed-DB provision silently fell back to a tunnel
with NO vaulted credential. The fix associates the account to the tunnel's Jump
GROUP via ``criteria.shared_jump_groups`` instead (the same wall the k8s tunnel
path hit — see test_pra_k8s_vault.py). The VDI Remote-RDP path intentionally keeps
jump_items[].type because the backend DOES accept "remote_rdp".

Imports terraform_pra_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_pra_db_tunnel_vault.py
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


def _hcl(engine="postgres", **kw):
    return pra._generate_db_tunnel_hcl(
        engine=engine, name="clouddb-abc123", hostname="10.0.0.5",
        jump_group_name="centralus", jumpoint_name="GCP Run",
        username="dbadmin", database="app_db", tag="clouddb", **kw)


def test_db_vault_associates_via_shared_jump_groups_not_jump_items_type():
    hcl = _hcl(vault_account_name="clouddb-abc123-admin", vault_username="dbadmin",
               vault_account_group_id=6)
    assert 'resource "sra_vault_username_password_account" "db_admin"' in hcl
    # The fix: associate to the tunnel's Jump Group, jump_items empty.
    assert "shared_jump_groups = [tonumber(data.sra_jump_group_list.jg.items[0].id)]" in hcl
    assert "jump_items = []" in hcl
    assert 'output "vault_account_id"' in hcl
    # The account group makes it visible to users via a group policy.
    assert "account_group_id = 6" in hcl
    # Regression: NO per-item type association for any DB tunnel jump-item type
    # (these are what the PRA backend 422s). The tunnel *resource* keeps its
    # sra_-prefixed name; only the bare jump-item type strings must be absent.
    for bad in ("postgresql_tunnel_jump", "my_sql_tunnel_jump", "protocol_tunnel_jump"):
        assert f'type = "{bad}"' not in hcl


def test_db_vault_group_line_optional():
    hcl = _hcl(vault_account_name="clouddb-abc123-admin", vault_username="dbadmin")
    assert 'resource "sra_vault_username_password_account" "db_admin"' in hcl
    assert "shared_jump_groups = [tonumber(data.sra_jump_group_list.jg.items[0].id)]" in hcl
    assert "account_group_id" not in hcl  # omitted → provider default (Default group)


def test_mysql_tunnel_uses_same_shared_group_association():
    hcl = _hcl(engine="mysql", vault_account_name="clouddb-abc123-admin",
               vault_username="dbadmin", vault_account_group_id=3)
    assert "shared_jump_groups = [tonumber(data.sra_jump_group_list.jg.items[0].id)]" in hcl
    assert 'type = "my_sql_tunnel_jump"' not in hcl


def test_no_vault_block_when_name_empty():
    # With vault_account_name="" the output carries no Vault resource at all
    # (the state-driven destroy relies on this staying tunnel-only).
    hcl = _hcl(vault_account_name="")
    assert "sra_vault_username_password_account" not in hcl
    assert "vault_account_id" not in hcl


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
