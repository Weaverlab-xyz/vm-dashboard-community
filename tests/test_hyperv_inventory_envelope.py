"""An empty Hyper-V host and an unreadable one must not look the same.

`Get-VM | ConvertTo-Json` prints NOTHING for an empty result — PowerShell pipes nothing
into ConvertTo-Json — and it prints nothing for a host whose Hyper-V module never
loaded, and nothing for a service account that cannot enumerate VMs. All three reached
the dashboard as a successful scan of zero VMs, and on the dashboard a zero-VM pass
prunes the connection's entire cached inventory and stamps it as synced. A Hyper-V page
that had been listing rows went to "No VMs" with no error anywhere and the cache gone.
Confirmed live.

The runner therefore returns an ENVELOPE: an empty host is a positive statement
(`{"enumerated": true, "count": 0, "vms": []}`) and empty stdout is a refusal carrying a
reason. `enumerated` is what the dashboard's guard keys on — see
``hypervisor_sync_service._empty_prune_refusal`` and the scenario in
tests/test_hypervisor_sync.py, which pins the other half: without that flag an empty
page may not empty a populated cache, and with it a genuinely empty host still can.

The sweep at the bottom is the part that ages well. Five more inventory producers live
in the agent itself, and one forgetting `enumerated` does not fail anywhere visible —
it reads as a connection whose rows never go away.

No WinRM and no Windows: the session is a stub, and the sweep is AST.

Runs under pytest, or standalone:  python tests/test_hyperv_inventory_envelope.py
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNNER = os.path.join(_ROOT, "runners", "hypervisor", "run.py")
_AGENT = os.path.join(_ROOT, "runners", "agent", "agent.py")

try:
    _spec = importlib.util.spec_from_file_location("hv_runner_env", _RUNNER)
    runner = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(runner)
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)


# ── A stubbed WinRM session ───────────────────────────────────────────────────

class _FakeResult:
    """What pywinrm hands back, including its own type quirk.

    ``Session.run_ps`` REPLACES ``std_err`` with a cleaned *str* when it is non-empty and
    leaves it as *bytes* when it is not, so a caller that assumes bytes raises
    AttributeError on exactly the path meant to report the error. The default here is
    the real thing: a str when there is a message.
    """

    def __init__(self, std_out=b"", std_err=b"", status_code=0):
        self.std_out = std_out
        self.std_err = std_err
        self.status_code = status_code


class _FakeSession:
    def __init__(self, result):
        self.result = result
        self.scripts = []

    def run_ps(self, script):
        self.scripts.append(script)
        return self.result


def _scan(std_out=b"", std_err=b"", status_code=0):
    """Run the runner's inventory path against a stubbed session.

    Returns ``(page, "")`` or ``(None, reason)``. A refusal is a printed JSON object and
    a non-zero exit, never an exception — that is the contract the agent reads, in
    ``runners/agent/agent.py::_run_sibling``.
    """
    fake = types.ModuleType("winrm")
    session = _FakeSession(_FakeResult(std_out, std_err, status_code))
    fake.Session = lambda **kwargs: session
    sys.modules["winrm"] = fake
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            page = runner._hyperv("inventory_sync", "host", 5985, "u", "p", False, "")
        return page, ""
    except SystemExit:
        doc = json.loads(printed.getvalue().strip().splitlines()[-1])
        assert doc["ok"] is False, "a refusal must say ok: false"
        return None, doc["error"]
    finally:
        sys.modules.pop("winrm", None)


def _envelope(*vms, count=None):
    return json.dumps({"enumerated": True,
                       "count": len(vms) if count is None else count,
                       "vms": list(vms)}).encode()


_VM_ID = "5b2e9b0f-1c44-4a2e-9d61-7e0c1f2a3b4d"
_VM = {"Id": _VM_ID, "Name": "web01", "State": 2, "ProcessorCount": 4,
       "MemoryMB": 8192}


# ── Reading the two runner files ─────────────────────────────────────────────

def _inventory_pages(path, wanted):
    """Every ``return {...}`` that looks like an inventory page, by function.

    AST rather than an import: both files are standalone scripts whose dependencies
    (winrm, pyVmomi, xmlrpc over TLS) are not installed here.
    """
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    out = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and wanted(n.name)]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
                continue
            pairs = {k.value: v for k, v in zip(node.value.keys, node.value.values)
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "vms" in pairs:
                out.append((fn.name, pairs))
    return out


def _const(path, name):
    """The value of a module-level ``name = <literal>``."""
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path} — was it renamed?")


# ── The ambiguity itself ──────────────────────────────────────────────────────

def test_no_output_at_all_is_a_refusal_not_a_host_with_no_vms():
    """The whole bug, in one assertion.

    This used to return a clean, successful page of zero VMs, which the dashboard
    applied by deleting every cached row for the connection.
    """
    page, reason = _scan(std_out=b"")
    assert page is None, "empty stdout was reported as a successful scan of zero VMs"
    assert "no output" in reason and "envelope" in reason
    assert "Hyper-V PowerShell module" in reason, (
        "the reason has to name the likely cause: the operator is looking at a job that "
        "failed on a host they believe is fine")


def test_a_host_with_no_vms_says_so_and_is_believed():
    """The other half. An empty host must still be able to reach an empty list, or the
    prune can never delete the last VM off a host."""
    page, reason = _scan(std_out=_envelope())
    assert reason == ""
    assert page["vms"] == [] and page["scanned"] == 0
    assert page["enumerated"] is True, (
        "a confirmed empty host that does not say so can never prune a cache to zero")


def test_a_populated_host_is_parsed_and_vouched_for():
    page, _ = _scan(std_out=_envelope(_VM))
    assert page["enumerated"] is True and page["complete"] is True
    assert page["next_cursor"] == "", "Hyper-V has no cursor"
    vm = page["vms"][0]
    assert vm["vm_id"] == _VM_ID and vm["name"] == "web01"
    assert vm["power_state"] == "Running", "State 2 is Running"
    assert vm["vcpus"] == 4 and vm["mem_mib"] == 8192


def test_a_single_vm_survives_powershells_array_unwrapping():
    """PS 5.1's ConvertTo-Json serialises a one-element array as a bare object. The
    envelope should keep it wrapped, but the parser must not depend on that — a host
    with exactly one VM is not the place to find out."""
    raw = json.dumps({"enumerated": True, "count": 1, "vms": _VM}).encode()
    page, _ = _scan(std_out=raw)
    assert len(page["vms"]) == 1 and page["vms"][0]["name"] == "web01"


def test_output_that_is_not_the_envelope_is_a_refusal():
    """Neither of these may fall through to "this host has no VMs"."""
    page, reason = _scan(std_out=b"Get-VM : The term is not recognized")
    assert page is None and "not JSON" in reason

    page, reason = _scan(std_out=b'[{"Id": "a"}]')
    assert page is None and "unexpected shape" in reason, (
        "a bare list is the OLD output shape — accepting it would accept the old empty "
        "case with it, since `[]` and no output are both empty there")


def test_a_powershell_failure_reports_what_powershell_said():
    """pywinrm hands back a cleaned *str* here, not bytes. Decoding it raised
    AttributeError inside the failure path, so the runner died with a traceback and the
    agent reported "unparseable output" — the wrong cause, at the moment the right one
    had already been written down."""
    page, reason = _scan(std_err="The Hyper-V module could not be loaded", status_code=1)
    assert page is None
    assert "Hyper-V module could not be loaded" in reason


def test_the_scan_script_cannot_answer_with_silence():
    """Text assertions on the PowerShell, because running it needs a Windows host with a
    WinRM listener. What can be checked here is that it still returns an envelope rather
    than a piped ConvertTo-Json, which is the difference the tests above rely on."""
    script = _const(_RUNNER, "_PS_LIST")
    assert "-InputObject" in script and "enumerated=$true" in script, (
        "the Hyper-V scan no longer builds an envelope — a piped ConvertTo-Json emits "
        "nothing at all for an empty result, which is exactly the ambiguity this fixed")
    assert "$ErrorActionPreference = 'Stop'" in script, (
        "without this a failing Import-Module is a warning and a blank pipe, which "
        "reads here as a host with no VMs")
    assert "Import-Module Hyper-V" in script
    assert "Depth 4" in script, (
        "the envelope nests the VM objects one level deeper than the piped form did, "
        "so the depth has to go up with it or they serialise as type names")


# ── Every producer has to say whether it read the host ────────────────────────

def test_every_inventory_producer_says_whether_it_enumerated():
    """The five in-agent products, plus Hyper-V and ESXi in the sibling runner.

    Forgetting the flag on a new product is not a wipe — the dashboard's default is to
    refuse — but it is a connection whose rows can never be pruned, reported to the
    operator as a permanently failing sync. There is no other place this shows up.
    """
    found = (_inventory_pages(_AGENT, lambda n: n.startswith("_sync_"))
             + _inventory_pages(_RUNNER, lambda n: n in ("_hyperv", "_esxi")))
    assert len(found) >= 7, (
        f"the sweep found only {len(found)} inventory pages — it should reach vsphere, "
        f"proxmox, nutanix, xcpng and workstation in the agent plus hyperv and esxi in "
        f"the runner. A rename has made this test vacuous.")
    for name, pairs in found:
        assert "enumerated" in pairs, (
            f"{name} returns an inventory page with no `enumerated` — an empty page "
            f"from it can never prune the connection's cache, and nothing else will "
            f"tell anyone")
        value = pairs["enumerated"]
        assert isinstance(value, ast.Constant) and value.value is True, (
            f"{name} sets `enumerated` to something other than a literal True. It is a "
            f"statement that the product's API answered, which is knowable only where "
            f"the answer was parsed.")


if __name__ == "__main__":
    _TESTS = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {exc}")
            failed = 1
        else:
            print(f"ok   {fn.__name__}")
    sys.exit(failed)
