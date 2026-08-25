"""Cross-file agreements the OT cell's teardown depends on.

The cell has no teardown path of its own — by design. Its Web Jump and tunnel
state are written onto the VM-deploy child's metadata, and every cloud's
``_run_destroy`` (reached by BOTH the Destroy button and the expiry reaper) is
extended to remove them. That is a cross-file agreement a refactor can silently
break in either direction — and a NEW cloud can simply forget its half — so it
is pinned statically per cloud, the same reasoning as tests/test_worker_dispatch.py.

Run: python tests/test_ot_destroy_cleanup.py   (or under pytest)
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GCP_VM = os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py")
_AWS_VM = os.path.join(_ROOT, "web_dashboard", "services", "aws_vm_service.py")
_AZURE_VM = os.path.join(_ROOT, "web_dashboard", "services", "azure_vm_service.py")
_JHS = os.path.join(_ROOT, "web_dashboard", "services", "jumpoint_host_service.py")
_OT = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")

# Every cloud that can host a cell (ot_service.CELL_CHILD_JOB_TYPE) and its vm
# service module. Extending the cell to a new cloud means adding it here too —
# that is the point.
_CELL_VM_SERVICES = {"gcp": _GCP_VM, "aws": _AWS_VM, "azure": _AZURE_VM}


def _fn_src(path, name):
    src = open(path, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


def test_every_cell_cloud_destroy_removes_the_ot_wiring():
    for cloud, path in _CELL_VM_SERVICES.items():
        src = _fn_src(path, "_run_destroy")
        for needle in ("ot_web_jump_tf_state", "remove_web_jump",
                       "ot_tunnel_tf_state", "remove_api_tunnel",
                       "ot_ps_mirror_tf_state", "unlink_synced_account",
                       "ot_vault_tf_state", "remove_vault_account"):
            assert needle in src, (
                f"{cloud}: _run_destroy lost its {needle!r} handling — destroying "
                "(or expiring) an OT cell would orphan its PRA jump items or its "
                "PRA-checkout pair")
        # The mirror + vault teardown must run BEFORE ps_vm_hook.deregister removes
        # the parent adminuser account: the SyncedAccounts unlink needs both account
        # ids, and off-boarding the parent first would strand a dangling link.
        assert src.index("ot_ps_mirror_tf_state") < src.index("ps_registration_tf_state"), (
            f"{cloud}: the PRA-checkout teardown moved below the Password Safe "
            "deregistration — the unlink would then run against an already-removed "
            "parent account")


def test_the_gce_destroy_removes_the_ot_wiring():
    """Kept under its original name so a bisect over the GCP-only slice still finds
    it; the per-cloud sweep above is the real rule now."""
    src = _fn_src(_GCP_VM, "_run_destroy")
    assert "ot_web_jump_tf_state" in src


def test_the_orchestrator_covers_exactly_the_swept_clouds():
    """The dispatch table in ot_service and the sweep above must name the same
    clouds — a cloud added to one but not the other either can't deploy cells or
    deploys cells whose teardown nobody checks."""
    src = open(_OT, encoding="utf-8").read()
    tree = ast.parse(src)
    table = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "CELL_CHILD_JOB_TYPE" for t in node.targets)):
            table = {k.value for k in node.value.keys}
    assert table is not None, "CELL_CHILD_JOB_TYPE vanished from ot_service"
    assert table == set(_CELL_VM_SERVICES), (
        f"ot_service supports {sorted(table)} but this sweep checks "
        f"{sorted(_CELL_VM_SERVICES)} — add the new cloud's destroy path here")


def test_the_wiring_writes_the_keys_the_destroy_reads():
    src = _fn_src(_OT, "_wire_cell")
    for needle in ("ot_web_jump_tf_state", "ot_tunnel_tf_state"):
        assert needle in src, (
            f"_wire_cell no longer persists {needle!r} onto the child job — the "
            "destroy path reads exactly that key")
    assert "update_metadata" in src, (
        "_wire_cell must persist each artifact onto the CHILD's metadata the "
        "moment it exists, or a partial failure leaves untracked PRA items")


def test_the_checkout_wiring_writes_the_keys_the_destroy_reads():
    src = _fn_src(_OT, "_wire_ps_checkout")
    for needle in ("ot_vault_tf_state", "ot_ps_mirror_tf_state",
                   "ot_ps_mirror_account_id", "ot_ps_synced", "update_metadata"):
        assert needle in src, (
            f"_wire_ps_checkout no longer persists {needle!r} onto the child job — "
            "the destroy path and the rewire idempotency both key off exactly it")


def test_standalone_tunnels_hold_a_gateway_reference():
    """Standalone tunnels are per-cloud now (the meta rows carry `cloud`), so every
    cloud's idle-gateway sum needs its own term — losing one means a cloud-database
    decommission could reap that gateway from under a live OT tunnel."""
    for fn in ("_teardown_jumpoint_host_if_idle_gcp",
               "_teardown_jumpoint_host_if_idle_aws",
               "_teardown_jumpoint_host_if_idle_azure"):
        src = _fn_src(_JHS, fn)
        assert "_active_ot_tunnel_count" in src, (
            f"{fn} lost its OT tunnel term — a cloud-database decommission could "
            "reap the gateway from under a live OT tunnel")


def test_the_cell_parent_is_a_first_class_worker_type():
    src = open(_WORKER, encoding="utf-8").read()
    assert '"ot_cell_deploy"' in src, "ot_cell_deploy vanished from jobs_worker"
    assert "run_cell_deploy" in src, "the dispatch branch no longer calls ot_service"


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
