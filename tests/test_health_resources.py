"""Health: resources the dashboard built or registered.

What these pin:

- every inventory object gets a row under its inventory id, directories excepted (they
  have their own area);
- a VM the cloud no longer has is failing; a stopped VM is fine only when something in
  the dashboard stopped it (a power action, a suspend schedule, a spend cap);
- impaired status checks fail, scheduled events degrade;
- managed database and Kubernetes states map per provider, node pools included;
- a cloud that cannot be asked makes its resources unknown, never failed;
- access wiring is reported only where it was set up, and a recorded onboarding failure
  degrades the resource; an auto-delete inside a day and a spend cap degrade it;
- reads are batched: one EC2 call per region however many VMs.

Uses a real temp SQLite database; every cloud read is stubbed.

Run: python tests/test_health_resources.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="health-res-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-health-resource-tests")

from web_dashboard.database import (Base, CertLab, CloudDatabase, K8sCluster,  # noqa: E402
                                    ManagedDirectory, SessionLocal, engine)
from web_dashboard.services import job_service  # noqa: E402
from web_dashboard.services.health import core  # noqa: E402
from web_dashboard.services.health.collectors import cloud_status, resources as R  # noqa: E402

Base.metadata.create_all(bind=engine)
NOW = datetime(2026, 10, 12, 12, 0, 0)


# ── pure mappings ─────────────────────────────────────────────────────────────

def test_vm_states():
    assert R.vm_check("aws", None)["status"] == "unknown"
    gone = R.vm_check("aws", {})
    assert gone["status"] == "fail" and "no longer has" in gone["detail"]
    assert R.vm_check("aws", {"state": "terminated"})["status"] == "fail"
    assert R.vm_check("aws", {"state": "running", "system": "ok", "instance": "ok",
                              "events": []})["status"] == "ok"
    impaired = R.vm_check("aws", {"state": "running", "system": "impaired",
                                  "instance": "ok", "events": []})
    assert impaired["status"] == "fail" and "system" in impaired["detail"]
    event = R.vm_check("aws", {"state": "running", "events": ["instance-retirement"]})
    assert event["status"] == "warn"
    assert R.vm_check("azure", {"power": "deallocated"})["status"] == "warn"
    fine = R.vm_check("azure", {"power": "deallocated"}, stop_expected="suspend schedule")
    assert fine["status"] == "ok" and "suspend schedule" in fine["detail"]
    assert R.vm_check("azure", {"power": "running", "provisioning": "failed"})["status"] == "fail"
    assert R.vm_check("gcp", {"state": "provisioning"})["status"] == "unknown"


def test_database_states():
    assert R.database_check("rds", "available")["status"] == "ok"
    assert R.database_check("rds", "stopped")["status"] == "warn"
    assert R.database_check("rds", "storage-full")["status"] == "fail"
    assert R.database_check("rds", {})["status"] == "fail"
    assert R.database_check("rds", None)["status"] == "unknown"
    assert R.database_check("cloudsql", {"state": "RUNNABLE", "activation": "ALWAYS"})["status"] == "ok"
    assert R.database_check("cloudsql", {"state": "RUNNABLE", "activation": "NEVER"})["status"] == "warn"
    assert R.database_check("cloudsql", {"state": "FAILED"})["status"] == "fail"
    assert R.database_check("flexibleserver", {"state": "Ready"})["status"] == "ok"
    assert R.database_check("flexibleserver", {"state": "Stopped"})["status"] == "warn"
    assert R.database_check("flexibleserver", {"state": "Disabled"})["status"] == "fail"


def test_cluster_states():
    eks = R.cluster_checks("aws", {"status": "ACTIVE", "issues": [], "nodegroups": {
        "ng1": {"status": "ACTIVE", "issues": []},
        "ng2": {"status": "DEGRADED", "issues": ["AsgInstanceLaunchFailures"]}}})
    keys = {c["key"]: c["status"] for c in eks}
    assert keys == {"cluster": "ok", "pool:ng1": "ok", "pool:ng2": "fail"}, keys
    issues = R.cluster_checks("aws", {"status": "ACTIVE", "issues": ["subnet full"],
                                      "nodegroups": {}})
    assert issues[0]["status"] == "warn"
    gke = R.cluster_checks("gcp", {"status": "ERROR", "message": "quota", "nodepools": {}})
    assert gke[0]["status"] == "fail" and "quota" in gke[0]["detail"]
    aks = R.cluster_checks("azure", {"provisioning": "Succeeded", "power": "Stopped",
                                     "pools": {"system": "Succeeded"}})
    assert [c["status"] for c in aks] == ["warn", "ok"]
    assert R.cluster_checks("aws", {})[0]["status"] == "fail"
    assert R.cluster_checks("aws", None)[0]["status"] == "unknown"


def test_lifecycle_and_wiring():
    soon = R.lifecycle_checks(expires_at=NOW + timedelta(hours=5), spend_capped=False,
                              spend_warned=False, now=NOW)
    assert soon and soon[0]["status"] == "warn" and "5 h" in soon[0]["detail"]
    assert not R.lifecycle_checks(expires_at=NOW + timedelta(days=3), spend_capped=False,
                                  spend_warned=False, now=NOW)
    capped = R.lifecycle_checks(expires_at=None, spend_capped=True, spend_warned=True, now=NOW)
    assert [c["key"] for c in capped] == ["spend"]
    assert R.wiring_checks() == [], "wiring that was never asked for was reported"
    wired = R.wiring_checks(ps_account="42", pra="jp-east", entitle="int-1")
    assert {c["key"]: c["status"] for c in wired} == {
        "password_safe": "ok", "pra": "ok", "entitle": "ok"}
    broken = R.wiring_checks(ps_system="7", ps_error="functional account missing")
    assert broken[0]["status"] == "warn" and "functional account" in broken[0]["detail"]


def test_stored_states():
    assert R.stored_check("active")["status"] == "ok"
    assert R.stored_check("failed")["status"] == "fail"
    assert R.stored_check("provisioning")["status"] == "unknown"
    assert R.stored_check("odd")["status"] == "warn"


# ── collection ────────────────────────────────────────────────────────────────

def _vm(db, instance_id, region="us-east-2", **meta_extra):
    meta = {"instance_id": instance_id, "instance_name": f"vm-{instance_id}",
            "region": region, **meta_extra}
    return job_service.create_job(db, job_type="ec2_deploy", created_by="t",
                                  metadata=meta, status="completed")


def test_collect_reads_each_region_once_and_maps_every_object():
    db = SessionLocal()
    a = _vm(db, "i-aaa")
    b = _vm(db, "i-bbb", ps_managed_system_id=12)
    gone = _vm(db, "i-gone")
    rds = CloudDatabase(engine="postgres", provider="rds", cloud="aws", region="us-east-2",
                        source="provisioned", status="available", instance_id="db-orders",
                        ps_managed_account_id="7", entitle_integration_id="int",
                        created_by="t")
    eks = K8sCluster(cloud="aws", name="demo", status="active", region="us-east-2",
                     source="provisioned", created_by="t")
    lab = CertLab(name="pki", cloud="aws", status="failed", created_by="t")
    ad = ManagedDirectory(name="corp.example.com", cloud="aws", provider="aws_managed_ad",
                          source="registered", status="available", created_by="t")
    db.add_all([rds, eks, lab, ad])
    db.commit()

    calls = []
    real = (cloud_status.aws_instances, cloud_status.aws_databases, cloud_status.aws_cluster)

    def fake_instances(region, ids):
        calls.append(("ec2", region, tuple(sorted(ids))))
        return {"i-aaa": {"state": "running", "system": "ok", "instance": "ok", "events": []},
                "i-bbb": {"state": "stopped", "system": "", "instance": "", "events": []}}

    def fake_dbs(region):
        return {"db-orders": "available"}

    def fake_cluster(region, name):
        raise RuntimeError("AccessDenied")
    try:
        cloud_status.aws_instances, cloud_status.aws_databases, cloud_status.aws_cluster = (
            fake_instances, fake_dbs, fake_cluster)
        items = {i.object_id: i for i in asyncio.run(R.collect(db))}
    finally:
        cloud_status.aws_instances, cloud_status.aws_databases, cloud_status.aws_cluster = real

    east = [c for c in calls if c[1] == "us-east-2"]
    assert east == [("ec2", "us-east-2", ("i-aaa", "i-bbb", "i-gone"))], calls
    assert len(calls) == len({c[1] for c in calls}), f"a region was read twice: {calls}"
    assert f"directory:{ad.id}" not in items, "directories belong to their own area"
    assert items[f"job:{a.id}"].status == "ok"
    stopped = items[f"job:{b.id}"]
    assert stopped.status == "warn" and any(c["key"] == "password_safe" for c in stopped.checks)
    assert items[f"job:{gone.id}"].status == "fail"
    db_item = items[f"clouddb:{rds.id}"]
    assert db_item.status == "ok" and {c["key"] for c in db_item.checks} >= {
        "server", "password_safe", "entitle"}
    assert items[f"k8s:{eks.id}"].status == "unknown", "an unreadable cloud failed a cluster"
    assert items[f"certlab:{lab.id}"].status == "fail"
    db.close()


def test_a_vm_the_dashboard_stopped_is_fine():
    from web_dashboard.services import spend_sweeper
    db = SessionLocal()
    vm = _vm(db, "i-ccc", region="eu-west-1")
    real_live, real_power = cloud_status.aws_instances, spend_sweeper.last_power_action
    try:
        cloud_status.aws_instances = lambda region, ids: {
            "i-ccc": {"state": "stopped", "system": "", "instance": "", "events": []}}
        spend_sweeper.last_power_action = lambda _db: {vm.id: "stop"}
        items = {i.object_id: i for i in asyncio.run(R.collect(db))}
    finally:
        cloud_status.aws_instances, spend_sweeper.last_power_action = real_live, real_power
    check = items[f"job:{vm.id}"].checks[0]
    assert check["status"] == "ok" and "stopped by the dashboard" in check["detail"]
    db.close()


def test_the_area_is_registered_and_always_on():
    from web_dashboard.services.health import registry
    c = registry.get("resources")
    assert c is not None and c.flags == () and c.interval_minutes >= 15


def test_no_status_read_reads_identities():
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "health", "collectors",
                            "cloud_status.py"), encoding="utf-8").read()
    for forbidden in ("list_users", "get_role", "list_roles", "iam.", "roleAssignments",
                      "checkout_credential"):
        assert forbidden not in src, f"cloud_status uses {forbidden}"


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
