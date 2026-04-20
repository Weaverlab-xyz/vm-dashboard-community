"""
PowerShell execution service.
Handles all communication between Python and the PowerShell CLI wrapper.

Execution mode is controlled by the POWERSHELL_EXECUTION_MODE env var:
  "local"      (default) — subprocess.run / Popen via pwsh.exe on the local machine.
                           Used for development on Windows.
  "ssh"                  — SSH to the Windows host (host.docker.internal) and run
                           pwsh there. Used when the app runs in a Linux dev container
                           but PowerShell scripts need the Windows host environment
                           (VMware, SMB shares, etc.). Mirrors the Hybrid Worker
                           pattern without requiring Azure.
  "automation"           — Azure Automation Hybrid Runbook Worker.
                           Used in cloud (Azure Container Apps) where pwsh.exe
                           and the VMware SMB share are not available.

Uses asyncio.to_thread + subprocess.run so the local path works with both
ProactorEventLoop and SelectorEventLoop on Windows.
"""
import json
import logging
import os
import re
import subprocess
import asyncio
import tempfile
import threading
from typing import Optional, Callable, AsyncGenerator
from datetime import datetime

from ..config import settings

logger = logging.getLogger(__name__)

# Execution mode: "local" or "automation"
_EXECUTION_MODE = os.getenv("POWERSHELL_EXECUTION_MODE", "local").lower()

# Progress line pattern emitted by vm_cli_api_wrapper.ps1
_PROGRESS_RE = re.compile(r'^PROGRESS:(\d+):(.+)$')

_PS_CMD = [
    "pwsh.exe",
    "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
]


def _ssh_cmd(wrapper_path: str) -> list:
    r"""Build the SSH command list for running pwsh on the Windows host.

    JSON is piped via stdin; the remote pwsh reads it via $input so no temp
    file is needed and there are no quoting issues with the JSON payload.

    Setup-DevSsh.ps1 sets two registry values under HKLM:\SOFTWARE\OpenSSH:
      DefaultShell            = C:\...\pwsh.exe
      DefaultShellCommandOption = -Command
    OpenSSH therefore executes our script as: pwsh -Command "<remote_ps>"
    We must NOT prefix the command with "pwsh ..." ourselves — that causes
    double-invocation where the inner pwsh loses SSH stdin.
    """
    from ..config import settings
    remote_ps = (
        "$j = $input | Out-String; "
        f"& '{wrapper_path}' -JsonInput $j"
    )
    return [
        "ssh",
        "-i", settings.ssh_key_file,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        f"{settings.ssh_user}@{settings.ssh_host}",
        remote_ps,   # OpenSSH prepends: pwsh -Command
    ]


def _run_ps_ssh(wrapper_path: str, json_input: str, timeout: int) -> subprocess.CompletedProcess:
    """Blocking SSH→pwsh call — safe to run in a thread."""
    return subprocess.run(
        _ssh_cmd(wrapper_path),
        input=json_input.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )


class PowerShellError(Exception):
    """Raised when a PowerShell execution fails"""
    def __init__(self, message: str, error_code: str = "POWERSHELL_ERROR"):
        super().__init__(message)
        self.error_code = error_code


# ── Synchronous helper (runs in thread pool) ──────────────────────────────────

