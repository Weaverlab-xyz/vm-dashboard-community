"""Unit test for the GKE↔sandbox VPC peering variable threading.

A dashboard-managed GKE cluster builds its own self-contained VPC, so an
in-cluster Entitle agent has no route to the private lab VMs (in the sandbox
`vm-subnet`) and resource sync fails "Failed to fetch the resources of <target>".
The fix mirrors the aws_eks pattern: `_build_cluster_tf_variables` emits
`sandbox_network` + `sandbox_vm_target_tags` for the gcp module, which then peers
its VPC back to the sandbox VPC and opens vm-subnet:22. This locks in that the
gcp branch threads those vars from config (and omits them when unconfigured, so
the cluster stays fully isolated — the pre-fix behavior).

Stubs the DB / sqlalchemy / config imports so k8s_service loads without an
app/DB (same lightweight approach as test_entitle_agent_rbac). Runs under pytest
or standalone:
    python tests/test_gke_sandbox_peering.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub the heavy module-load deps so k8s_service imports without a real DB engine.
_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
_orm_stub = types.ModuleType("sqlalchemy.orm")
_orm_stub.Session = object
sys.modules.setdefault("sqlalchemy.orm", _orm_stub)

_db_stub = types.ModuleType("web_dashboard.database")
_db_stub.Job = type("Job", (), {})
_db_stub.K8sCluster = type("K8sCluster", (), {})
sys.modules.setdefault("web_dashboard.database", _db_stub)

# A controllable config_service stub — `_cfg` / `_cfg_list` read through this.
_CONFIG = {}
_cfgsvc_stub = types.ModuleType("web_dashboard.services.config_service")
_cfgsvc_stub.get = lambda key, default=None: _CONFIG.get(key, "")
sys.modules.setdefault("web_dashboard.services.config_service", _cfgsvc_stub)

# A region_config stub mirroring resolve_region: per-region key
# (<cloud>_region.<region>.<field>) wins, else the flat key. Only the fields the
# k8s peering branches read are modeled.
_REGION_FIELDS = {
    "gcp": {"network": "gcp_network", "default_network_tag": "gcp_default_network_tag",
            "k8s_subnetwork": "gcp_k8s_subnetwork",
            "k8s_pods_range": "gcp_k8s_pods_range_name",
            "k8s_services_range": "gcp_k8s_services_range_name",
            "k8s_node_tag": "gcp_k8s_node_tag"},
    "azure": {"default_subnet_id": "azure_default_subnet_id",
              "vnet_resource_group": "azure_vnet_resource_group",
              "resource_group": "azure_resource_group"},
}


def _stub_resolve_region(cloud, region):
    out = {}
    for fld, flat in _REGION_FIELDS.get(cloud, {}).items():
        out[fld] = _CONFIG.get(f"{cloud}_region.{region}.{fld}") or _CONFIG.get(flat, "")
    return out


_region_stub = types.ModuleType("web_dashboard.services.region_config")
_region_stub.resolve_region = _stub_resolve_region
_region_stub.resolve_azure_region = lambda region: _stub_resolve_region("azure", region)
sys.modules["web_dashboard.services.region_config"] = _region_stub

from web_dashboard.services import k8s_service as k  # noqa: E402


def _gcp_vars():
    return k._build_cluster_tf_variables(
        cloud="gcp", cluster_id="c-123", name="gke-demo",
        region="us-central1", opts={})


def test_gcp_emits_peering_vars_when_sandbox_configured():
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project": "sandbox-proj",
        "gcp_network": "dashboard-sandbox-vpc",
        "gcp_default_network_tag": "bt-vm",
    })
    tf = _gcp_vars()
    assert tf["sandbox_network"] == "dashboard-sandbox-vpc"
    assert tf["sandbox_vm_target_tags"] == ["bt-vm"]


def test_gcp_omits_peering_when_network_unset_or_default():
    # Unset → fully isolated cluster (pre-fix behavior preserved).
    _CONFIG.clear()
    _CONFIG.update({"gcp_project": "sandbox-proj"})
    tf = _gcp_vars()
    assert "sandbox_network" not in tf
    assert "sandbox_vm_target_tags" not in tf

    # The stock "default" VPC is not a sandbox → also skip peering.
    _CONFIG.update({"gcp_network": "default", "gcp_default_network_tag": "bt-vm"})
    tf = _gcp_vars()
    assert "sandbox_network" not in tf
    assert "sandbox_vm_target_tags" not in tf


def test_gcp_peers_network_but_omits_firewall_without_tag():
    # sandbox_network set but no VM tag → peer (routes) but skip the VM firewall.
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project": "sandbox-proj",
        "gcp_network": "dashboard-sandbox-vpc",
    })
    tf = _gcp_vars()
    assert tf["sandbox_network"] == "dashboard-sandbox-vpc"
    assert "sandbox_vm_target_tags" not in tf


def test_gcp_colocates_when_k8s_subnet_configured():
    # gcp_k8s_subnetwork set → provision the cluster IN the sandbox VPC
    # (co-location, so the agent reaches VMs AND Cloud SQL): emit existing_network/
    # subnetwork + pod/service range names + node tag, and NOT the peering vars.
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project": "sandbox-proj",
        "gcp_network": "dashboard-sandbox-vpc",
        "gcp_default_network_tag": "bt-vm",
        "gcp_k8s_subnetwork": "projects/p/regions/us-central1/subnetworks/sb-k8s-subnet",
        "gcp_k8s_pods_range_name": "gke-pods",
        "gcp_k8s_services_range_name": "gke-services",
        "gcp_k8s_node_tag": "sb-k8s",
    })
    tf = _gcp_vars()
    assert tf["existing_network"] == "dashboard-sandbox-vpc"
    assert tf["existing_subnetwork"].endswith("/sb-k8s-subnet")
    assert tf["pods_range_name"] == "gke-pods"
    assert tf["services_range_name"] == "gke-services"
    assert tf["node_network_tags"] == ["sb-k8s"]
    # Co-located → the self-contained-VPC peering vars are NOT emitted.
    assert "sandbox_network" not in tf
    assert "sandbox_vm_target_tags" not in tf


def test_gcp_peering_still_used_when_k8s_subnet_unset():
    # No gcp_k8s_subnetwork → self-contained VPC + peering (unchanged fallback).
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project": "sandbox-proj",
        "gcp_network": "dashboard-sandbox-vpc",
        "gcp_default_network_tag": "bt-vm",
    })
    tf = _gcp_vars()
    assert tf["sandbox_network"] == "dashboard-sandbox-vpc"
    assert "existing_network" not in tf


_AZ_SUBNET_ID = ("/subscriptions/s/resourceGroups/vm-cli-rg/providers/"
                 "Microsoft.Network/virtualNetworks/dashboard-sandbox-vnet/subnets/vm-subnet")
_AZ_VNET_ID = ("/subscriptions/s/resourceGroups/vm-cli-rg/providers/"
               "Microsoft.Network/virtualNetworks/dashboard-sandbox-vnet")


def _azure_vars():
    return k._build_cluster_tf_variables(
        cloud="azure", cluster_id="c-123", name="aks-demo",
        region="eastus", opts={})


def test_azure_derives_vnet_from_subnet_id():
    # Older sandbox: only the vm-subnet id is emitted → derive the VNet id + name.
    _CONFIG.clear()
    _CONFIG.update({
        "azure_default_subnet_id": _AZ_SUBNET_ID,
        "azure_vnet_resource_group": "vm-cli-rg",
    })
    tf = _azure_vars()
    assert tf["sandbox_vnet_id"] == _AZ_VNET_ID
    assert tf["sandbox_vnet_name"] == "dashboard-sandbox-vnet"
    assert tf["sandbox_vnet_rg"] == "vm-cli-rg"


def test_azure_prefers_explicit_vnet_id():
    _CONFIG.clear()
    _CONFIG.update({
        "azure_vnet_id": _AZ_VNET_ID,
        "azure_vnet_name": "explicit-vnet",
        "azure_vnet_resource_group": "vm-cli-rg",
    })
    tf = _azure_vars()
    assert tf["sandbox_vnet_id"] == _AZ_VNET_ID
    assert tf["sandbox_vnet_name"] == "explicit-vnet"


def test_azure_omits_peering_without_vnet_or_subnet():
    _CONFIG.clear()
    tf = _azure_vars()
    assert "sandbox_vnet_id" not in tf
    assert "sandbox_vnet_name" not in tf


def test_gcp_resolves_per_region_network():
    # A non-default region picks THAT region's network/tag, not the flat default.
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project": "sandbox-proj",
        "gcp_network": "default-region-vpc",
        "gcp_default_network_tag": "default-tag",
        "gcp_region.us-east1.network": "east-vpc",
        "gcp_region.us-east1.default_network_tag": "east-tag",
    })
    tf = k._build_cluster_tf_variables(
        cloud="gcp", cluster_id="c", name="d", region="us-east1", opts={})
    assert tf["sandbox_network"] == "east-vpc"
    assert tf["sandbox_vm_target_tags"] == ["east-tag"]


def test_azure_resolves_per_region_subnet():
    # A non-default region derives the VNet from THAT region's vm-subnet.
    _CONFIG.clear()
    east_subnet = ("/subscriptions/s/resourceGroups/east-rg/providers/"
                   "Microsoft.Network/virtualNetworks/east-vnet/subnets/vm-subnet")
    _CONFIG.update({
        "azure_default_subnet_id": _AZ_SUBNET_ID,
        "azure_vnet_resource_group": "vm-cli-rg",
        "azure_region.eastus2.default_subnet_id": east_subnet,
        "azure_region.eastus2.vnet_resource_group": "east-rg",
    })
    tf = k._build_cluster_tf_variables(
        cloud="azure", cluster_id="c", name="d", region="eastus2", opts={})
    assert tf["sandbox_vnet_id"].endswith("/virtualNetworks/east-vnet")
    assert tf["sandbox_vnet_name"] == "east-vnet"
    assert tf["sandbox_vnet_rg"] == "east-rg"


if __name__ == "__main__":
    fns = [v for k_, v in sorted(globals().items()) if k_.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
