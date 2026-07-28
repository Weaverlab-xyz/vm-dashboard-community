"""Unit tests: inventory_service.plan_bulk_run — the guard on a bulk
Config-Management selection made from the inventory page.

The inventory lists everything the dashboard has deployed, which is a strictly
larger set than Config Management can target. These assertions pin the three ways a
selection can be invalid, because each one fails differently and only the first is
obvious:

  * MIXED KINDS. VMs are configured over SSH; Kubernetes clusters and databases by a
    localhost play reaching out over a kubeconfig / DB login. Different request
    fields, different runner, and a playbook for one is meaningless against another —
    so one bulk run targets one kind.
  * A KIND WITH NO PATH AT ALL. A virtual-desktop seat has no Ansible target behind
    it and can never be selected, whether or not the selection is homogeneous.
  * A ROW THAT ISN'T INDIVIDUALLY TARGETABLE. Proxmox/Nutanix VMs record no IP (they
    are configured through their hypervisor group), a database engine may have no
    client library in the runner image, and a cluster's cloud may have no runner.

Plus the two that protect the fan-out itself: unknown ids are refused (that's what
stops a client naming a resource its RBAC filter hid), and the batch is capped.

The planner is pure — it takes already-filtered items — so all of this runs with no
DB and no app. Runs under pytest, or standalone:
    python tests/test_inventory_bulk_run.py
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
    sys.modules.setdefault("sqlalchemy", sa)
    sys.modules.setdefault("sqlalchemy.orm", sa_orm)

    db = types.ModuleType("web_dashboard.database")
    for name in ("CloudDatabase", "Job", "K8sCluster", "VirtualDesktop"):
        setattr(db, name, type(name, (), {}))
    sys.modules.setdefault("web_dashboard.database", db)

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.resolve_reference = lambda ref: ref
    cfg.is_reference = lambda ref: False
    sys.modules.setdefault("web_dashboard.services.config_service", cfg)

    # ansible_cloud_run_service is imported by _target_spec for its target-cloud
    # tuples; its other siblings are only imported, never called, at module load.
    for name in ("cloud_database_service", "job_service", "k8s_runner_service",
                 "k8s_service", "storage_service"):
        sys.modules.setdefault(f"web_dashboard.services.{name}",
                               types.ModuleType(f"web_dashboard.services.{name}"))


_install_stubs()
try:
    from web_dashboard.services import inventory_service as inv
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"inventory_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── fixtures: inventory rows in the shape collect() emits ─────────────────────

def _vm(id_="job:1", name="web-01", cloud="aws", ip="10.0.0.5"):
    return {"id": id_, "kind": "vm", "cloud": cloud, "name": name, "ip": ip}


def _k8s(id_="k8s:abc", name="prod", cloud="aws"):
    return {"id": id_, "kind": "k8s", "cloud": cloud, "name": name}


def _db(id_="clouddb:d1", name="postgres d1", cloud="aws", engine="postgres"):
    return {"id": id_, "kind": "database", "cloud": cloud, "name": name, "engine": engine}


def _desktop(id_="vdesktop:v1", name="pool-a"):
    return {"id": id_, "kind": "desktop", "cloud": "aws", "name": name}


def _expect_error(items, ids, *fragments):
    try:
        inv.plan_bulk_run(items, ids)
    except inv.BulkSelectionError as e:
        for frag in fragments:
            assert frag in str(e), f"expected {frag!r} in error, got: {e}"
        return str(e)
    raise AssertionError(f"expected BulkSelectionError for {ids}")


# ── the kind guard ────────────────────────────────────────────────────────────

def test_mixing_k8s_and_database_is_refused():
    items = [_k8s(), _db()]
    _expect_error(items, ["k8s:abc", "clouddb:d1"], "one kind of resource at a time",
                  "database", "k8s")


def test_mixing_vm_and_database_is_refused():
    items = [_vm(), _db()]
    _expect_error(items, ["job:1", "clouddb:d1"], "one kind of resource at a time")


def test_mixing_vm_and_k8s_is_refused():
    items = [_vm(), _k8s()]
    _expect_error(items, ["job:1", "k8s:abc"], "one kind of resource at a time")


def test_desktops_are_never_selectable():
    """Even a homogeneous selection — there is no Ansible target behind a seat."""
    items = [_desktop()]
    _expect_error(items, ["vdesktop:v1"], "cannot be a Config-Management target")


# ── homogeneous selections that should work ───────────────────────────────────

def test_many_vms_resolve_to_ip_targets():
    items = [_vm("job:1", "web-01", ip="10.0.0.5"), _vm("job:2", "web-02", ip="10.0.0.6")]
    plan = inv.plan_bulk_run(items, ["job:1", "job:2"])
    assert plan["kind"] == "vm"
    assert [t["spec"]["target"] for t in plan["targets"]] == ["10.0.0.5", "10.0.0.6"]
    assert all(t["spec"]["cloud"] == "aws" for t in plan["targets"])


def test_many_clusters_resolve_to_target_ids():
    items = [_k8s("k8s:a", "one"), _k8s("k8s:b", "two", cloud="local")]
    plan = inv.plan_bulk_run(items, ["k8s:a", "k8s:b"])
    assert plan["kind"] == "k8s"
    assert [t["spec"] for t in plan["targets"]] == [
        {"target_kind": "k8s", "target_id": "a"},
        {"target_kind": "k8s", "target_id": "b"},
    ]


def test_on_prem_cluster_is_selectable():
    """cloud=local is a valid k8s target — it runs on the local runner."""
    plan = inv.plan_bulk_run([_k8s("k8s:a", "onprem", cloud="local")], ["k8s:a"])
    assert plan["targets"][0]["spec"]["target_id"] == "a"


def test_databases_resolve_to_target_ids():
    items = [_db("clouddb:d1", "pg one"), _db("clouddb:d2", "my two", engine="mysql")]
    plan = inv.plan_bulk_run(items, ["clouddb:d1", "clouddb:d2"])
    assert plan["kind"] == "database"
    assert [t["spec"]["target_id"] for t in plan["targets"]] == ["d1", "d2"]


# ── per-row targetability ─────────────────────────────────────────────────────

def test_vm_without_an_ip_is_refused_with_the_group_hint():
    """Proxmox/Nutanix deploys record node/vmid, not an address."""
    items = [_vm("job:1", "pve-01", cloud="proxmox", ip="")]
    _expect_error(items, ["job:1"], "no recorded IP address", "hypervisor group")


def test_one_bad_row_blocks_the_whole_batch():
    """A half-applied fleet change is worse than none, so nothing is enqueued."""
    items = [_vm("job:1", "web-01"), _vm("job:2", "pve-01", cloud="proxmox", ip="")]
    msg = _expect_error(items, ["job:1", "job:2"], "can't be targeted")
    assert "pve-01" in msg
    assert "web-01" not in msg, "only the offending rows should be named"


def test_database_with_an_unsupported_engine_is_refused():
    items = [_db("clouddb:d1", "oracle d1", engine="oracle")]
    _expect_error(items, ["clouddb:d1"], "not supported for Ansible runs")


def test_local_database_is_selectable():
    """A registered on-prem database runs on the local runner, like a kubeconfig-
    registered cluster. This asserted a refusal while every database row was something
    the dashboard had provisioned into a cloud."""
    items = [_db("clouddb:d1", "pg d1", cloud="local")]
    plan = inv.plan_bulk_run(items, ["clouddb:d1"])
    assert plan["kind"] == "database"
    assert [t["spec"]["target_id"] for t in plan["targets"]] == ["d1"]


def test_cluster_in_an_unsupported_cloud_is_refused():
    items = [_k8s("k8s:a", "oke", cloud="oci")]
    _expect_error(items, ["k8s:a"], "no Ansible runner for Kubernetes targets")


def test_vm_on_a_non_key_cloud_still_runs_as_an_adhoc_ip_target():
    """cloud only means something where an SSH key is stored; an OCI VM with an
    address is still a perfectly good ad-hoc target."""
    plan = inv.plan_bulk_run([_vm("job:9", "oci-01", cloud="oci", ip="10.1.1.1")], ["job:9"])
    assert plan["targets"][0]["spec"] == {"target": "10.1.1.1", "cloud": ""}


# ── fan-out protection ────────────────────────────────────────────────────────

def test_empty_selection_is_refused():
    _expect_error([_vm()], [], "No resources selected")


def test_unknown_id_is_refused():
    """The caller passes only RBAC-visible items, so an id outside that set is how a
    request for someone else's resource shows up. It must not resolve."""
    _expect_error([_vm("job:1")], ["job:1", "job:hidden"], "no longer in your inventory")


