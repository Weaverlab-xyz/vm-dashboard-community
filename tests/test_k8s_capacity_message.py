"""Unit tests for k8s_service._explain_apply_failure (zonal-capacity provisions).

A GKE cluster's create includes a one-node default pool in a SINGLE zone, so a
zone with no room for the chosen machine type fails the apply ~35 min in with a
wall of instance-group text ("Not all instances running in IGM … [GCE_STOCKOUT]").
Retrying the same zone just repeats it, so the job's failure message leads with
the zone, the machine type and the way out. This pins that wrapping — and that
unrelated failures (and non-GCP clouds) are passed through untouched.

Heavy deps (sqlalchemy, database, config, config_service, region_config, yaml) are
stubbed in sys.modules — the function under test is pure string handling.
Runs under pytest, or standalone:  python tests/test_k8s_capacity_message.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The tail of a real failed GKE apply (trimmed).
STOCKOUT = (
    "terraform apply failed:\n"
    "Error: Error waiting for creating GKE cluster: Google Compute Engine: Not all "
    "instances running in IGM after 35m6.9s. Expected 1, running 0, transitioning 1. "
    "Current errors: [GCE_STOCKOUT]: Instance "
    "'gke-k8s-test-default-pool-934f544e-10vf' creation failed: The zone "
    "'projects/p/zones/us-central1-c' does not have enough resources available to "
    "fulfill the request.  Try a different zone, or try again later."
)


class _Settings:
    def __getattr__(self, _key):
        return ""


def _install_stubs():
    # k8s_service imports sqlalchemy.orm.Session (type hints only) + yaml at module
    # level. Stub them ONLY when actually absent — gating on `not in sys.modules`
    # would shadow the real libraries in CI, where they are installed but not yet
    # imported (see test_stub_guards.py).
    try:
        from sqlalchemy.orm import Session  # noqa: F401
    except Exception:
        sa = types.ModuleType("sqlalchemy")
        orm = types.ModuleType("sqlalchemy.orm")
        orm.Session = type("Session", (), {})
        sa.orm = orm
        sys.modules["sqlalchemy"] = sa
        sys.modules["sqlalchemy.orm"] = orm
    try:
        import yaml  # noqa: F401
    except Exception:
        y = types.ModuleType("yaml")
        y.safe_load = lambda *_a, **_k: {}
        y.safe_dump = lambda *_a, **_k: ""
        sys.modules["yaml"] = y

    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.Job = type("Job", (), {})
    dbmod.K8sCluster = type("K8sCluster", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    sys.modules["web_dashboard.services.config_service"] = cfg

    rc = types.ModuleType("web_dashboard.services.region_config")
    rc.resolve_azure_region = lambda region: {}
    rc.resolve_region = lambda cloud, region: {}
    sys.modules["web_dashboard.services.region_config"] = rc


_install_stubs()
try:
    from web_dashboard.services import k8s_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"k8s_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def test_stockout_leads_with_the_zone_and_machine_type():
    msg = svc._explain_apply_failure(
        "gcp", {"zone": "us-central1-c", "machine_type": "e2-medium"}, STOCKOUT)
    head = msg.split("\n", 1)[0]
    assert head.startswith("us-central1-c has no capacity for e2-medium")
    assert "Multi-region" in msg, "the operator needs to be told where to change the zone"
    assert STOCKOUT in msg, "the raw terraform output must still be there"


def test_stockout_without_explicit_vars_still_explains():
    # Blank zone/machine_type = the module's own defaults; the message must not
    # render "None" or "" where the zone belongs.
    msg = svc._explain_apply_failure("gcp", {}, STOCKOUT)
    assert msg.startswith("the region's first available zone has no capacity for "
                          "the node machine type")


def test_other_gcp_failures_pass_through():
    other = "terraform apply failed:\nError: Error 403: Permission denied on 'locations/x'"
    assert svc._explain_apply_failure("gcp", {"zone": "us-central1-c"}, other) == other


def test_non_gcp_clouds_pass_through():
    # Only GKE pins a single zone this way; don't put GCP advice on an AWS/Azure job.
    assert svc._explain_apply_failure("aws", {}, STOCKOUT) == STOCKOUT
    assert svc._explain_apply_failure("azure", {}, STOCKOUT) == STOCKOUT


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
