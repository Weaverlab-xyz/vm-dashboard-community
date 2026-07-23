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


def test_run_command_script_wraps_with_set_e_and_marker():
    script = az._run_command_script(["do-a", "do-b"])
    assert script[0] == "set -e"
    assert script[1:3] == ["do-a", "do-b"]
    assert script[-1] == f'echo "{az._RUN_CMD_MARKER}=$?"'


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