def _run_ps(wrapper_path: str, json_input: str, timeout: int) -> subprocess.CompletedProcess:
    """Blocking PowerShell call - safe to run in a thread.

    Writes JSON to a temp file and reads it back via -Command to avoid
    Windows command-line escaping issues with embedded quotes/backslashes.
    """
    json_file = None
    try:
        json_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        )
        json_file.write(json_input)
        json_file.close()

        ps_command = (
            f"& '{wrapper_path}' -JsonInput "
            f"(Get-Content -Raw -LiteralPath '{json_file.name}')"
        )
        return subprocess.run(
            _PS_CMD + ["-Command", ps_command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            text=False,
        )
    finally:
        if json_file and os.path.exists(json_file.name):
            os.unlink(json_file.name)


# ── Public async API ──────────────────────────────────────────────────────────

async def execute(action: str, params: dict) -> dict:
    """
    Execute an action via vm_cli_api_wrapper.ps1 and return parsed JSON result.
    Suitable for short-running operations (list VMs, get IP, health check, etc.)

    Routes through Azure Automation Hybrid Worker when
    POWERSHELL_EXECUTION_MODE=automation; uses local subprocess otherwise.

    Raises:
        PowerShellError: If execution fails or the wrapper returns an error.
    """
    if _EXECUTION_MODE == "automation":
        from . import automation_service
        try:
            return await automation_service.execute(action, params)
        except Exception as exc:
            raise PowerShellError(str(exc), "AUTOMATION_ERROR") from exc

    json_input = json.dumps({"action": action, **params})
    wrapper_path = settings.vm_cli_wrapper_path

    # In SSH mode the wrapper lives on the Windows host — skip the local path check.
    if _EXECUTION_MODE != "ssh" and not os.path.exists(wrapper_path):
        raise PowerShellError(
            f"PowerShell wrapper not found: {wrapper_path}",
            "WRAPPER_NOT_FOUND"
        )

    logger.debug("PS execute action=%s mode=%s", action, _EXECUTION_MODE)

    _runner = _run_ps_ssh if _EXECUTION_MODE == "ssh" else _run_ps
    try:
        result = await asyncio.to_thread(
            _runner, wrapper_path, json_input, settings.powershell_timeout
        )
    except subprocess.TimeoutExpired:
        raise PowerShellError(
            f"PowerShell timed out after {settings.powershell_timeout}s", "TIMEOUT"
        )
    except Exception as e:
        raise PowerShellError(f"Failed to launch PowerShell: {type(e).__name__}: {e}", "LAUNCH_ERROR")

    stdout_text = result.stdout.decode("utf-8", errors="replace").strip()
    stderr_text = result.stderr.decode("utf-8", errors="replace").strip()

    if stderr_text:
        logger.warning("PS stderr action=%s: %s", action, stderr_text[:500])

    json_line = _extract_json(stdout_text)
    if not json_line:
        logger.error(
            "PS no-JSON action=%s returncode=%s\nSTDERR: %s\nSTDOUT (last 1000): %s",
            action, result.returncode, stderr_text[:500], stdout_text[-1000:],
        )
        raise PowerShellError(
            f"No JSON in PowerShell output. rc={result.returncode} stdout={stdout_text[:500]}",
            "NO_JSON_RESPONSE"
        )

    try:
        data = json.loads(json_line)
    except json.JSONDecodeError as e:
        raise PowerShellError(f"Invalid JSON: {e}. raw={json_line[:200]}", "INVALID_JSON_RESPONSE")

    if not data.get("success", False):
        raise PowerShellError(
            data.get("error", "Unknown error"),
            data.get("error_code", "PS_ERROR")
        )

    return data


async def execute_with_progress(
    action: str,
    params: dict,
    on_progress: Optional[Callable[[int, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> AsyncGenerator[dict, None]:
    """
    Execute a long-running action and stream progress + log events.

    Runs the PowerShell process in a thread and yields events:
      {"type": "progress", "pct": int, "message": str}
      {"type": "log",      "line": str, "timestamp": str}
      {"type": "result",   "data": dict}   ← final JSON from wrapper
    """
    json_input = json.dumps({"action": action, **params})
    wrapper_path = settings.vm_cli_wrapper_path

    if _EXECUTION_MODE != "ssh" and not os.path.exists(wrapper_path):
        raise PowerShellError(
            f"PowerShell wrapper not found: {wrapper_path}", "WRAPPER_NOT_FOUND"
        )

    _runner = _run_ps_ssh if _EXECUTION_MODE == "ssh" else _run_ps
    # Run blocking subprocess in thread pool and collect all output
    try:
        result = await asyncio.to_thread(
            _runner, wrapper_path, json_input, settings.powershell_timeout
        )
    except subprocess.TimeoutExpired:
        raise PowerShellError(
            f"PowerShell timed out after {settings.powershell_timeout}s", "TIMEOUT"
        )
    except Exception as e:
        raise PowerShellError(f"Failed to launch PowerShell: {type(e).__name__}: {e}", "LAUNCH_ERROR")

    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace").strip()

    if stderr_text:
        logger.warning("PS stderr action=%s: %s", action, stderr_text[:500])

    result_json_line: Optional[str] = None

    for line in stdout_text.splitlines():
        line = line.rstrip()
        progress_match = _PROGRESS_RE.match(line)
        if progress_match:
            pct = int(progress_match.group(1))
            message = progress_match.group(2)
            if on_progress:
                on_progress(pct, message)
            yield {"type": "progress", "pct": pct, "message": message}
        elif line.startswith("{") and line.endswith("}"):
            result_json_line = line
        else:
            if on_log:
                on_log(line)
            yield {"type": "log", "line": line, "timestamp": _now()}

    if result_json_line:
        try:
            data = json.loads(result_json_line)
            yield {"type": "result", "data": data}
        except json.JSONDecodeError as e:
            raise PowerShellError(f"Invalid final JSON: {e}", "INVALID_JSON_RESPONSE")
    elif result.returncode != 0:
        raise PowerShellError(
            f"PowerShell exited {result.returncode}. stderr={stderr_text[:300]}",
            "NONZERO_EXIT"
        )


# ── True streaming (Popen + thread reader) ────────────────────────────────────

async def execute_streaming(
    action: str,
    params: dict,
) -> AsyncGenerator[dict, None]:
    """
    Execute a long-running PowerShell action with real-time streaming.

    Routes through Azure Automation Hybrid Worker when
    POWERSHELL_EXECUTION_MODE=automation (polls job streams every 3s).
    Uses subprocess.Popen with a daemon thread for the local path (true streaming).

    Yields the same event dicts in both modes:
      {"type": "progress", "pct": int, "message": str}
      {"type": "log",      "line": str, "timestamp": str}
      {"type": "result",   "data": dict}   ← final JSON from wrapper

    Raises PowerShellError on launch failure or non-zero exit without a result.
    """
    if _EXECUTION_MODE == "automation":
        from . import automation_service
        try:
            async for event in automation_service.execute_streaming(action, params):
                yield event
        except Exception as exc:
            raise PowerShellError(str(exc), "AUTOMATION_ERROR") from exc
        return

    json_input = json.dumps({"action": action, **params})
    wrapper_path = settings.vm_cli_wrapper_path

    if _EXECUTION_MODE != "ssh" and not os.path.exists(wrapper_path):
        raise PowerShellError(
            f"PowerShell wrapper not found: {wrapper_path}", "WRAPPER_NOT_FOUND"
        )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _reader():
        try:
            if _EXECUTION_MODE == "ssh":
                proc = subprocess.Popen(
                    _ssh_cmd(wrapper_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                )
                proc.stdin.write(json_input.encode("utf-8"))
                proc.stdin.close()
            else:
                json_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, encoding="utf-8",
                )
                json_file.write(json_input)
                json_file.close()
                ps_command = (
                    f"& '{wrapper_path}' -JsonInput "
                    f"(Get-Content -Raw -LiteralPath '{json_file.name}')"
                )
                proc = subprocess.Popen(
                    _PS_CMD + ["-Command", ps_command],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                )
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                loop.call_soon_threadsafe(queue.put_nowait, ("line", line))
            proc.stdout.close()
            proc.stderr.close()
            proc.wait()
            loop.call_soon_threadsafe(queue.put_nowait, ("done", proc.returncode))
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
        finally:
            if _EXECUTION_MODE != "ssh":
                try:
                    os.unlink(json_file.name)
                except Exception:
                    pass

    threading.Thread(target=_reader, daemon=True).start()

    result_json_line: Optional[str] = None

    while True:
        tag, value = await queue.get()
        if tag == "error":
            raise PowerShellError(f"Subprocess launch error: {value}", "LAUNCH_ERROR")
        if tag == "done":
            break
        # tag == "line"
        line: str = value
        m = _PROGRESS_RE.match(line)
        if m:
            yield {"type": "progress", "pct": int(m.group(1)), "message": m.group(2)}
        elif line.startswith("{") and line.endswith("}"):
            result_json_line = line
        else:
            yield {"type": "log", "line": line, "timestamp": _now()}

    if result_json_line:
        try:
            yield {"type": "result", "data": json.loads(result_json_line)}
        except json.JSONDecodeError as e:
            raise PowerShellError(
                f"Invalid final JSON: {e}. raw={result_json_line[:200]}", "INVALID_JSON_RESPONSE"
            )


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[str]:
    """Return the last JSON-looking line from multi-line stdout."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return line
    return None


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
