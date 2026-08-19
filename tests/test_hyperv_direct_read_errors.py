"""What the DIRECT (non-agent) Hyper-V path tells an operator when the host says no.

Two defects in `web_dashboard/services/hyperv_service.py`, one bug class: a failed read
that does not read as a failure. Both have a twin on the agent path
(`runners/hypervisor/run.py`), which is the point — one host, two transports, and a
message an operator can act on has to come out of either.

**A PowerShell error arrived as a type error.** `_run_ps` did
`result.std_err.decode(...)`, but pywinrm's `Session.run_ps` does not leave `std_err`
alone: whenever it is non-empty it REPLACES it with the return of `_clean_error_msg`,
which unwraps the CLIXML envelope and yields a `str`. It stays `bytes` only while it is
empty — only when there is nothing to report. So the attribute error fired on exactly the
branch that exists to report the failure, for every op: Start, Force Off, the inventory
read. What surfaced through the router's 502 was `'str' object has no attribute 'decode'`
instead of the sentence PowerShell had already written ("You do not have the required
permission..."). An operator reading that files a bug against the dashboard rather than a
permission request against the host.

**Silence was reported as an empty host.** `_LIST_VMS_PS` prints a literal `'[]'` when
`Get-VM` returns nothing, and a JSON array in every other branch, so genuinely empty
stdout means the script never got as far as printing — the Hyper-V PowerShell module not
loading, or an account that can open a WinRM session but cannot enumerate VMs. Both leave
the exit code at 0, so `_run_ps` passes them straight through, and `return []` turned each
into an empty VM table with no error anywhere on the page. This is the milder half of the
pair — a live read, so nothing cached is destroyed and a Refresh self-corrects once the
cause is fixed — but it is the same wrong answer. Both directions are pinned below: the
unreadable host errors, and the genuinely empty host still lists as empty rather than
being traded for a false alarm.

`winrm` is stubbed in `sys.modules` and the service is loaded from its file: the module is
stdlib-only by design, so this file must not need the app's dependencies to run on the
machine where someone edits it.

Runs under pytest, or standalone:  python tests/test_hyperv_direct_read_errors.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICE = os.path.join(_ROOT, "web_dashboard", "services", "hyperv_service.py")

# ── the fake WinRM host ───────────────────────────────────────────────────────

SESSIONS = []          # every winrm.Session(**kwargs) the service opened
SCRIPTS = []           # every script it ran


class _Result:
    """pywinrm's `Response`: three attributes, and std_err's TYPE is the whole trap."""

    def __init__(self, status_code=0, std_out=b"", std_err=b""):
        self.status_code = status_code
        self.std_out = std_out
        self.std_err = std_err


class _Session:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        SESSIONS.append(kwargs)

    def run_ps(self, script):
        SCRIPTS.append(script)
        return self.result


def _winrm_stub(result):
    """Install a `winrm` module whose one session returns `result` for any script."""
    mod = types.ModuleType("winrm")

    def session(**kwargs):
        sess = _Session(**kwargs)
        sess.result = result
        return sess

    mod.Session = session
    sys.modules["winrm"] = mod
    return mod


def _stub_cloud_executor():
    """A stand-in for `services/cloud_executor`, which the service's `_to_thread` needs.

    Blocking WinRM calls run on the hyperv pool now rather than the event loop's shared
    default executor, so `_to_thread` does `from . import cloud_executor`. Under the plain
    file load this module used to get, that raised "attempted relative import with no known
    parent package" at CALL time — surfacing as a HyperVError with an import message in it,
    since `_to_thread` translates every CloudCallError into one.

    The real module needs config_service, so it is stubbed rather than loaded: what this
    file tests is the message a failed read produces, not thread-pool admission. Running
    the function inline is enough for that, and `tests/test_cloud_executor.py` owns the
    pool's own behaviour.
    """
    mod = types.ModuleType("web_dashboard.services.cloud_executor")

    class CloudCallError(Exception):
        pass

    async def run(_provider, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    mod.CloudCallError = CloudCallError
    mod.run = run
    sys.modules[mod.__name__] = mod
    return mod


def _service():
    """Load `services/hyperv_service` from source, with `winrm` already stubbed.

    Loaded under its REAL dotted name, with stub parent packages, so the relative import in
    `_to_thread` resolves. In the app this module is only ever imported as
    `web_dashboard.services.hyperv_service`; the bare name this used to load under was the
    one context in which its relative imports could not work.
    """
    for name in ("web_dashboard", "web_dashboard.services"):
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = []          # mark it a package so submodule imports resolve
            sys.modules[name] = pkg
    _stub_cloud_executor()

    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.hyperv_service", _SERVICE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Conn:
    """The resolved `Connection` the router hands in — enough of one for `_session`."""

    def __init__(self, **kw):
        self.name = kw.get("name", "hv-lab-01")
        self.host = kw.get("host", "10.0.0.50")
        self.port = kw.get("port", 5985)
        self.username = kw.get("username", "LAB\\svc-dash")
        self.secret = kw.get("secret", "pw")
        self.options = kw.get("options", {})
        self.verify_ssl = kw.get("verify_ssl", False)


def _load(result):
    SESSIONS.clear()
    SCRIPTS.clear()
    _winrm_stub(result)
    return _service()


_VM_ROW = {
    "VMId": "8f2b1c44-3a5e-4d61-9b0f-6c7e2a1d4b93",
    "Name": "dc01",
    "State": 2,
    "CPUUsage": 3,
    "MemoryAssignedMB": 4096,
    "MemoryStartupMB": 2048,
    "ProcessorCount": 4,
    "Generation": 2,
    "UptimeSecs": 90061,
    "IPAddresses": ["10.0.0.51"],
    "IntegrationServicesState": "Up to date",
    "Path": "C:\\VMs\\dc01",
}

# The shape pywinrm hands back after cleaning: a str, not bytes.
_PS_DENIED = (
    "Get-VM : You do not have the required permission to complete this task. Contact "
    "the administrator of the authorization policy for the computer 'HV-LAB-01'."
)


# ── defect 1: the PowerShell message, not an AttributeError ───────────────────

def test_a_str_stderr_is_reported_rather_than_decoded():
    """The regression. pywinrm replaces std_err with a cleaned `str` whenever it is
    non-empty, so `.decode()` on it raised AttributeError on the one path meant to report
    the error."""
    svc = _load(_Result(status_code=1, std_err=_PS_DENIED))
    try:
        svc._list_vms_sync(_Conn())
    except svc.HyperVError as e:
        assert "required permission" in str(e), str(e)
    except AttributeError as e:                      # what shipped
        raise AssertionError(
            f"_run_ps still decodes a str std_err: {e}. pywinrm replaces std_err with a "
            f"cleaned str whenever it is non-empty — route it through _text()."
        ) from None
    else:
        raise AssertionError("a non-zero status_code must raise HyperVError")


def test_a_bytes_stderr_is_still_decoded():
    """std_err stays bytes when empty, and a patched or older pywinrm can leave a raw
    payload in place. _text has to take both, not swap one crash for the other."""
    svc = _load(_Result(status_code=1, std_err=_PS_DENIED.encode("utf-8")))
    try:
        svc._list_vms_sync(_Conn())
        raise AssertionError("expected HyperVError")
    except svc.HyperVError as e:
        assert "required permission" in str(e), str(e)


def test_undecodable_bytes_do_not_replace_the_error_with_a_worse_one():
    """A UnicodeDecodeError here would lose the failure entirely, so errors='replace'."""
    svc = _load(_Result(status_code=1, std_err=b"Get-VM : \xff\xfe broke"))
    try:
        svc._list_vms_sync(_Conn())
        raise AssertionError("expected HyperVError")
    except svc.HyperVError as e:
        assert "Get-VM" in str(e), str(e)


def test_an_empty_stderr_falls_back_to_a_reason():
    """A non-zero exit with nothing on stderr still has to say something."""
    svc = _load(_Result(status_code=1, std_err=b""))
    try:
        svc._list_vms_sync(_Conn())
        raise AssertionError("expected HyperVError")
    except svc.HyperVError as e:
        assert "non-zero exit" in str(e), str(e)


def test_text_takes_every_shape_run_ps_can_return():
    svc = _load(_Result())
    assert svc._text(b"bytes") == "bytes"
    assert svc._text("str") == "str"
    assert svc._text(None) == ""
    assert svc._text(b"") == ""
    assert svc._text(b"\xff") == "\ufffd"


def test_a_power_op_failure_reports_the_message_the_host_gave():
    """Power ops are the other caller of _run_ps, and the surface where an operator meets
    this most often: one click, one 502, and the detail string is all they get."""
    svc = _load(_Result(status_code=1, std_err=_PS_DENIED))
    try:
        svc._power_op_sync(_Conn(), _VM_ROW["VMId"], "dc01", "stop")
        raise AssertionError("expected HyperVError")
    except svc.HyperVError as e:
        assert "required permission" in str(e), str(e)


def test_the_message_survives_the_async_wrapper_the_router_catches():
    """`power_op` re-raises HyperVError untouched and wraps anything else as
    "failed: <exc>" — which is how "has no attribute 'decode'" reached the page."""
    svc = _load(_Result(status_code=1, std_err=_PS_DENIED))
    try:
        asyncio.run(svc.power_op(_Conn(), _VM_ROW["VMId"], "dc01", "start"))
        raise AssertionError("expected HyperVError")
    except svc.HyperVError as e:
        assert "required permission" in str(e), str(e)
        assert "has no attribute" not in str(e), str(e)


# ── defect 2: silence is not an empty host ────────────────────────────────────

def test_no_output_is_an_error_naming_the_likely_cause():
    """The regression. `_LIST_VMS_PS` prints '[]' for a host with no VMs, so empty stdout
    means the session could not read the host — and `return []` rendered that as an empty
    VM table with no error at all."""
    svc = _load(_Result(status_code=0, std_out=b"   \r\n"))
    try:
        svc._list_vms_sync(_Conn())
    except svc.HyperVError as e:
        msg = str(e)
        assert "hv-lab-01" in msg, msg
        # The message has to point somewhere. These are the two causes that produce it.
        assert "Hyper-V" in msg and "module" in msg, msg
        assert "svc-dash" in msg, msg
    else:
        raise AssertionError(
            "empty stdout still lists as an empty host. Get-VM printing nothing is an "
            "unreadable host, not a host with no VMs — raise HyperVError."
        )


def test_a_bare_null_is_an_error_too():
    """`_LIST_VMS_PS` has no branch that prints 'null'; only a broken read does."""
    svc = _load(_Result(status_code=0, std_out=b"null"))
    try:
        svc._list_vms_sync(_Conn())
        raise AssertionError("'null' still lists as an empty host")
    except svc.HyperVError as e:
        assert "no VM data" in str(e), str(e)


def test_a_host_with_no_vms_still_lists_as_empty():
    """The other half, and the reason the fix checks stdout rather than the parsed length:
    a real empty host prints '[]' and must NOT become a false alarm."""
    svc = _load(_Result(status_code=0, std_out=b"[]"))
    assert svc._list_vms_sync(_Conn()) == []


def test_the_empty_host_literal_is_still_in_the_script():
    """The whole distinction rests on `_LIST_VMS_PS` printing '[]' for Count -eq 0. If
    that branch is ever dropped, empty stdout is ambiguous again and the error above
    starts firing on healthy hosts that simply have no VMs."""
    svc = _load(_Result())
    assert "'[]'" in svc._LIST_VMS_PS, (
        "_LIST_VMS_PS no longer prints a literal '[]' for a host with no VMs — "
        "_list_vms_sync treats empty stdout as an unreadable host on the strength of it."
    )


def test_a_real_vm_list_still_parses():
    """Guards the happy path against both edits."""
    svc = _load(_Result(status_code=0, std_out=json.dumps([_VM_ROW]).encode("utf-8")))
    vms = svc._list_vms_sync(_Conn())
    assert len(vms) == 1, vms
    assert vms[0]["name"] == "dc01"
    assert vms[0]["state_label"] == "Running"
    assert vms[0]["is_running"] is True
    assert vms[0]["ip_addresses"] == ["10.0.0.51"]


def test_a_single_vm_serialised_as_an_object_still_parses():
    """PS 5.1's ConvertTo-Json quirk. `_LIST_VMS_PS` wraps the one-VM case itself, and the
    dict branch in `_list_vms_sync` is the belt to that braces — neither edit touches it,
    so pin it while the parse path is under test."""
    svc = _load(_Result(status_code=0, std_out=json.dumps(_VM_ROW).encode("utf-8")))
    assert [v["name"] for v in svc._list_vms_sync(_Conn())] == ["dc01"]


def test_unparseable_output_is_still_its_own_error():
    """Not the same fault as silence, and it must not be folded into it: output that fails
    to parse means the host said something, and the operator needs to see what."""
    svc = _load(_Result(status_code=0, std_out=b"Get-VM : partial <html>"))
    try:
        svc._list_vms_sync(_Conn())
        raise AssertionError("expected HyperVError")
    except svc.HyperVError as e:
        assert "Failed to parse VM list" in str(e), str(e)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
