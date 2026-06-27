"""Unit tests for k8s_service._build_cluster_tf_variables (+ _eks_name).

The cluster -var builder maps a cloud + opts to the Terraform variable dict for
the EKS / AKS / GKE module. The per-cloud divergence is exactly the kind of
thing that breaks silently when a cloud is added: the node-size var is
`node_instance_type` (EKS) vs `vm_size` (AKS) vs `machine_type` (GKE), and the
node-count var is `node_desired` (EKS) vs `node_count` (AKS/GKE). This pins that
mapping down without Terraform or a cloud account.

`_gke_name` is already covered in test_runner_kubeconfig.py, so it isn't
re-tested here. Heavy deps (database → bcrypt, config_service, region_config)
are stubbed in sys.modules; config lookups route through a controllable dict.
Runs under pytest, or standalone:  python tests/test_k8s_tf_vars.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


class _Settings:
    """Stand-in for the pydantic Settings: any unknown key resolves to ""."""
    def __getattr__(self, _key):
        return ""


def _install_stubs():
    # _cfg lazily does `from ..config import settings`; stub it so the builder
    # runs without pydantic (config lookups route through config_service below).
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.Job = type("Job", (), {})
    dbmod.K8sCluster = type("K8sCluster", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    sys.modules["web_dashboard.services.config_service"] = cfg

    # The AKS branch resolves the dashboard's resource group for the region.
    rc = types.ModuleType("web_dashboard.services.region_config")
    rc.resolve_azure_region = lambda region: {"resource_group": "rg-test"}
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


def _build(cloud, **over):
    args = dict(cloud=cloud, cluster_id="cid-123", name="demo", region="r1", opts={})
    args.update(over)
    return svc._build_cluster_tf_variables(**args)


# ── AWS / EKS ────────────────────────────────────────────────────────────────

def test_aws_basics_and_node_desired():
    tf = _build("aws", opts={
        "subnet_ids": ["subnet-a", "subnet-b"], "node_instance_type": "m5.large",
        "k8s_version": "1.29", "node_count": 3,
    })
    assert tf["cluster_name"] == "k8s-demo"            # _eks_name("k8s-demo")
    assert tf["region"] == "r1"
    assert tf["subnet_ids"] == ["subnet-a", "subnet-b"]
    assert tf["tags"] == {"managed-by": "vm-dashboard", "k8s-cluster-id": "cid-123"}
    assert tf["k8s_version"] == "1.29"
    assert tf["node_instance_type"] == "m5.large"
    assert tf["node_desired"] == 3                     # EKS uses node_desired
    assert "node_count" not in tf
    assert "vm_size" not in tf and "machine_type" not in tf


def test_aws_subnet_fallback_from_config():
    CONF.clear()
    CONF.update({"aws_k8s_subnet_a_id": "sn-a", "aws_k8s_subnet_b_id": "sn-b"})
    try:
        assert _build("aws")["subnet_ids"] == ["sn-a", "sn-b"]
    finally:
        CONF.clear()


# ── Azure / AKS ──────────────────────────────────────────────────────────────

def test_azure_uses_vm_size_and_resource_group():
    tf = _build("azure", opts={
        "node_instance_type": "Standard_D2s_v3", "node_count": 2,
        "authorized_cidrs": ["10.0.0.0/8"],
    })
    assert tf["location"] == "r1"
    assert tf["cluster_name"] == "k8s-demo"
    assert tf["resource_group_name"] == "rg-test"      # from region_config stub
    assert tf["vm_size"] == "Standard_D2s_v3"          # AKS node-size var
    assert tf["node_count"] == 2                        # AKS uses node_count
    assert tf["authorized_ip_ranges"] == ["10.0.0.0/8"]
    assert "node_desired" not in tf and "machine_type" not in tf


# ── GCP / GKE ────────────────────────────────────────────────────────────────

def test_gcp_uses_machine_type_and_project():
    CONF.clear()
    CONF["gcp_project"] = "proj-x"
    try:
        tf = _build("gcp", name="Demo", opts={
            "node_instance_type": "e2-standard-2", "node_count": 4,
            "authorized_cidrs": ["10.0.0.0/8"], "zone": "us-central1-a",
        })
        assert tf["project"] == "proj-x"
        assert tf["cluster_name"] == "k8s-demo"         # _gke_name lowercases
        assert tf["machine_type"] == "e2-standard-2"    # GKE node-size var
        assert tf["node_count"] == 4
        assert tf["authorized_cidrs"] == ["10.0.0.0/8"]
        assert tf["zone"] == "us-central1-a"
        assert "vm_size" not in tf and "node_desired" not in tf
    finally:
        CONF.clear()


def test_gcp_without_project_raises():
    CONF.clear()
    try:
        _build("gcp")
    except svc.K8sError:
        return
    finally:
        CONF.clear()
    raise AssertionError("expected K8sError when gcp_project is unconfigured")


# ── cross-cloud node-size var mapping (the regression this file guards) ───────

def test_node_instance_type_maps_to_the_right_per_cloud_var():
    CONF.clear()
    CONF["gcp_project"] = "proj-x"
    try:
        opts = {"node_instance_type": "SIZE"}
        assert _build("aws", opts=opts)["node_instance_type"] == "SIZE"
        assert _build("azure", opts=opts)["vm_size"] == "SIZE"
        assert _build("gcp", opts=opts)["machine_type"] == "SIZE"
    finally:
        CONF.clear()


def test_unknown_cloud_raises_not_implemented():
    try:
        _build("local")
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError for an unimplemented cloud")


# ── _eks_name ────────────────────────────────────────────────────────────────

def test_eks_name_sanitizes_and_caps():
    assert svc._eks_name("k8s-My Cluster!") == "k8s-My-Cluster"  # space/'!' → '-', no lowercasing
    assert svc._eks_name("@@@") == "k8s-"                        # all-symbol slug → prefixed
    assert len(svc._eks_name("k8s-" + "x" * 200)) <= 100


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
