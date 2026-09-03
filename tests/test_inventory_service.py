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
    # Every name inventory_service imports from web_dashboard.database. A missing one
    # does not fail this file -- it makes the import below raise and the whole thing
    # SKIP, silently, which is how PovEnvironment went unnoticed here for as long as it
    # did. If you add a model to that import, add it here.
    for name in ("Job", "CertLab", "CloudDatabase", "K8sCluster", "VirtualDesktop",
                 "HypervisorConnection", "HypervisorVMCache", "PovEnvironment"):
        setattr(db, name, type(name, (), {}))
    sys.modules["web_dashboard.database"] = db

    # sqlalchemy is only needed for the `Session` type hint and the query builder in
    # collect() / live_or_pending_vm_names, neither of which the pure mappers touch.
    # Stubbing it (as tests/test_job_batches.py does) means this file actually runs
    # on a checkout without the app requirements instead of skipping itself.
    sa = types.ModuleType("sqlalchemy")
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = type("Session", (), {})
    sa.orm = sa_orm
    sys.modules.setdefault("sqlalchemy", sa)
    sys.modules.setdefault("sqlalchemy.orm", sa_orm)


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
                expires_at=None, metadata_dict={})
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
    # Auto-delete timer. A VM has no table, so its expiry rides on the deploy Job —
    # and "provisioned" is true by construction: collect() only reaches _vm_item for a
    # deploy the dashboard itself ran.
    assert it["expires_at"] is None and it["source"] == "provisioned"


def test_vm_item_carries_its_expiry():
    exp = datetime(2026, 7, 1, 9, 30, 0)
    it = svc._vm_item(_job(expires_at=exp))
    assert it["expires_at"] == exp.isoformat()


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
    row = _db_row()
    it = svc._db_item(row)
    assert it["cloud"] == "azure" and it["kind"] == "database"
    # Drives the delete verb on /databases and the badge on /inventory.
    assert it["source"] == "provisioned"
    assert it["name"] == "postgres clouddb-ab" and it["state"] == "available"
    assert it["workgroup"] is None and it["detail_href"] == "/databases"
    assert it["id"] == "clouddb:d1234567"
    assert it["expires_at"] is None