def test_selection_is_capped():
    items = [_vm(f"job:{n}", f"vm-{n}") for n in range(inv.MAX_BULK_TARGETS + 5)]
    ids = [i["id"] for i in items]
    _expect_error(items, ids, "limit for one bulk run")


def test_duplicate_ids_are_collapsed():
    plan = inv.plan_bulk_run([_vm("job:1")], ["job:1", "job:1", "job:1"])
    assert len(plan["targets"]) == 1, "a repeated id must not enqueue the same job twice"


def test_selection_order_is_preserved():
    items = [_vm("job:1", "a"), _vm("job:2", "b"), _vm("job:3", "c")]
    plan = inv.plan_bulk_run(items, ["job:3", "job:1", "job:2"])
    assert [t["name"] for t in plan["targets"]] == ["c", "a", "b"]


# ── connection-identity fields vs non-VM targets ──────────────────────────────

def test_connection_fields_are_fine_for_vms():
    assert inv.reject_connection_fields("vm", {"managed_account": {"account_name": "svc"},
                                               "secret_ssh_key_source": "bt_safe://k"}) is None


def test_managed_account_is_refused_for_clusters():
    """The run path IGNORES these for a localhost play. One run can absorb that
    silently; a batch would leave the operator believing 50 clusters got a
    credential they never got."""
    msg = inv.reject_connection_fields("k8s", {"managed_account": {"account_name": "svc"}})
    assert msg and "managed_account" in msg and "no" in msg and "SSH connection" in msg


