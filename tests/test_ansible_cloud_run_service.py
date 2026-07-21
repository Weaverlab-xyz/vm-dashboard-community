"""Unit tests: ansible_cloud_run_service backend selection + helpers.

Pins the private-resource invariant — k8s/DB Ansible runs ALWAYS pick an in-cloud
transient runner from the resource's cloud, and 'local' is rejected — plus the
output scrubbing and kubeconfig-token extraction feeding it.

Heavy service deps are stubbed in sys.modules (mirrors the other config-mgmt unit
tests). Runs under pytest, or standalone:
    python tests/test_ansible_cloud_run_service.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _install_stubs():
    sa = types.ModuleType("sqlalchemy")
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = type("Session", (), {})
    sa.orm = sa_orm
    sys.modules["sqlalchemy"] = sa
    sys.modules["sqlalchemy.orm"] = sa_orm

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.resolve_reference = lambda ref: ref
    cfg.is_reference = lambda ref: False
    sys.modules["web_dashboard.services.config_service"] = cfg

    # The remaining siblings are only imported (not called) at module load.
    for name in ("cloud_database_service", "job_service", "k8s_runner_service",
                 "k8s_service", "storage_service"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import ansible_cloud_run_service as acr
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"ansible_cloud_run_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def test_resolve_runner_cloud_native_defaults():
    CONF.clear()
    assert acr.resolve_runner("aws") == "ecs"
    assert acr.resolve_runner("azure") == "aci"
    assert acr.resolve_runner("gcp") == "gcp"


def test_resolve_runner_per_cloud_override_honored():
    CONF.clear()
    CONF["ansible_runner_aws"] = "ecs"
    assert acr.resolve_runner("aws") == "ecs"


def test_resolve_runner_rejects_local():
    CONF.clear()
    CONF["ansible_runner_gcp"] = "local"
    try:
        acr.resolve_runner("gcp")
    except acr.AnsibleCloudRunError as e:
        assert "must run on an in-cloud runner" in str(e)
        return
    raise AssertionError("expected AnsibleCloudRunError for a 'local' override")


def test_resolve_runner_rejects_unsupported_cloud():
    CONF.clear()
    try:
        acr.resolve_runner("oci")
    except acr.AnsibleCloudRunError:
        return
    raise AssertionError("expected AnsibleCloudRunError for an unsupported cloud")


def test_scrub_redacts_long_values_only():
    out = acr._scrub("pw=supersecret host=db1 pin=42", ["supersecret", "42"])
    assert "supersecret" not in out
    assert "***" in out
    assert "42" in out, "short values (<4 chars) are not redacted"


def test_kubeconfig_tokens_extraction():
    try:
        import yaml  # noqa: F401
    except ModuleNotFoundError:
        return  # best-effort helper is a no-op without PyYAML; skip when absent
    kc = (
        "apiVersion: v1\n"
        "users:\n"
        "- name: eks\n"
        "  user:\n"
        "    token: k8s-bearer-abcdef\n"
    )
    assert acr._kubeconfig_tokens(kc) == ["k8s-bearer-abcdef"]


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
