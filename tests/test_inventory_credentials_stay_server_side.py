"""Hypervisor credentials never leave the server in an inventory, and a run sees only its own.

``build_inventory`` puts each on-prem hypervisor's DECRYPTED credential in its hostvars
(``ansible_password`` for Proxmox root@pam, ESXi root, Hyper-V, Nutanix, XCP-ng), because a
playbook run needs it. Two things leaked it:

  * ``GET /api/config-mgmt/inventory`` returned the whole thing to anyone with
    ``config_mgmt:read``, although its one UI caller reads only ``targets``;
  * every run wrote the WHOLE inventory into the directory mounted into the playbook's
    container, so a playbook aimed at one host could read every hypervisor's password with
    ``lookup('file', '/ansible/inventory.json')``.

Runs standalone:
    python tests/test_inventory_credentials_stay_server_side.py
"""
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="inventory-redaction-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "inventory-redaction-test-secret")

from web_dashboard.services import ansible_local_service as A  # noqa: E402


def _inventory():
    return {
        "_meta": {"hostvars": {
            "10.0.0.5": {"ansible_host": "10.0.0.5", "ansible_user": "root@pam",
                         "ansible_password": "proxmox-root-pw", "ansible_connection": "ssh"},
            "10.0.0.6": {"ansible_host": "10.0.0.6", "ansible_user": "root",
                         "ansible_password": "esxi-root-pw",
                         "ansible_become_pass": "esxi-become-pw"},
            "10.0.0.7": {"ansible_host": "10.0.0.7", "ansible_user": "admin",
                         "ansible_ssh_private_key_file": "/keys/nutanix",
                         "api_token": "nutanix-token", "ansible_password": ""},
        }},
        "on_premises": {"children": ["proxmox", "vsphere", "nutanix"]},
        "proxmox": {"hosts": ["10.0.0.5"]},
        "vsphere": {"hosts": ["10.0.0.6"]},
        "nutanix": {"hosts": ["10.0.0.7"]},
    }


_SECRETS = ("proxmox-root-pw", "esxi-root-pw", "esxi-become-pw", "nutanix-token",
            "/keys/nutanix")


def test_redaction_hides_every_credential_and_keeps_the_shape():
    inv = _inventory()
    out = A.redact_inventory(inv)
    dumped = json.dumps(out)
    for secret in _SECRETS:
        assert secret not in dumped, f"{secret!r} survived redaction"
    hv = out["_meta"]["hostvars"]
    assert hv["10.0.0.5"]["ansible_password"] == "********"
    assert hv["10.0.0.6"]["ansible_become_pass"] == "********"
    assert hv["10.0.0.5"]["ansible_user"] == "root@pam", "non-secret hostvars must stay"
    assert hv["10.0.0.7"]["ansible_password"] == "", "an empty value is not a secret to mask"
    assert {k for k in out if k != "_meta"} == {k for k in inv if k != "_meta"}
    assert inv["_meta"]["hostvars"]["10.0.0.5"]["ansible_password"] == "proxmox-root-pw", (
        "redaction must copy, not mutate the inventory the run still needs")


def test_a_run_against_one_group_gets_only_that_groups_credentials():
    out = A.inventory_for_target(_inventory(), "proxmox")
    assert out == {"_meta": {"hostvars": {"10.0.0.5": _inventory()["_meta"]["hostvars"]["10.0.0.5"]}},
                   "proxmox": {"hosts": ["10.0.0.5"]}}
    dumped = json.dumps(out)
    assert "esxi-root-pw" not in dumped and "nutanix-token" not in dumped


def test_a_run_against_one_host_gets_only_that_host():
    out = A.inventory_for_target(_inventory(), "10.0.0.6")
    assert list(out["_meta"]["hostvars"]) == ["10.0.0.6"]
    assert "proxmox-root-pw" not in json.dumps(out)


def test_a_cloud_host_or_the_umbrella_group_gets_no_hypervisor_credentials():
    """A cloud VM is not in the inventory at all; `on_premises` would be everything."""
    for target in ("54.1.2.3", "on_premises", "_meta"):
        dumped = json.dumps(A.inventory_for_target(_inventory(), target))
        for secret in _SECRETS:
            assert secret not in dumped, f"{target!r} run inventory carries {secret!r}"


def test_the_run_writes_the_filtered_inventory_and_the_api_the_redacted_one():
    with open(os.path.join(_ROOT, "web_dashboard", "services", "ansible_local_service.py"),
              encoding="utf-8") as fh:
        run_src = fh.read().split("async def run_playbook(", 1)[1]
    assert "json.dump(inventory_for_target(inventory, target), f)" in run_src
    assert "json.dump(inventory, f)" not in run_src

    from web_dashboard.api import config_mgmt
    saved = (A.build_inventory, A.get_configured_targets)
    A.build_inventory = lambda db=None: _inventory()
    A.get_configured_targets = lambda db=None: [{"key": "proxmox"}]
    try:
        body = config_mgmt.get_inventory(db=None, current_user=None)
    finally:
        A.build_inventory, A.get_configured_targets = saved
    assert body["targets"] == [{"key": "proxmox"}]
    dumped = json.dumps(body)
    for secret in _SECRETS:
        assert secret not in dumped, f"GET /api/config-mgmt/inventory returns {secret!r}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