def test_ssh_key_and_become_are_refused_for_databases():
    msg = inv.reject_connection_fields("database", {
        "secret_ssh_key_source": "bt_safe://key", "secret_become_source": "bt_safe://sudo"})
    assert msg and "secret_ssh_key_source" in msg and "secret_become_source" in msg


def test_named_secret_vars_stay_allowed_for_non_vm_kinds():
    """secret_vars is the one secret kind a localhost play does honor, so it must
    not be swept up by this guard."""
    assert inv.reject_connection_fields("k8s", {"secret_vars": {"db_pw": "bt_safe://x"}}) is None


def test_empty_connection_fields_do_not_trip_the_guard():
    assert inv.reject_connection_fields("k8s", {"managed_account": None,
                                                "secret_ssh_key_source": ""}) is None


# ── RBAC helper shared with the inventory listing ─────────────────────────────

def test_accessible_workgroups_admin_is_none():
    """None means "no filter" to visible_to — the admin case."""
    admin = types.SimpleNamespace(is_effective_admin=True, workgroups_list=["x"])
    assert inv.accessible_workgroups(admin) is None


def test_accessible_workgroups_lowercases():
    """visible_to compares against item['workgroup'] verbatim, so the case fold has
    to happen here or a workgroup named 'Hydra' silently matches nothing."""
    user = types.SimpleNamespace(is_effective_admin=False, workgroups_list=["Hydra", "WeaverLab"])
    assert inv.accessible_workgroups(user) == ["hydra", "weaverlab"]


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