def _db_row(**kw):
    base = dict(id="d1234567", cloud="azure", engine="postgres",
                instance_id="clouddb-ab", region="eastus",
                status="available", created_by="bob", created_at=_TS,
                source="provisioned", private_host=None, expires_at=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _k8s_row(**kw):
    base = dict(id="k1", cloud="gcp", name="prod-gke", region="us-central1",
                status="registered", deploy_job_id="j9", source="provisioned",
                created_by="bob", created_at=_TS, expires_at=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_k8s_item_shape():
    it = svc._k8s_item(_k8s_row())
    assert it["cloud"] == "gcp" and it["kind"] == "k8s"
    assert it["name"] == "prod-gke" and it["state"] == "registered"
    assert it["job_id"] == "j9" and it["detail_href"] == "/k8s"
    assert it["source"] == "provisioned" and it["expires_at"] is None


def test_db_and_k8s_items_carry_their_expiry():
    exp = datetime(2026, 7, 1, 9, 30, 0)
    assert svc._db_item(_db_row(expires_at=exp))["expires_at"] == exp.isoformat()
    assert svc._k8s_item(_k8s_row(expires_at=exp))["expires_at"] == exp.isoformat()


def test_a_registered_row_reports_its_source():
    """`source` is what excludes a registered resource from the auto-delete timer, in
    both the stamping and the reaping guard — so it has to reach the item."""
    assert svc._db_item(_db_row(source="registered"))["source"] == "registered"
    assert svc._k8s_item(_k8s_row(source="registered"))["source"] == "registered"
    # A K8sCluster row predating the `source` column defaults to registered, which is
    # the safe direction: unknown provenance is never auto-deleted.
    assert svc._k8s_item(_k8s_row(source=None))["source"] == "registered"


def test_desktop_item_includes_assignee():
    row = types.SimpleNamespace(id="v1", cloud="azure", pool_name="eng", assigned_user="carol",
                                status="running", created_by="bob", created_at=_TS)
    it = svc._desktop_item(row)
    assert it["kind"] == "desktop" and it["state"] == "running"
    assert "eng" in it["name"] and "carol" in it["name"]


def test_a_desktop_seat_never_carries_an_expiry():
    """virtual_desktops has no expires_at column on purpose: the teardown takes seat_ids,
    so expiring one seat would silently shrink a live pool. The mapper hardcodes None so
    nothing can stamp what the sweeper wouldn't honour."""
    row = types.SimpleNamespace(id="v1", cloud="azure", pool_name="eng", assigned_user=None,
                                status="running", created_by="bob", created_at=_TS)
    assert svc._desktop_item(row)["expires_at"] is None


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


# ── name claims (the pre-flight behind count-based deploys) ──────────────────
#
# live_or_pending_vm_names needs a Session, so what's tested here is the predicate it
# is built on. That predicate decides whether a deploy can reuse a name — and because
# Azure/GCP/OCI destroy resolve their deploy job by FIRST MATCH on name, getting it
# wrong means a destroy can target the wrong VM.

def test_name_claim_uses_the_same_key_order_as_the_inventory_mapper():
    assert svc._name_claimed_by(_job(metadata_dict={"instance_name": "web-01"})) == "web-01"
    assert svc._name_claimed_by(_job(metadata_dict={"vm_name": "vm-01"})) == "vm-01"
    assert svc._name_claimed_by(_job(metadata_dict={"name": "n-01"})) == "n-01"


def test_name_claim_falls_back_to_cloud_resource_id():
    """Azure/GCP/OCI set cloud_resource_id to the VM name at enqueue, so an in-flight
    row is still findable before its metadata is fully written."""
    assert svc._name_claimed_by(_job(metadata_dict={}, cloud_resource_id="i-123")) == "i-123"


def test_a_destroyed_vm_releases_its_name():
    job = _job(metadata_dict={"instance_name": "web-01", "destroyed": True})
    assert svc._name_claimed_by(job) is None


def test_a_nameless_job_claims_nothing():
    """Must be None, not '(unnamed)' — the mapper's placeholder would otherwise
    become a name that every nameless job collides with."""
    assert svc._name_claimed_by(_job(metadata_dict={}, cloud_resource_id=None)) is None
    assert svc._name_claimed_by(_job(metadata_dict={"instance_name": "   "},
                                     cloud_resource_id="")) is None


def test_in_flight_statuses_hold_a_name():
    """A batch submitted while another is still running has to see those names, or
    both batches pick the same ones and collide at launch."""
    for status in ("pending", "queued", "running"):
        assert status in svc._NAME_HOLDING_STATUSES
    assert "completed" in svc._NAME_HOLDING_STATUSES
    assert "failed" not in svc._NAME_HOLDING_STATUSES
    assert "cancelled" not in svc._NAME_HOLDING_STATUSES



# ── Synced hypervisor VMs ─────────────────────────────────────────────────────
#
# The pure mappers only. The join, the override lookup and the dedup need a database and
# are covered in tests/test_inventory_hypervisor.py.

def _conn(**kw):
    base = dict(id="c1", kind="workstation", name="my-ws", site="")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _row(**kw):
    base = dict(vm_id="AB12", name="win11-lab", power_state="poweredOn", scope="",
                ip_addresses="[]", vcpus=4, mem_mib=8192)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_hv_item_workstation_shape():
    it = svc._hv_item(_conn(site="home"), _row(scope=r"C:\VMs\win11\win11.vmx"),
                      None, ["10.0.0.5"])
    assert it["id"] == "hv:c1:AB12"
    assert it["cloud"] == "workstation" and it["kind"] == "vm"
    assert it["name"] == "win11-lab" and it["state"] == "running"
    assert it["ip"] == "10.0.0.5" and it["detail_href"] == "/vms"
    assert it["expires_at"] is None and it["job_id"] is None
    assert it["created_at"] is None, "synced_at is not a creation date"
    assert it["region"] == "home", (
        "a VMX path is per-VM and would make one Region entry per row")


def test_hv_item_proxmox_puts_the_node_in_region():
    """A deployed Proxmox VM already reports its node as `region`, so a synced one has to
    agree or the same node appears twice in the Region dropdown."""
    it = svc._hv_item(_conn(kind="proxmox", id="c2"),
                      _row(vm_id="100", name="web-01", power_state="running",
                           scope="pve1"), "dev", [])
    assert it["region"] == "pve1"
    assert it["workgroup"] == "dev" and it["ip"] == ""


def test_a_synced_row_declares_its_own_source_and_no_deployer():
    it = svc._hv_item(_conn(), _row(), None, [])
    assert it["source"] == "hypervisor", (
        "not 'registered': the dashboard holds no record of this VM at all")
    assert it["deployed_by"] is None, (
        "load-bearing — visible_to compares this against the caller, so None is what "
        "makes an untagged synced VM admin-only")


def test_the_override_key_matches_each_api_modules_own():
    """The highest-value guard here. If these drift, an admin's assignment on /proxmox
    silently stops applying to the inventory and nothing else notices."""
    import ast as _ast
    api_dir = os.path.join(_ROOT, "web_dashboard", "api")
    for module, vm, expected in (
            ("proxmox", {"node": "pve1", "vmid": "100"}, "pve1/100"),
            ("nutanix", {"uuid": "u-1"}, "u-1"),
            ("hyperv", {"vmid": "g-1"}, "g-1"),
            ("vsphere", {"moref": "vm-42"}, "vm-42"),
            ("xcpng", {"uuid": "x-1"}, "x-1")):
        with open(os.path.join(api_dir, f"{module}.py"), encoding="utf-8") as fh:
            tree = _ast.parse(fh.read())
        fn = next(n for n in tree.body
                  if isinstance(n, _ast.FunctionDef) and n.name == "_override_key")
        ns = {}
        exec(compile(_ast.Module(body=[fn], type_ignores=[]), "<k>", "exec"), ns)
        theirs = ns["_override_key"](vm)
        assert theirs == expected, f"{module}: {theirs!r}"
        scope = vm.get("node", "")
        vm_id = vm.get("vmid") or vm.get("uuid") or vm.get("moref")
        assert svc._hv_override_key(module, vm_id, scope) == theirs, (
            f"{module}: inventory builds {svc._hv_override_key(module, vm_id, scope)!r}, "
            f"api/{module}.py builds {theirs!r}")


def test_a_completed_deploy_claims_the_vm_it_created():
    """set_completed merges the deploy result into the job metadata, so a completed
    proxmox_deploy carries its vmid — which makes the dedup exact rather than by name."""
    job = _job(job_type="proxmox_deploy",
               metadata_dict={"connection_id": "c2", "vmid": 100,
                              "vm_name": "web-01", "node": "pve1"})
    conn = _conn(id="c2", kind="proxmox")
    row = _row(vm_id="100", name="web-01", scope="pve1")
    assert svc._job_match_keys(job) & svc._hv_match_keys(conn, row), (
        "an int vmid in metadata must still match a string vm_id in the cache")


def test_an_older_deploy_still_claims_it_by_name_and_node():
    job = _job(job_type="proxmox_deploy",
               metadata_dict={"vm_name": "Web-01", "node": "PVE1"})
    conn = _conn(id="c2", kind="proxmox")
    row = _row(vm_id="100", name="web-01", scope="pve1")
    assert svc._job_match_keys(job) & svc._hv_match_keys(conn, row), (
        "casefolded, so capitalisation cannot produce a duplicate row")


def test_a_deploy_for_a_different_vm_claims_nothing_of_this_one():
    job = _job(job_type="proxmox_deploy",
               metadata_dict={"connection_id": "c2", "vmid": 999, "vm_name": "other",
                              "node": "pve1"})
    conn = _conn(id="c2", kind="proxmox")
    row = _row(vm_id="100", name="web-01", scope="pve1")
    assert not (svc._job_match_keys(job) & svc._hv_match_keys(conn, row))


def test_a_non_hypervisor_deploy_claims_nothing():
    assert svc._job_match_keys(_job(job_type="ec2_deploy")) == set()

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
