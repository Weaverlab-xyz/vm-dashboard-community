"""OCI's shared BeyondTrust gateway host, and the three things that followed from not having one.

OCI was the one cloud where the dashboard provisioned no gateway inside the VCN — "bring
your own". That gap reached further than it looked. With nothing in the VCN to broker a
session, `oci_vm_service` could not assume a private address was reachable, so it wired the
PUBLIC one into every jump item; and because an auto-assigned public address does not
survive a stop, `vm_suspend_policy` then had to refuse those instances a suspend schedule.
One missing host, three consequences.

What this file pins:

  * **Parity across every dispatch point.** `jumpoint_host_service` branches on cloud in
    seven places and every one of them ends in a bare `else` that means AWS. A cloud added
    to six of the seven does not fail — it silently runs the AWS path, which looks for an
    ECS cluster that is not there and reports "no gateway" for a reason that has nothing to
    do with the truth. Enumerated rather than listed, so an eighth branch is covered too.
  * **The name collision.** `oci_jumpoint_name` already existed and means the PRA Gateway a
    Shell Jump binds to. The host instance needs its own key; reusing that one would point
    the launcher at a PRA display name and the jump items at an instance. GCP carries the
    same hazard resolved the other way round, so the two cannot be reasoned about by
    analogy.
  * **Default off.** Turning this on creates a billable instance. An upgrade must not.
  * **The address follows the gateway.** Private when there is one to broker it, public
    when there is not — not a flag read twice, but the same fact used in both places.
  * **Teardown ordering.** The destroy marks its row destroyed BEFORE asking whether the
    gateway is idle, because the count has no way to exclude the caller.

Run: python tests/test_oci_gateway.py   (or under pytest)
"""
import ast
import inspect
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="oci-gateway-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-oci-gateway-tests")

try:
    from web_dashboard.config import settings
    from web_dashboard.services import (gateway_service, jumpoint_host_service,
                                        oci_service, vm_suspend_policy)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_JHS = os.path.join(_ROOT, "web_dashboard/services/jumpoint_host_service.py")
CLOUDS = ("aws", "azure", "gcp", "oci")


# ── Parity: every per-cloud branch knows about OCI ────────────────────────────

