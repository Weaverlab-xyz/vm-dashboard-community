"""Unit tests for the Azure VM Run Command helpers in azure_service.

These cover the pure, SDK-free plumbing that gives ``vm_run_command`` the same
``{status, response_code, stdout, stderr}`` contract as ``aws_service.ssm_send_command``:
- ``_run_command_script`` wraps the caller's commands with ``set -e`` + the exit-code
  marker (so an in-guest failure is not masked by Azure's ARM-level success);
- ``_parse_run_command_output`` pulls stdout/stderr out of the RunCommandResult's
  InstanceViewStatus list;
- ``_finalize_run_result`` extracts the marker → real response_code and strips it.

Runs under pytest or standalone:  python tests/test_azure_run_command.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import azure_service as az  # noqa: E402


class _St:
    def __init__(self, code, message):
        self.code = code
        self.message = message


class _Res:
    def __init__(self, value):
        self.value = value


def test_run_command_script_wraps_with_set_e_and_trap_marker():
    script = az._run_command_script(["do-a", "do-b"])
    assert script[0] == "set -e"
    # The marker is printed from an EXIT trap, not a trailing echo: a trailing echo
    # only runs on success, so every `set -e` abort came back as the same rc of -1.
    assert script[1] == f"""trap 'rc=$?; echo "{az._RUN_CMD_MARKER}=$rc"' EXIT"""
    assert script[2:] == ["do-a", "do-b"]


def test_parse_run_command_output_splits_stdout_and_stderr():
    res = _Res([_St("ComponentStatus/StdOut/succeeded", "the output"),
                _St("ComponentStatus/StdErr/succeeded", "the error")])
    stdout, stderr = az._parse_run_command_output(res)
    assert stdout == "the output"
    assert stderr == "the error"


def test_parse_run_command_output_tolerates_empty_value():
    stdout, stderr = az._parse_run_command_output(_Res(None))
    assert (stdout, stderr) == ("", "")


def test_finalize_success_extracts_and_strips_marker():
    res = az._finalize_run_result(f"created ok\n{az._RUN_CMD_MARKER}=0", "")
    assert res["status"] == "Success"
    assert res["response_code"] == 0
    assert res["stdout"] == "created ok"          # marker stripped
    assert res["stderr"] == ""


def test_finalize_nonzero_marker_is_failure():
    res = az._finalize_run_result(f"partial\n{az._RUN_CMD_MARKER}=1", "boom")
    assert res["status"] == "Failed"
    assert res["response_code"] == 1
    assert res["stderr"] == "boom"


def test_finalize_missing_marker_is_failure():
    # set -e aborted before the marker printed → treat as failure (rc -1).
    res = az._finalize_run_result("aborted early", "psql: connection refused")
    assert res["status"] == "Failed"
    assert res["response_code"] == -1
    assert res["stdout"] == "aborted early"


# ── The Linux shape ───────────────────────────────────────────────────────────
# A Linux guest (RunShellScript) returns ONE ProvisioningState status with both
# streams packed into its message; only a Windows guest returns the ComponentStatus
# pair above. Parsing only the Windows shape made every Azure Password Safe cloud-DB
# onboarding fail with "status=Failed, rc=-1" and an empty detail, whatever the
# script actually did.

def _linux_status(message):
    return _Res([_St("ProvisioningState/succeeded", message)])


def test_parse_linux_packed_message_success():
    stdout, stderr = az._parse_run_command_output(_linux_status(
        f"Enable succeeded: \n[stdout]\nreading package lists\n"
        f"{az._RUN_CMD_MARKER}=0\n\n[stderr]\n"))
    assert stdout == f"reading package lists\n{az._RUN_CMD_MARKER}=0"
    assert stderr == ""
    res = az._finalize_run_result(stdout, stderr)
    assert (res["status"], res["response_code"]) == ("Success", 0)


def test_parse_linux_packed_message_failure_keeps_stderr_and_exit_status():
    res = az._finalize_run_result(*az._parse_run_command_output(_linux_status(
        "Enable failed: failed to execute command: command terminated with "
        "exit status=100\n[stdout]\nreading package lists\n\n[stderr]\n"
        "E: Unable to locate package mssql-tools18\n")))
    assert res["status"] == "Failed"
    assert res["response_code"] == 100      # from Azure's verdict, not the catch-all -1
    assert "Unable to locate package mssql-tools18" in res["stderr"]
    assert res["stdout"] == "reading package lists"


def test_parse_linux_message_without_sections_is_all_stderr():
    # An extension-level failure: the script never ran, so there are no [stdout] /
    # [stderr] sections and the message itself is the whole diagnosis.
    stdout, stderr = az._parse_run_command_output(
        _Res([_St("ProvisioningState/failed", "VMAgent unresponsive")]))
    assert stdout == ""
    assert stderr == "VMAgent unresponsive"


def test_component_status_shape_still_wins():
    # Windows guests keep the per-stream shape; the packed fallback must not eat it.
    stdout, stderr = az._parse_run_command_output(
        _Res([_St("ProvisioningState/succeeded", "Enable succeeded: \n[stdout]\nx"),
              _St("ComponentStatus/StdOut/succeeded", "the output"),
              _St("ComponentStatus/StdErr/succeeded", "the error")]))
    assert (stdout, stderr) == ("the output", "the error")


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
