"""One Hyper-V power button, two implementations — they must not drift.

"Force Off" on the Hyper-V page can reach the host two ways: directly over WinRM
(`web_dashboard/services/hyperv_service.py::_POWER_OPS_PS`, op `stop`) or, for an
agent-bound connection, through the sibling runner
(`runners/hypervisor/run.py::_PS_POWER`, verb `power_off`). Two PowerShell strings in two
repos-worth of code behind one red button is exactly the shape that drifts.

The bug this file is named for: `stop` was `Stop-VM -VM $vm -Force`. In Hyper-V
PowerShell `-Force` only suppresses the confirmation prompt — **-TurnOff** is the switch
that makes it a hard power cut. So `stop` and `shutdown` issued the *same* graceful,
Integration-Services-dependent shutdown, and the button labelled "Force Off (hard stop)"
did nothing at all on a VM whose guest could not answer. That is precisely the state an
operator reaches for Force Off in. Nothing errored: `Stop-VM` returns cleanly after
asking a guest that will never respond, so the job went green and the VM stayed Running.
The runner's `power_off` had `-TurnOff` all along.

These are text assertions on the script strings, not executions: running either one needs
a Windows host, a WinRM listener and a real VM. What can be checked here is that both
strings still say the thing the button promises — and that `shutdown`, the graceful op
the UI disables when Integration Services are absent, has *not* quietly acquired
-TurnOff, which would make the two buttons identical again from the other direction.

Runs under pytest, or standalone:  python tests/test_hyperv_power_parity.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICE = os.path.join(_ROOT, "web_dashboard", "services", "hyperv_service.py")
_RUNNER = os.path.join(_ROOT, "runners", "hypervisor", "run.py")


def _dict_literal(path, name):
    """The value of a module-level `name = {...}` of string constants.

    AST rather than import: `runners/hypervisor/run.py` is a standalone script and
    `hyperv_service` sits inside the web package, so importing either drags in
    dependencies this assertion does not need.
    """
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                out = ast.literal_eval(node.value)
                assert isinstance(out, dict) and out, f"{name} in {path} is not a dict"
                return out
    raise AssertionError(f"{name} not found in {path} — was it renamed?")


def _service_ops():
    return _dict_literal(_SERVICE, "_POWER_OPS_PS")


def _runner_ops():
    return _dict_literal(_RUNNER, "_PS_POWER")


# ── the regression ────────────────────────────────────────────────────────────

def test_the_direct_stop_is_a_hard_power_cut():
    """`stop` is the page's "Force Off". -Force alone is not one."""
    script = _service_ops()["stop"]
    assert "-TurnOff" in script, (
        "hyperv_service `stop` lost -TurnOff. Without it this is a graceful, "
        "Integration-Services-dependent shutdown that does nothing on a wedged guest, "
        'while the page still labels the button "Force Off (hard stop)".')


def test_the_runner_power_off_is_a_hard_power_cut():
    """The agent-bound half of the same button."""
    script = _runner_ops()["power_off"]
    assert "-TurnOff" in script, (
        "the hypervisor runner's `power_off` lost -TurnOff, so an agent-bound Force Off "
        "no longer matches the direct path.")


def test_both_implementations_of_force_off_agree():
    """The assertion this file exists for: one button, one behaviour, either route.

    Compared as a set of switches rather than as text, because the two call forms
    legitimately differ — the service resolves a VM object first (`-VM $vm`), the runner
    addresses the VM by id (`-Id '{vm}'`).
    """
    direct = _service_ops()["stop"]
    agent = _runner_ops()["power_off"]
    for switch in ("-TurnOff", "-Force"):
        assert switch in direct and switch in agent, (
            f"{switch} is on only one of the two Force Off paths: "
            f"direct={direct!r} agent={agent!r}")
    assert "Stop-VM" in direct and "Stop-VM" in agent


# ── the graceful op must stay graceful ────────────────────────────────────────

def test_graceful_shutdown_is_not_a_hard_power_cut():
    """The other direction of the same drift.

    The UI disables `shutdown` when Integration Services are absent and promises a guest
    OS shutdown. -TurnOff here would silently turn that button into a plug-pull.
    """
    script = _service_ops()["shutdown"]
    assert "-TurnOff" not in script, (
        "hyperv_service `shutdown` gained -TurnOff — the UI presents this as a graceful "
        "guest shutdown via Integration Services, not a power cut.")


def test_stop_and_shutdown_are_actually_different_operations():
    """They were identical once. Two buttons that do the same thing is the bug."""
    ops = _service_ops()
    assert ops["stop"] != ops["shutdown"], (
        "`stop` and `shutdown` are the same script again — the page shows them as "
        '"Force Off" and "Shutdown" and disables only the latter without '
        "Integration Services.")


# ── the map stays a closed map ────────────────────────────────────────────────

def test_force_off_is_reachable_under_the_name_the_router_uses():
    """api/hyperv.py registers /power/stop and passes op="stop"; the runner is dispatched
    on verb "power_off". A rename on either side is a 502 or a refusal, not a test
    failure, so pin the keys."""
    assert "stop" in _service_ops()
    assert "power_off" in _runner_ops()


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