def test_every_cloud_dispatch_point_handles_oci():
    """The load-bearing one. Each of these functions branches on cloud and ends in a bare
    `else` meaning AWS, so a cloud missing from one does not raise — it quietly runs the
    AWS path. Enumerated from the source rather than listed here, so a new branch is
    covered without anybody remembering to add it."""
    tree = ast.parse(open(_JHS, encoding="utf-8").read())

    dispatchers = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = [a.arg for a in fn.args.args]
        if "cloud" not in args:
            continue
        # A dispatcher is a function that compares `cloud` against a literal.
        compared = {c.value for n in ast.walk(fn) if isinstance(n, ast.Compare)
                    for c in n.comparators
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        if compared & {"gcp", "azure"}:
            dispatchers.append((fn.name, compared))

    assert len(dispatchers) >= 6, f"expected the known dispatch points, found {dispatchers}"
    missing = [name for name, compared in dispatchers if "oci" not in compared]
    assert not missing, (
        f"these dispatch functions fall through to the AWS path for OCI: {missing}")


def test_the_gateway_registry_accepts_oci():
    """`adopt_managed` filters on this tuple; a cloud missing from it means the managed
    gateway is created and then never recorded, so the Gateways page shows nothing and
    reconcile has no row to keep current."""
    assert "oci" in gateway_service.CLOUDS


def test_managed_host_name_is_distinct_per_cloud():
    """Not for safety — each idle teardown only ever touches its own cloud's API, so a
    shared name could not cross-reap. For legibility: these names show up in one PRA
    Gateway list, one Gateways page and one registry table, and two rows reading
    `clouddb-shared-jumpoint` for different clouds is a question an operator should not
    have to answer. OCI's default originally collided with GCP's."""
    names = {c: jumpoint_host_service.managed_host_name(c) for c in CLOUDS}
    assert len(set(names.values())) == len(names), names
    assert names["oci"] == "oci-shared-jumpoint"


# ── The name collision ────────────────────────────────────────────────────────

def test_the_host_name_does_not_read_the_pra_gateway_key():
    """`oci_jumpoint_name` is the PRA Gateway a Shell Jump binds to — it predates this and
    means something else entirely. Reading it here would point the launcher at a PRA
    display name (spaces, uppercase) and the jump items at a compute instance."""
    src = inspect.getsource(jumpoint_host_service._oci_jumpoint_host_name)
    assert "oci_jumpoint_host_name" in src
    assert '"oci_jumpoint_name"' not in src, "that key means the PRA Gateway, not the host"
    # And both keys exist, so the distinction is real rather than aspirational.
    assert hasattr(settings, "oci_jumpoint_name")
    assert hasattr(settings, "oci_jumpoint_host_name")


# ── Default off ───────────────────────────────────────────────────────────────

def test_the_shared_gateway_is_off_by_default():
    """It creates a billable instance. An upgrade must not start one on its own."""
    assert settings.oci_vm_jumpoint_mode == "none"
    assert jumpoint_host_service.oci_shared_gateway_enabled() is False


def test_the_mode_is_read_through_one_predicate():
    """Three things branch on this — whether a deploy ensures a host, whether a destroy
    reaps one, and which address the wire-up targets. Three readers of a config string is
    three chances to spell it differently."""
    for path in ("web_dashboard/services/oci_vm_service.py",):
        src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
        assert "oci_shared_gateway_enabled" in src, path
        assert '"oci_vm_jumpoint_mode"' not in src, \
            f"{path} should ask the predicate, not re-read the key"


# ── The address follows the gateway ───────────────────────────────────────────

def test_the_wired_address_follows_whether_a_gateway_exists():
    """The whole point of the feature. With a gateway in the VCN the private address is
    reachable and is preferred, as on the other three clouds; without one the historical
    public-first order stands, so an install that never turns this on is untouched."""
    src = open(os.path.join(_ROOT, "web_dashboard/services/oci_vm_service.py"),
               encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src))
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
              and f.name == "_run_deploy")

    # The hostname assignment must be conditional on the gateway, not a fixed order.
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "hostname" for t in n.targets)]
    assert len(assigns) == 2, f"expected one hostname per branch, found {len(assigns)}"

    ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)
           and isinstance(n.test, ast.Name) and n.test.id == "gateway_host_id"]
    assert ifs, "the address choice must branch on whether a gateway was ensured"

    # And the gateway must be ensured BEFORE the address is chosen, or the branch reads a
    # variable whose value has not been decided yet.
    ensure = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "ensure_jumpoint_host"]
    assert ensure and ensure[0].lineno < assigns[0].lineno


def test_a_privately_wired_oci_instance_can_carry_a_suspend_schedule():
    """The consequence worth having. `wired_address` is what the policy reads, so an OCI
    instance brokered through a gateway is schedulable even with a public address — which
    is exactly the case the old blanket refusal got wrong."""
    private_via_gateway = {"instance_ocid": "ocid1.instance.oc1..aaa",
                           "private_ip": "10.0.1.5", "public_ip": "203.0.113.7",
                           "wired_address": "10.0.1.5", "bt_tf_state": "{}"}
    assert vm_suspend_policy.schedulable("oci_deploy", private_via_gateway) == (True, "")

    # Same instance without a gateway: wired publicly, still refused.
    public_no_gateway = {**private_via_gateway, "wired_address": "203.0.113.7"}
    assert vm_suspend_policy.schedulable("oci_deploy", public_no_gateway)[0] is False


# ── Reference counting ────────────────────────────────────────────────────────

