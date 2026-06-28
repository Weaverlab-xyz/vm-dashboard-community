"""Unit tests for inventory_service — the cross-provider deployment inventory.

Covers the pure row→item mappers (each source normalizes to the right
cloud/kind/region/state/name, with the VM metadata-key fallbacks) and the RBAC
visibility predicate. The DB-querying ``collect`` isn't exercised here (it needs a
live session); the mappers take row-like objects so they test without a DB.
Heavy deps (web_dashboard.database → bcrypt) are stubbed in sys.modules. Runs
under pytest, or standalone:  python tests/test_inventory_service.py
"""
import os
import sys
import types
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_stubs():
    db = types.ModuleType("web_dashboard.database")
    for name in ("Job", "CloudDatabase", "K8sCluster", "VirtualDesktop"):
        setattr(db, name, type(name, (), {}))
    sys.modules["web_dashboard.database"] = db


_install_stubs()
try:
    from web_dashboard.services import inventory_service as svc
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"inventory_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_TS = datetime(2026, 6, 28, 12, 0, 0)


def _job(**kw):
    base = dict(id="j1", job_type="ec2_deploy", workgroup="hydra",
                cloud_resource_id="i-123", created_by="alice", created_at=_TS,
                metadata_dict={})
    base.update(kw)
    return types.SimpleNamespace(**base)


# ── VM mapper ────────────────────────────────────────────────────────────────

def test_vm_item_aws_shape():
    it = svc._vm_item(_job(metadata_dict={"instance_name": "web-1", "region": "us-east-2"}))
    assert it["cloud"] == "aws" and it["kind"] == "vm"
    assert it["name"] == "web-1" and it["region"] == "us-east-2"
    assert it["state"] == "active"
    assert it["workgroup"] == "hydra" and it["deployed_by"] == "alice"
    assert it["job_id"] == "j1" and it["detail_href"] == "/aws#instances"
    assert it["created_at"] == _TS.isoformat()
    assert it["id"] == "job:j1"


def test_vm_item_region_fallbacks():
    cases = {
        "azure_deploy":   ("location", "azure", "/azure#vms"),
        "gce_deploy":     ("zone",     "gcp",   "/gcp"),
        "proxmox_deploy": ("node",     "proxmox", "/proxmox"),
        "nutanix_deploy": ("cluster",  "nutanix", "/nutanix"),
    }
    for jt, (key, cloud, href) in cases.items():
        it = svc._vm_item(_job(job_type=jt, metadata_dict={"vm_name": "x", key: "R"}))
        assert it["cloud"] == cloud and it["detail_href"] == href
        assert it["region"] == "R"


def test_vm_item_name_fallback_to_resource_id_then_placeholder():
    assert svc._vm_item(_job(metadata_dict={}))["name"] == "i-123"  # cloud_resource_id
    assert svc._vm_item(_job(metadata_dict={}, cloud_resource_id=None))["name"] == "(unnamed)"


# ── table mappers ────────────────────────────────────────────────────────────

def test_db_item_shape():
    row = types.SimpleNamespace(id="d1234567", cloud="azure", engine="postgres",
                                instance_id="clouddb-ab", region="eastus",
                                status="available", created_by="bob", created_at=_TS)
    it = svc._db_item(row)
    assert it["cloud"] == "azure" and it["kind"] == "database"
    assert it["name"] == "postgres clouddb-ab" and it["state"] == "available"
    assert it["workgroup"] is None and it["detail_href"] == "/databases"
    assert it["id"] == "clouddb:d1234567"


def test_k8s_item_shape():
    row = types.SimpleNamespace(id="k1", cloud="gcp", name="prod-gke", region="us-central1",
                                status="registered", deploy_job_id="j9",
                                created_by="bob", created_at=_TS)
    it = svc._k8s_item(row)
    assert it["cloud"] == "gcp" and it["kind"] == "k8s"
    assert it["name"] == "prod-gke" and it["state"] == "registered"
    assert it["job_id"] == "j9" and it["detail_href"] == "/k8s"


def test_desktop_item_includes_assignee():
    row = types.SimpleNamespace(id="v1", cloud="azure", pool_name="eng", assigned_user="carol",
                                status="running", created_by="bob", created_at=_TS)
    it = svc._desktop_item(row)
    assert it["kind"] == "desktop" and it["state"] == "running"
    assert "eng" in it["name"] and "carol" in it["name"]


# ── RBAC predicate ───────────────────────────────────────────────────────────

def test_visible_admin_sees_all():
    assert svc.visible_to({"workgroup": "x", "deployed_by": "z"}, None, "anyone") is True


def test_visible_workgroup_scoped():
    accessible = ["hydra", "weaverlab"]
    assert svc.visible_to({"workgroup": "hydra", "deployed_by": "x"}, accessible, "me") is True
    assert svc.visible_to({"workgroup": "secret", "deployed_by": "x"}, accessible, "me") is False


def test_visible_nonworkgroup_is_owner_only():
    # DB/k8s/desktop items have no workgroup → only the creator sees them.
    item = {"workgroup": None, "deployed_by": "alice"}
    assert svc.visible_to(item, ["hydra"], "alice") is True
    assert svc.visible_to(item, ["hydra"], "bob") is False


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
