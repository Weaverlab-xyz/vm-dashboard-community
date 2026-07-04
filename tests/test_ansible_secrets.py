"""Unit tests for services/ansible_secrets.resolve_secret_vars.

Pure + dependency-injected — loaded by file path (stdlib only).
Runs under pytest, or standalone:  python tests/test_ansible_secrets.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "ansible_secrets.py")
_spec = importlib.util.spec_from_file_location("ansible_secrets", _PATH)
asec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asec)


# Fakes standing in for config_service.
_DB = {"vsphere_password": "vs-pw", "aws_secret_access_key": ""}   # one set, one empty
_VAULT = {"bt_safe://Dashboard/db_pw": "vault-db-pw"}


def _get(key):
    return _DB.get(key, "")


def _is_ref(s):
    return s.startswith(("aws_sm://", "azure_kv://", "gcp_sm://", "bt_safe://"))


def _resolve_ref(raw):
    return _VAULT.get(raw, "")


def _run(secret_vars):
    return asec.resolve_secret_vars(secret_vars, get=_get,
                                    resolve_reference=_resolve_ref, is_reference=_is_ref)


def test_registry_key_resolves_via_get():
    assert _run({"vs_pw": "vsphere_password"}) == {"vs_pw": "vs-pw"}


def test_raw_ref_resolves_via_resolve_reference():
    assert _run({"db_pw": "bt_safe://Dashboard/db_pw"}) == {"db_pw": "vault-db-pw"}


def test_mixed_sources():
    out = _run({"vs_pw": "vsphere_password", "db_pw": "bt_safe://Dashboard/db_pw"})
    assert out == {"vs_pw": "vs-pw", "db_pw": "vault-db-pw"}


def test_blank_var_or_source_dropped():
    assert _run({"": "vsphere_password", "x": "", "  ": "  "}) == {}


def test_empty_resolved_value_dropped():
    # key exists but resolves to "" (e.g. not configured) → not injected
    assert _run({"aws": "aws_secret_access_key", "missing": "bt_safe://nope"}) == {}


def test_none_input():
    assert _run(None) == {}


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