def test_active_oci_count_ignores_destroyed_rows():
    from web_dashboard.database import Base, Job, SessionLocal, engine
    Base.metadata.create_all(bind=engine)
    import json
    from datetime import datetime

    db = SessionLocal()
    try:
        db.query(Job).delete()
        for jid, meta in (("live-1", {}), ("live-2", {}), ("gone", {"destroyed": True})):
            db.add(Job(id=jid, job_type="oci_deploy", status="completed",
                       created_by="alice", extra_data=json.dumps(meta),
                       created_at=datetime.utcnow()))
        db.commit()
        assert jumpoint_host_service._active_oci_count(db) == 2
    finally:
        db.close()


def test_the_destroy_marks_its_row_before_asking_whether_the_gateway_is_idle():
    """_active_oci_count counts live rows and takes no "exclude me" argument, so reaping
    first lets the row being destroyed count itself — and the gateway is never reclaimed.
    Every other cloud's runner orders it this way for the same reason."""
    src = open(os.path.join(_ROOT, "web_dashboard/services/oci_vm_service.py"),
               encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src))
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
              and f.name == "_run_destroy")

    marked = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Subscript)
                      and isinstance(t.slice, ast.Constant) and t.slice.value == "destroyed"
                      for t in n.targets)]
    reaped = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "teardown_jumpoint_host_if_idle"]
    assert marked, "the destroy must mark its deploy row destroyed"
    assert reaped, "the destroy must release the gateway reference"
    assert marked[0].lineno < reaped[0].lineno, \
        "the row must be marked destroyed BEFORE the idle check counts it"


# ── The launcher ──────────────────────────────────────────────────────────────

def test_the_cloud_init_gives_the_container_what_a_tunnel_needs():
    """A protocol tunnel needs NET_ADMIN, NET_RAW and /dev/net/tun. Without `modprobe tun`
    on a fresh Oracle Linux image the container starts and silently cannot broker — the
    quiet failure this whole path exists to avoid."""
    import base64
    body = base64.b64decode(
        oci_service._jumpoint_cloud_init("beyondtrust/sra-jumpoint:latest", "KEY")).decode()
    assert body.startswith("#cloud-config")
    for needed in ("modprobe tun", "/dev/net/tun", "NET_ADMIN", "NET_RAW",
                   "--privileged", "--restart always"):
        assert needed in body, needed
    assert "DEPLOY_KEY='KEY'" in body, "the deploy key must be quoted for the shell"


def test_a_deploy_key_with_a_quote_cannot_break_out_of_the_shell_command():
    """The key is an opaque token from a secrets backend. It is interpolated into a shell
    command, so a quote in it would otherwise end the string and run the rest."""
    import base64
    body = base64.b64decode(
        oci_service._jumpoint_cloud_init("img", "a'b;rm -rf /")).decode()
    # The cloud-init lines are JSON-encoded, so the payload stays inside one string
    # argument no matter what it contains.
    assert "rm -rf /" in body                      # present, but…
    for line in body.splitlines():
        if "docker run" in line:
            assert line.strip().startswith("- [ sh, -c, \""), line
            assert line.rstrip().endswith("\" ]"), line


def test_the_launcher_reuses_a_live_instance_and_ignores_a_terminated_one():
    """OCI display names are not unique, and a name match on a terminated instance is what
    makes an idempotent launcher report a gateway that is not there."""
    assert "TERMINATED" not in oci_service._LIVE_STATES
    assert "TERMINATING" not in oci_service._LIVE_STATES
    for live in ("RUNNING", "STOPPED", "PROVISIONING"):
        assert live in oci_service._LIVE_STATES


def test_the_gateway_carries_the_dashboards_tags():
    """So unmanaged discovery does not offer it as somebody else's VM, and so the managed
    listings can see it."""
    assert oci_service._JUMPOINT_MANAGED_TAGS.get("managed-by") == "vm-dashboard"
    from web_dashboard.services import unmanaged_vms
    assert unmanaged_vms.is_dashboard_tagged(oci_service._JUMPOINT_MANAGED_TAGS)


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
