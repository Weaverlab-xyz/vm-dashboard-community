"""Native Password Safe database onboarding — asset -> database -> managed system.

Oracle onboards on Password Safe's OWN Oracle platform: the Resource Broker serving the
asset's workgroup connects to the listener itself, so there is no plugin, no packed
address, and the instance (Oracle's service name) lives on a Database object. That is
why this path is not passwordsafe_managed_system_by_workgroup, which has no instance
field. These pin the HCL shape, that the password never lands in it, the rollback on a
partial apply, and the IP resolution a native asset requires.

Runs under pytest or standalone:  python tests/test_ps_native_db.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import ps_resource_service as ps  # noqa: E402


def _hcl(**kw):
    args = dict(name="clouddb-abc-db", workgroup_id="7", host_name="db.abc.rds.amazonaws.com",
                ip_address="10.99.4.12", port=1521, platform_id=9, instance_name="ORAABCDE",
                functional_account_id=42, managed_account_name="psafe_abc123")
    args.update(kw)
    return ps._generate_native_db_hcl(**args)


def test_the_chain_is_asset_database_managed_system_account():
    hcl = _hcl()
    for res in ('resource "passwordsafe_asset_by_workgroup_id"',
                'resource "passwordsafe_database"',
                'resource "passwordsafe_managed_system_by_database"',
                'resource "passwordsafe_managed_account"'):
        assert res in hcl, res
    # The by-workgroup managed system has no instance field — it must not be used.
    assert "passwordsafe_managed_system_by_workgroup" not in hcl


def test_the_database_carries_platform_instance_and_port():
    hcl = _hcl()
    assert "platform_id   = 9" in hcl
    assert 'instance_name = "ORAABCDE"' in hcl
    assert "port          = 1521" in hcl
    assert 'ip_address    = "10.99.4.12"' in hcl
    assert 'dns_name      = "db.abc.rds.amazonaws.com"' in hcl
    assert 'work_group_id = "7"' in hcl


def test_the_account_attaches_by_the_systems_own_name_and_rotates_itself():
    import re
    hcl = _hcl()
    assert re.search(r"system_name\s*=\s*passwordsafe_managed_system_by_database\."
                     r"clouddb_abc_db\.managed_system_name", hcl), hcl
    assert re.search(r"use_own_credentials\s*=\s*true", hcl)
    assert re.search(r"functional_account_id\s*=\s*42", hcl)
    assert "use_own_credentials" not in _hcl(use_own_credentials=False)


def test_the_password_rides_a_sensitive_variable_never_the_hcl():
    hcl = _hcl()
    assert 'variable "ps_account_password"    { sensitive = true }' in hcl
    assert "var.ps_account_password" in hcl


def test_both_ids_are_output_for_teardown_and_the_row():
    hcl = _hcl()
    assert 'output "managed_system_id"' in hcl
    assert 'output "managed_account_id"' in hcl


def test_register_refuses_a_missing_ip_or_instance():
    base = dict(name="n", workgroup_id="7", host_name="h", port=1521, platform_id=9,
                functional_account_id=1, managed_account_name="m", managed_password="p")
    for missing in ({"ip_address": "", "instance_name": "S"},
                    {"ip_address": "10.0.0.1", "instance_name": ""}):
        try:
            asyncio.run(ps.register_native_database(**base, **missing))
        except ps.PSResourceError:
            continue
        raise AssertionError(f"registered with {missing}")


def test_a_failed_apply_is_rolled_back():
    """Four resources are created in order, and a failed apply returns no state — so a
    failure on the third would leave an asset and a database nothing tears down."""
    calls = []

    class _R:
        def __init__(self, rc, err=""):
            self.returncode, self.stdout, self.stderr = rc, "", err

    def _fake_run(args, work_dir, env, timeout=180):
        calls.append(args[0])
        return _R(1, "managed system create failed") if args[0] == "apply" else _R(0)

    real_run, real_env = ps._run_tf, ps._tf_env
    ps._run_tf, ps._tf_env = _fake_run, (lambda extra=None, tenant=None: {})
    try:
        try:
            ps._apply_native_db_sync(_hcl(), {"ps_account_password": "x"})
        except ps.PSResourceError as exc:
            assert "managed system create failed" in str(exc)
        else:
            raise AssertionError("a failed apply did not raise")
    finally:
        ps._run_tf, ps._tf_env = real_run, real_env
    assert calls == ["init", "apply", "destroy"], calls


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
