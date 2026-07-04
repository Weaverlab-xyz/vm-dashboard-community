"""Unit tests for services/managed_accounts.py (pure shaping + guard logic).

Loaded by file path (stdlib only) — no config / FastAPI / ps-cli needed.
Runs under pytest, or standalone:  python tests/test_managed_accounts.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "managed_accounts.py")
_spec = importlib.util.spec_from_file_location("managed_accounts", _PATH)
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)


# ── host_is_ip ──────────────────────────────────────────────────────────────────

def test_host_is_ip():
    assert ma.host_is_ip("10.0.0.5")
    assert ma.host_is_ip(" 192.168.1.1 ")   # trimmed
    assert not ma.host_is_ip("host.example.com")
    assert not ma.host_is_ip("")
    assert not ma.host_is_ip("dc01")


# ── normalize_managed_systems ───────────────────────────────────────────────────

def test_normalize_locally_managed_account_ssh_and_password():
    systems = [{"ManagedSystemID": 5, "Name": "web01", "IPAddress": "10.0.0.5"}]
    accounts = {5: [
        {"ManagedAccountID": 45, "AccountName": "root", "DSSAutoManagementFlag": True},
        {"ManagedAccountID": 46, "AccountName": "deploy", "DSSAutoManagementFlag": False},
    ]}
    out = ma.normalize_managed_systems(systems, accounts)
    assert out == [{
        "system_id": 5, "name": "web01", "ip": "10.0.0.5",
        "accounts": [
            {"account_id": 45, "name": "root", "domain": "", "uses_ssh_key": True},
            {"account_id": 46, "name": "deploy", "domain": "", "uses_ssh_key": False},
        ],
    }]


def test_normalize_domain_linked_field_variants():
    # list-accounts fallback shape: SystemId / AccountId / DomainName, no DSS flag.
    systems = [{"SystemId": 9, "SystemName": "DC01", "IPAddress": "10.0.0.9"}]
    accounts = {9: [
        {"AccountId": 100, "AccountName": "svc-ansible", "DomainName": "SHIELD"},
    ]}
    out = ma.normalize_managed_systems(systems, accounts)
    assert out[0]["system_id"] == 9 and out[0]["name"] == "DC01"
    acct = out[0]["accounts"][0]
    assert acct == {"account_id": 100, "name": "svc-ansible",
                    "domain": "SHIELD", "uses_ssh_key": False}


def test_normalize_skips_systems_and_accounts_without_ids():
    systems = [{"Name": "nope", "IPAddress": "1.2.3.4"},          # no system id → skipped
               {"ManagedSystemID": 3, "Name": "ok", "IPAddress": "1.2.3.5"}]
    accounts = {3: [{"AccountName": "no-id"},                     # no account id → skipped
                    {"ManagedAccountID": 7, "AccountName": "yes"}]}
    out = ma.normalize_managed_systems(systems, accounts)
    assert len(out) == 1 and out[0]["system_id"] == 3
    assert [a["account_id"] for a in out[0]["accounts"]] == [7]


def test_normalize_system_with_no_accounts():
    out = ma.normalize_managed_systems(
        [{"ManagedSystemID": 1, "Name": "x", "IPAddress": "1.1.1.1"}], {})
    assert out == [{"system_id": 1, "name": "x", "ip": "1.1.1.1", "accounts": []}]


def test_normalize_empty():
    assert ma.normalize_managed_systems([], {}) == []
    assert ma.normalize_managed_systems(None, {}) == []


# ── local_only_violation ────────────────────────────────────────────────────────

def test_local_only_violation_true_for_cloud_runner_adhoc_playbook():
    assert ma.local_only_violation(True, "ecs", True, True)
    assert ma.local_only_violation(True, "aci", True, True)
    assert ma.local_only_violation(True, "gcp", True, True)


def test_local_only_violation_false_when_no_managed():
    assert not ma.local_only_violation(False, "ecs", True, True)


def test_local_only_violation_false_for_local_runner():
    assert not ma.local_only_violation(True, "local", True, True)


def test_local_only_violation_false_when_not_adhoc_or_not_playbook():
    # group target (not adhoc) or non-playbook → falls back to local anyway
    assert not ma.local_only_violation(True, "ecs", False, True)
    assert not ma.local_only_violation(True, "ecs", True, False)


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
