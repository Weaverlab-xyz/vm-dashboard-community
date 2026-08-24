"""Cross-file agreements the OT cell's teardown depends on.

The cell has no teardown path of its own — by design. Its Web Jump and tunnel
state are written onto the ``gce_deploy`` child's metadata, and
``gcp_vm_service._run_destroy`` (reached by BOTH the Destroy button and the
expiry reaper) is extended to remove them. That is a cross-file agreement a
refactor can silently break in either direction, so it is pinned statically —
the same reasoning as tests/test_worker_dispatch.py.

Run: python tests/test_ot_destroy_cleanup.py   (or under pytest)
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GCP_VM = os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py")
_JHS = os.path.join(_ROOT, "web_dashboard", "services", "jumpoint_host_service.py")
_OT = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")


def _fn_src(path, name):
    src = open(path, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


def test_the_gce_destroy_removes_the_ot_wiring():
    src = _fn_src(_GCP_VM, "_run_destroy")
    for needle in ("ot_web_jump_tf_state", "remove_web_jump",
                   "ot_tunnel_tf_state", "remove_api_tunnel"):
        assert needle in src, (
            f"_run_destroy lost its {needle!r} handling — destroying (or expiring) "
            "an OT cell would orphan its PRA jump items")


def test_the_wiring_writes_the_keys_the_destroy_reads():
    src = _fn_src(_OT, "_wire_cell")
    for needle in ("ot_web_jump_tf_state", "ot_tunnel_tf_state"):
        assert needle in src, (
            f"_wire_cell no longer persists {needle!r} onto the child job — the "
            "destroy path reads exactly that key")
    assert "update_metadata" in src, (
        "_wire_cell must persist each artifact onto the CHILD's metadata the "
        "moment it exists, or a partial failure leaves untracked PRA items")


def test_standalone_tunnels_hold_a_gateway_reference():
    src = _fn_src(_JHS, "_teardown_jumpoint_host_if_idle_gcp")
    assert "_active_ot_tunnel_count" in src, (
        "the GCP idle-gateway sum lost its OT tunnel term — a cloud-database "
        "decommission could reap the gateway from under a live OT tunnel")


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
