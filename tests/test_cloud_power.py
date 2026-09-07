"""Cloud VMs can be started and suspended, and each cloud's verb is the right one.

Every on-prem hypervisor here has had `/power/*` for its whole life — five of them plus
VMware Workstation. The four clouds had nothing: a VM was deploy-or-destroy, so an
operator who wanted one off overnight used the cloud console, which puts this dashboard's
inventory out of step with reality. The estate profile's only lifecycle lever was the
irreversible one.

The per-cloud verbs are NOT interchangeable, and each cloud has a wrong choice that looks
right and costs money. Those choices are asserted structurally, because the failure mode
is silent — the page reports the fleet asleep and the bill says otherwise:

  * **Azure** must `begin_deallocate`. `begin_power_off` leaves the VM "Stopped" and still
    billing for compute.
  * **GCE** must `stop`, not `suspend`. Suspend preserves RAM to disk and charges for that
    storage plus the reserved resources; stop lands on TERMINATED, where only disks bill.
  * **EC2** must be a plain stop, never Hibernate — hibernation must be enabled at launch
    and silently degrades to a stop where it is not.
  * **OCI** uses SOFTSTOP, which asks the guest before pulling the cord.

Run: python tests/test_cloud_power.py   (or under pytest)
"""
import ast
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="cloud-power-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-cloud-power-tests")

try:
    from web_dashboard.api import aws as api_aws
    from web_dashboard.api import azure as api_azure
    from web_dashboard.api import gcp as api_gcp
    from web_dashboard.api import oci as api_oci
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

CONSOLES = {
    "aws":   (api_aws,   "ec2_power"),
    "azure": (api_azure, "azure_power"),
    "gcp":   (api_gcp,   "gce_power"),
    "oci":   (api_oci,   "oci_power"),
}


def _src(path):
    return open(os.path.join(_ROOT, path), encoding="utf-8").read()


def _fn(path, name):
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path}: no {name!r} — renamed?")


# ── The routes exist, on every cloud ──────────────────────────────────────────

def test_all_four_clouds_expose_start_and_stop():
    for name, (mod, _) in CONSOLES.items():
        paths = {r.path for r in mod.router.routes}
        for suffix in ("/power/start", "/power/stop"):
            assert any(p.endswith(suffix) for p in paths), f"{name} has no {suffix}"


def test_the_identifier_is_in_the_body_not_the_path():
    """Matching every existing /power/* route here — and the only shape that works for
    OCI, whose greedy `:path` OCID converter would swallow a `/power/start` suffix."""
    for name, (mod, _) in CONSOLES.items():
        for r in mod.router.routes:
            if r.path.endswith("/power/start"):
                assert "{" not in r.path, f"{name}: {r.path} takes the id in the path"


# ── The verbs ─────────────────────────────────────────────────────────────────

def test_azure_deallocates_and_never_merely_powers_off():
    # ast.dump, not raw source: the source says "begin_power_off" in the comment that
    # explains why it must not be called, and a text search cannot tell the two apart.
    body = ast.dump(_fn("web_dashboard/services/azure_service.py", "_power_vm_sync"))
    assert "begin_deallocate" in body, "Azure suspend must deallocate"
    assert "begin_power_off" not in body, \
        "begin_power_off leaves the VM billing for compute — it must not be called here"
    assert "begin_start" in body


def test_gce_stops_rather_than_suspends():
    body = ast.dump(_fn("web_dashboard/services/gcp_service.py", "_power_instance_sync"))
    assert ".stop" in body or "'stop'" in body
    assert "suspend" not in body.lower(), \
        "GCE suspend charges for preserved RAM; stop is what a suspend schedule wants"


def test_ec2_does_not_reach_for_hibernate():
    body = ast.dump(_fn("web_dashboard/services/aws_service.py", "_power_instance_sync"))
    assert "stop_instances" in body and "start_instances" in body
    assert "hibernate" not in body.lower(), "Hibernate degrades silently where unsupported"


def test_oci_asks_the_guest_before_pulling_the_cord():
    body = ast.dump(_fn("web_dashboard/services/oci_service.py", "_power_instance_sync"))
    assert "SOFTSTOP" in body, "a hard STOP risks a dirty filesystem on resume"
    assert "START" in body


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_the_job_types_are_handled_tiered_and_dispatched():
    """A handled type in no tier raises KeyError in the supervisor AFTER claiming the
    job, which kills the loop for every job — see tests/test_worker_tiers.py."""
    src = _src("web_dashboard/jobs_worker.py")
    for _, job_type in CONSOLES.values():
        assert f'"{job_type}"' in src, f"{job_type} is not in jobs_worker at all"
    # Light tier: one API call, no local process, no streamed output. Read via AST —
    # splitting the source on ")" truncates at the first paren inside a comment.
    tree = ast.parse(src)
    light = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "LIGHT_TYPES" for t in node.targets):
            light = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    assert light, "LIGHT_TYPES not found"
    for _, job_type in CONSOLES.values():
        assert job_type in light, f"{job_type} is not in LIGHT_TYPES"


def test_every_power_handler_checks_ownership():
    """Same guard destroy uses, and for the same reason: the question is ownership, which
    does not change with the verb. A cloud added later fails here by name."""
    for name, (mod, _) in CONSOLES.items():
        path = f"web_dashboard/api/{'aws' if name == 'aws' else name}.py"
        body = ast.dump(_fn(path, "_power_endpoint"))
        assert "'_assert_can_act'" in body, f"{name}: power skips the ownership check"


def test_power_is_write_not_delete():
    """Stopping a VM changes its state; it does not remove it. Requiring `delete` would
    mean an operator who may not destroy also may not save money."""
    for name, (mod, _) in CONSOLES.items():
        path = f"web_dashboard/api/{name}.py"
        body = ast.dump(_fn(path, "_power_endpoint"))
        assert "'write'" in body, f"{name}: power should require write"
        assert "'delete'" not in body, f"{name}: power should not require delete"


def test_power_is_deliberately_not_behind_admission_control():
    """Destroy is gated; power is not, and that is a decision rather than an oversight.
    A reversible action earns a lighter brake — services/pov_spend.py makes the same
    argument — and a change-freeze that forbade suspending a VM would forbid the cheapest
    thing an operator can do during one. If this ever changes, change it deliberately."""
    for name, (mod, _) in CONSOLES.items():
        body = ast.dump(_fn(f"web_dashboard/api/{name}.py", "_power_endpoint"))
        assert "admission_service" not in body, \
            f"{name}: power reached the admission gate — intended, or accidental?"


def test_destroy_and_power_resolve_the_same_deploy_row():
    """Both go through _find_deploy_job, so they cannot disagree about what counts as an
    active deployment — and therefore cannot disagree about its workgroup."""
    for name in CONSOLES:
        path = f"web_dashboard/api/{name}.py"
        src = _src(path)
        assert "def _find_deploy_job" in src, f"{name}: no shared lookup"
        power = ast.dump(_fn(path, "_power_endpoint"))
        assert "'_find_deploy_job'" in power, f"{name}: power does its own lookup"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
