"""Unit tests for services/config_drift.py (config-drift signals).

`content_hash` / `inputs_hash` / `evaluate` are pure — loaded by file path so the
test needs only stdlib.
Runs under pytest, or standalone:  python tests/test_config_drift.py
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "config_drift.py")
_spec = importlib.util.spec_from_file_location("config_drift", _PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)

NOW = datetime(2026, 7, 1, 12, 0, 0)


# ── hashing ──────────────────────────────────────────────────────────────────

def test_content_hash_stable_and_sensitive():
    a = cd.content_hash(b"- hosts: all\n  tasks: []\n")
    assert a == cd.content_hash(b"- hosts: all\n  tasks: []\n")   # stable
    assert a != cd.content_hash(b"- hosts: all\n  tasks: [x]\n")  # edit changes it
    assert len(a) == 64


def test_inputs_hash_order_independent_and_empty():
    assert cd.inputs_hash(None) == "" and cd.inputs_hash({}) == ""
    assert cd.inputs_hash({"a": 1, "b": 2}) == cd.inputs_hash({"b": 2, "a": 1})
    assert cd.inputs_hash({"a": 1}) != cd.inputs_hash({"a": 2})


# ── evaluate ─────────────────────────────────────────────────────────────────

def _rows():
    return [
        {"target": "proxmox", "playbook_ref": "base.yml", "content_hash": "h1",
         "applied_at": NOW - timedelta(days=30), "job_id": "j1"},   # unverified (30 > 14)
        {"target": "vsphere", "playbook_ref": "patch.yml", "content_hash": "h2",
         "applied_at": NOW - timedelta(days=2), "job_id": "j2"},    # fresh
    ]


def test_evaluate_flags_unverified():
    rep = cd.evaluate(_rows(), {}, 14, now=NOW)
    assert rep["unverified_count"] == 1 and rep["changed_count"] == 0
    assert rep["items"][0]["target"] == "proxmox"        # drift sorts first
    assert rep["items"][0]["unverified"] is True and rep["items"][0]["age_days"] == 30


def test_evaluate_flags_changed_playbook():
    # vsphere's stored patch.yml now hashes differently than what was applied
    rep = cd.evaluate(_rows(), {"base.yml": "h1", "patch.yml": "h2-NEW"}, 14, now=NOW)
    changed = [i for i in rep["items"] if i["changed"]]
    assert rep["changed_count"] == 1 and changed[0]["target"] == "vsphere"
    # base.yml unchanged (same hash) → not flagged changed
    assert not any(i["changed"] for i in rep["items"] if i["playbook_ref"] == "base.yml")


def test_evaluate_disabled_stale_window():
    rep = cd.evaluate(_rows(), {}, 0, now=NOW)
    assert rep["unverified_count"] == 0   # 0 disables the staleness window


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
