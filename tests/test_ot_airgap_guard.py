"""The OT cell's AWS air-gap preflight (services/ot_service.aws_airgap_problem).

The cell's demo rests on one claim: the "plant" has no path in except PRA. GCE and
Azure let a deploy pin the external IP off per instance, and the OT forms do exactly
that. EC2 has no such switch — the subnet's MapPublicIpOnLaunch decides — so on AWS
the claim was upheld only by the operator picking the right subnet, with a docs note
and a troubleshooting row for when they didn't. A cell that quietly came up
internet-addressable still *looked* like a successful deploy, which is the worst
shape a failure can take: the demo asserts something untrue and nobody is told.

These pin the guard's three answers (private / public / can't tell) and that it runs
before anything is launched.

Run: python tests/test_ot_airgap_guard.py   (or under pytest)
"""
import asyncio
import ast
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")

_PKG = "ot_airgap_pkg"


def _load(*, public, require_private=True):
    """Exec ot_service inside a throwaway package whose ``aws_service`` and
    ``config_service`` are stubs, so the guard's lazy ``from . import`` resolves
    without dragging in boto3 or the app config.

    ``public`` is what the subnet read returns: True (auto-assigns), False (private),
    or None (could not tell).
    """
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = []
    sys.modules[_PKG] = pkg

    cfg = types.ModuleType(f"{_PKG}.config_service")
    cfg.get_bool = lambda key, default=False: (
        require_private if key == "ot_aws_require_private_subnet" else default)
    cfg.get = lambda key, default="": default
    sys.modules[f"{_PKG}.config_service"] = cfg

    aws = types.ModuleType(f"{_PKG}.aws_service")
    calls = []

    async def _subnet_public(region, subnet_id):
        calls.append((region, subnet_id))
        return public

    aws.subnet_auto_assigns_public_ips = _subnet_public
    sys.modules[f"{_PKG}.aws_service"] = aws

    spec = importlib.util.spec_from_file_location(f"{_PKG}.ot_service", _SVC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.ot_service"] = mod
    spec.loader.exec_module(mod)
    mod._test_subnet_calls = calls
    return mod


def test_a_private_subnet_passes():
    ot = _load(public=False)
    assert asyncio.run(ot.aws_airgap_problem("us-east-1", "subnet-private")) == ""


def test_a_public_subnet_is_refused_with_the_remedy():
    ot = _load(public=True)
    msg = asyncio.run(ot.aws_airgap_problem("us-east-1", "subnet-0abc"))
    assert msg, "a subnet that auto-assigns public IPs must be refused"
    # The failed-job page renders error_message and NOTHING else, so everything the
    # operator needs to act has to be in this string: which subnet, why it matters,
    # what to do instead, the escape hatch, and that nothing was created.
    for needle in ("subnet-0abc", "MapPublicIpOnLaunch", "private sandbox subnet",
                   "Settings → Integrations → Privileged Remote Access",
                   "No instance was launched"):
        assert needle in msg, f"remedy lost {needle!r}: {msg}"


def test_an_unreadable_subnet_is_not_a_refusal():
    # None means "could not tell" — a transient DescribeSubnets failure must not
    # masquerade as a misconfigured subnet, or an AWS blip blocks every deploy.
    ot = _load(public=None)
    assert asyncio.run(ot.aws_airgap_problem("us-east-1", "subnet-0abc")) == ""


def test_the_kill_switch_skips_the_read_entirely():
    ot = _load(public=True, require_private=False)
    assert asyncio.run(ot.aws_airgap_problem("us-east-1", "subnet-0abc")) == ""
    assert ot._test_subnet_calls == [], (
        "with the guard off, the deploy should not spend a DescribeSubnets call")


def test_a_blank_subnet_is_not_a_refusal():
    # The child's metadata carries whatever the form sent; a blank subnet means the
    # deploy falls back to the account default, which this guard cannot resolve.
    ot = _load(public=True)
    assert asyncio.run(ot.aws_airgap_problem("us-east-1", "")) == ""
    assert ot._test_subnet_calls == []


def _run_cell_deploy_ast():
    tree = ast.parse(open(_SVC, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_cell_deploy":
            return node
    raise AssertionError("ot_service.run_cell_deploy not found")


def test_the_guard_runs_before_the_vm_is_launched():
    fn = _run_cell_deploy_ast()
    guard_line = launch_line = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "aws_airgap_problem":
            guard_line = node.lineno
        elif name == "run" and getattr(node.func, "value", None) is not None \
                and getattr(node.func.value, "id", "") == "vm_service":
            launch_line = node.lineno
    assert guard_line, "run_cell_deploy never calls aws_airgap_problem"
    assert launch_line, "run_cell_deploy no longer drives the child through vm_service.run"
    assert guard_line < launch_line, (
        "the air-gap guard must refuse BEFORE the VM is launched — refusing after "
        "leaves a public-IP cell running that someone has to notice and destroy")


def test_the_refusal_cancels_the_queued_child():
    # A queued child nothing will drive again must be cancelled, not abandoned: the
    # reconciler skips queued rows by design, so an abandoned one sits forever.
    src = ast.get_source_segment(open(_SVC, encoding="utf-8").read(), _run_cell_deploy_ast())
    airgap = src.split("aws_airgap_problem", 1)[1]
    tail = airgap.split("vm_service.run", 1)[0]
    assert "set_cancelled" in tail and "set_failed" in tail, (
        "the air-gap refusal must cancel the child job and fail the parent, the way "
        "the gateway sizing refusal beside it does")


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
