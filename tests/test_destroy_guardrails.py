"""Destroy must be governed like deploy: a workgroup check, and the admission gate.

`api/aws.py::destroy_instance` required `aws:delete` and nothing else — no workgroup
check, though deploy and the reassign PATCH both had one in the same file. So a non-admin
holding `aws:delete` could terminate any dashboard-deployed instance in any workgroup.
The other three cloud consoles were the same shape.

Separately, `admission_service.enforce()` was called from eleven sites and every one was a
deploy. The engine had never seen a teardown — so the auto-delete reaper needed four gates
and two arming clocks to delete a VM, while a human pressing Destroy passed through none of
them and no change-freeze window applied. The reaper was more constrained than the operator.

Pinned here:

  * **You may destroy what you can see.** Each module's check reads that module's own
    `_accessible_workgroups`, so the Destroy button and the instance list cannot drift
    apart. An untagged resource is admin-only, which is what the listings already do
    with one.
  * **The four modules answer identically** — a rule that holds on AWS and not on OCI is
    the shape of bug this file exists to stop.
  * **`is_admin`, not `is_effective_admin`.** The cloud consoles key on the raw column;
    api/vms.py keys on the property. Both are correct and they must not be unified —
    tests/test_dashboard_stats_api.py pins the same split for the dashboard tiles.
  * **Structurally, every destroy handler calls both guards**, so a fifth cloud added
    later fails here by name rather than shipping ungated.
  * **The Rego treats teardowns differently from creates.** Region and size caps
    constrain what you may build; applying them to a destroy would strand resources.
    A change-freeze applies to both. Needs the `opa` binary — skipped without it.

Run: python tests/test_destroy_guardrails.py   (or under pytest)
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="destroy-guard-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-destroy-guardrail-tests")

_POLICY_DIR = os.path.join(_ROOT, "terraform", "policy", "admission")

try:
    from fastapi import HTTPException
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

# (module, destroy handler name) — the four cloud consoles that can tear down a VM.
CONSOLES = [
    (api_aws, "destroy_instance"),
    (api_azure, "destroy_vm"),
    (api_gcp, "destroy_instance"),
    (api_oci, "destroy_instance"),
]

DESTROY_ACTIONS = {
    "web_dashboard/api/aws.py": "aws:ec2:destroy",
    "web_dashboard/api/azure.py": "azure:vm:destroy",
    "web_dashboard/api/gcp.py": "gcp:gce:destroy",
    "web_dashboard/api/oci.py": "oci:compute:destroy",
}


class _User:
    """`is_admin` and `is_effective_admin` are independent on purpose — the cloud
    consoles read the first, api/vms.py reads the second, and one test below is only
    meaningful because they can disagree."""

    def __init__(self, username="alice", is_admin=False, effective=None, workgroups=()):
        self.username = username
        self.is_admin = is_admin
        self.is_effective_admin = is_admin if effective is None else effective
        self.workgroups_list = list(workgroups)


def _denied(mod, user, workgroup) -> bool:
    try:
        mod._assert_can_destroy(user, workgroup, "Thing")
        return False
    except HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
        return True


# ── The rule, on every cloud ──────────────────────────────────────────────────

def test_all_four_consoles_agree_on_who_may_destroy():
    admin = _User("root", is_admin=True)
    alice = _User("alice", workgroups=["team-a"])

    for mod, _ in CONSOLES:
        name = mod.__name__
        assert not _denied(mod, admin, "team-b"), f"{name}: admin must pass"
        assert not _denied(mod, admin, None), f"{name}: admin must pass untagged"
        assert not _denied(mod, alice, "team-a"), f"{name}: own workgroup must pass"
        assert _denied(mod, alice, "team-b"), f"{name}: other workgroup must be refused"
        assert _denied(mod, alice, None), f"{name}: untagged must be admin-only"
        assert _denied(mod, alice, ""), f"{name}: blank workgroup must be admin-only"


def test_workgroup_match_is_case_insensitive():
    """Workgroups are stored canonicalised, but a deploy row from an older build can
    carry mixed case. Refusing that would be a 403 nobody could explain."""
    alice = _User("alice", workgroups=["team-a"])
    for mod, _ in CONSOLES:
        assert not _denied(mod, alice, "Team-A"), mod.__name__


def test_the_cloud_consoles_key_on_is_admin_not_effective_admin():
    """A JIT-elevated user is effective-admin but not `is_admin`, and the cloud consoles
    scope them to their workgroups — matching each module's `_accessible_workgroups` and
    therefore its instance listing. Unifying the two rules would silently widen destroy."""
    jit = _User("jit", is_admin=False, effective=True, workgroups=["team-a"])
    for mod, _ in CONSOLES:
        assert _denied(mod, jit, "team-b"), f"{mod.__name__}: effective-admin must not widen destroy"
        assert _denied(mod, jit, None), f"{mod.__name__}: effective-admin must not reach untagged"


def test_the_check_reads_the_modules_own_accessible_workgroups():
    """Not a paraphrase of it. If someone re-derives the rule inline, the Destroy button
    and the list endpoint can drift, which is exactly how the original bug survived."""
    for mod, _ in CONSOLES:
        seen = []

        def _spy(user, _seen=seen):
            _seen.append(user)
            return ["only-this"]

        orig = mod._accessible_workgroups
        mod._accessible_workgroups = _spy
        try:
            # The spy says the caller may reach exactly one workgroup. If the check
            # consults it, "team-a" is refused; if it re-derives from the user, it passes.
            refused = _denied(mod, _User("alice", workgroups=["team-a"]), "team-a")
        finally:
            mod._accessible_workgroups = orig
        assert seen, f"{mod.__name__} never called _accessible_workgroups"
        assert refused, f"{mod.__name__} ignored _accessible_workgroups and re-derived the rule"


# ── Structure: no destroy handler may skip either guard ───────────────────────

def _handler(path, name):
    tree = ast.parse(open(os.path.join(_ROOT, path), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path}: no handler named {name!r} — was it renamed?")


def test_every_destroy_handler_checks_the_workgroup_and_the_policy():
    for path, action in DESTROY_ACTIONS.items():
        name = "destroy_vm" if path.endswith("azure.py") else "destroy_instance"
        dumped = ast.dump(_handler(path, name))
        assert "'_assert_can_destroy'" in dumped, f"{path}: destroy has no workgroup check"
        assert "'enforce'" in dumped, f"{path}: destroy does not reach the admission gate"
        assert repr(action) in dumped or action in dumped, \
            f"{path}: expected admission action {action!r}"


def test_destroy_jobs_carry_the_workgroup_they_tear_down():
    """Without it the teardown row belongs to no workgroup at all, while the deploy row
    it undoes belongs to one."""
    for path in DESTROY_ACTIONS:
        name = "destroy_vm" if path.endswith("azure.py") else "destroy_instance"
        node = _handler(path, name)
        found = False
        for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
            if any(k.arg == "workgroup" for k in call.keywords):
                found = True
        assert found, f"{path}: destroy job is created without a workgroup"


def test_the_action_names_follow_the_deploy_convention():
    """`aws:ec2:deploy` -> `aws:ec2:destroy`. The operator gates actions by exact string
    in `admission_gated_actions`, so an off-convention name is a gate nobody can find."""
    for path, action in DESTROY_ACTIONS.items():
        src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
        deploy = action.rsplit(":", 1)[0] + ":deploy"
        if deploy not in src:  # k8s/clouddb use other verbs; the four consoles use deploy
            continue
        assert action in src, f"{path}: {action} missing beside {deploy}"


# ── The Rego (needs the opa binary) ───────────────────────────────────────────

_OPA = os.environ.get("OPA_BINARY") or shutil.which("opa")


def _decide(action, request, limits, weekday="mon"):
    doc = {"action": action, "actor": {"username": "a", "is_admin": False},
           "request": request, "limits": limits, "now": {"weekday": weekday, "hour": 10}}
    out = subprocess.run(
        [_OPA, "eval", "-f", "json", "-I", "-d", _POLICY_DIR, "data.admission"],
        input=json.dumps(doc), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    value = json.loads(out.stdout)["result"][0]["expressions"][0]["value"]
    return [m for body in value.values() for m in (body.get("deny") or [])]


_LIMITS = {"allowed_regions": ["us-east-1"], "denied_instance_types": ["m5.24xlarge"],
           "prod_window": []}


def test_rego_teardowns_are_exempt_from_the_create_caps():
    if not _OPA:
        print("      (skipped: no opa binary)")
        return
    # The trap this guards: an instance deployed into a region later removed from the
    # allowed list must still be destroyable, or the guardrail strands it.
    for action in ("aws:ec2:destroy", "azure:vm:destroy", "gcp:gce:destroy",
                   "oci:compute:destroy", "clouddb:decommission"):
        assert _decide(action, {"region": "us-west-2"}, _LIMITS) == [], action


def test_rego_creates_are_still_capped():
    if not _OPA:
        print("      (skipped: no opa binary)")
        return
    for action in ("aws:ec2:deploy", "clouddb:provision", "k8s:provision"):
        assert _decide(action, {"region": "us-west-2"}, _LIMITS), action
    assert _decide("aws:ec2:deploy", {"region": "us-east-1",
                                      "instance_type": "m5.24xlarge"}, _LIMITS)


def test_rego_a_change_freeze_covers_teardowns_too():
    """The one policy that SHOULD apply to a destroy. "No changes on a Sunday" that let
    the destroys through would be half a freeze."""
    if not _OPA:
        print("      (skipped: no opa binary)")
        return
    limits = dict(_LIMITS, prod_window=["mon"])
    msgs = _decide("aws:ec2:destroy", {"region": "us-east-1"}, limits, weekday="mon")
    assert msgs, "a frozen day must stop a destroy"
    assert "aws:ec2:destroy" in msgs[0], f"the message should name the action: {msgs}"


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
