"""Structural guards for scripts/Enable-VmrestAutostart.ps1.

The script registers a Windows scheduled task, so nothing here runs it. What it pins is
the handful of decisions that are wrong-but-plausible, each of which produces an
autostart that looks registered and then does not work — or works and nobody can tell
why it stopped:

  * running the task as SYSTEM, which cannot read the per-user vmrest credential
  * showing a console window, which is the thing that gets closed in the first place
  * registering an autostart with no credential configured, which is a relaunch loop
    every five minutes whose only symptom is a refused port
  * looking for vmrest only under Program Files (x86) — Workstation 26 installs 64-bit
  * starting a second vmrest on top of a running one

Runs under pytest, or standalone:
    python tests/test_vmrest_autostart_script.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "Enable-VmrestAutostart.ps1")
_DOC = os.path.join(_ROOT, "docs", "integrations", "vmware.md")

# The heading the agent's own refusal message points an operator at. Named here as well
# as there so that renaming it fails a test rather than only a doc-anchor sweep.
_ANCHOR = "1a-keep-it-running-reboots-logouts-closed-windows"


def _src():
    with open(_SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def _code():
    """The script with comment and help lines removed.

    Two thirds of this file is prose — a comment-block SYNOPSIS plus the reasoning for
    each decision — and an assertion satisfied by prose is an assertion about nothing.
    """
    out, in_help = [], False
    for line in _src().splitlines():
        stripped = line.strip()
        if stripped.startswith("<#"):
            in_help = True
        if in_help:
            if stripped.endswith("#>"):
                in_help = False
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def _launcher_lines():
    """The string literals the script writes into the generated launcher.

    The launcher is PowerShell built by PowerShell, so it is the one part of this that
    can be syntactically wrong without the script itself failing to parse.
    """
    code = _code()
    start = code.index("$lines = @(")
    end = code.index("\n    )", start)
    return code[start:end]


# ── Shape ────────────────────────────────────────────────────────────────────

def test_the_script_exists_and_declares_its_floor():
    assert os.path.isfile(_SCRIPT)
    # Windows PowerShell 5.1 is what a Workstation host has without installing
    # anything. ScheduledTasks cmdlets and Join-Path -LiteralPath both predate it.
    assert "#Requires -Version 5.1" in _src()


def test_it_has_a_read_only_status_path_and_an_undo():
    code = _code()
    assert "$Status" in code and "$Unregister" in code
    # -Status must not be able to change anything: it is what an operator runs while
    # diagnosing, often on someone else's machine.
    status_body = code[code.index("function Show-Status"):code.index("function Remove-Autostart")]
    for forbidden in ("Register-ScheduledTask", "Start-ScheduledTask", "Set-Content",
                      "Remove-Item", "Stop-Process"):
        assert forbidden not in status_body, f"-Status calls {forbidden}"


# ── Who the task runs as ─────────────────────────────────────────────────────

def test_the_task_runs_as_the_interactive_user():
    """`vmrest -C` writes its credential under %APPDATA% and the VM inventory is
    per-user, so a task running as SYSTEM authenticates with a credential it cannot read
    and lists an inventory that is not the operator's. It would register cleanly."""
    code = _code()
    assert "-LogonType Interactive" in code
    assert "WindowsIdentity]::GetCurrent().Name" in code, (
        "the principal must be the invoking user, not a hardcoded or guessed account")
    for forbidden in ("-ServiceAccount", "S4U", "'SYSTEM'", '"SYSTEM"', "-Password"):
        assert forbidden not in code, f"the task must not run as a service account ({forbidden})"


def test_the_task_is_unelevated():
    # vmrest does not need administrator; registering the task does. Conflating the two
    # would leave every VM's files owned in a way the interactive Workstation cannot use.
    assert "-RunLevel Limited" in _code()


# ── Finding vmrest ───────────────────────────────────────────────────────────

def test_the_registry_is_consulted_before_the_program_files_guesses():
    """Workstation 26 installs to Program Files (64-bit) and records InstallPath under
    `HKLM:\\SOFTWARE\\VMware, Inc.\\VMware Workstation` with no WOW6432Node key. A
    script that knew only the (x86) path reports "not installed" on a host where it is."""
    code = _code()
    reg = code.index("InstallPath")
    x86 = code.index("Program Files (x86)")
    assert reg < x86, "the hardcoded guesses are consulted before the registry"
    assert "WOW6432Node\\VMware, Inc.\\VMware Workstation" in code
    assert "HKLM:\\SOFTWARE\\VMware, Inc.\\VMware Workstation" in code
    assert re.search(r"'C:\\Program Files\\VMware\\VMware Workstation\\vmrest\.exe'", code), (
        "the 64-bit fallback path is missing")


# ── The credential prerequisite ──────────────────────────────────────────────

def test_it_refuses_to_register_without_a_credential():
    """vmrest with no stored credential does not 401 — it prints "Please use -C to
    update credential" and exits 1. Registering an autostart for that is a relaunch
    every CheckIntervalMinutes, forever, presenting as a refused port with no console
    anywhere to read the reason from."""
    code = _code()
    assert "vmrest.cfg" in code, "nothing looks for the credential file"
    guard = re.search(r"if \(-not \(Get-CredentialFileHint\) -and -not \$Force\) \{\s*throw",
                      code)
    assert guard, "the credential pre-flight must throw, not warn"
    assert "$Force" in code, "there is no override for a build that stores it elsewhere"


# ── The generated launcher ───────────────────────────────────────────────────

def test_the_launcher_starts_vmrest_hidden():
    """A visible console window is how vmrest goes away mid-session: someone closes it.
    Hiding it is the point of the launcher existing at all."""
    lines = _launcher_lines()
    assert lines.count("-WindowStyle Hidden") >= 2, (
        "both the arguments and the no-arguments branch must hide the window")


def test_the_launcher_never_redirects_vmrest_output():
    """A redirect forces UseShellExecute=false, which puts the console window back on
    screen — so capturing vmrest's own message costs exactly the property being bought.
    The early-exit code is the diagnosis instead."""
    code = _code()
    for forbidden in ("-RedirectStandardOutput", "-RedirectStandardError", "-NoNewWindow"):
        assert forbidden not in code, f"{forbidden} defeats -WindowStyle Hidden"


def test_the_launcher_is_idempotent():
    """It runs every few minutes. Starting a second vmrest on a taken port produces a
    process that exits immediately and a log that blames the credential."""
    lines = _launcher_lines()
    # The process name is interpolated, so match the guard's shape rather than the
    # rendered line: "if (Get-Process -Name '" + $ProcessName + "' … ) { exit 0 }".
    guard = lines.index("Get-Process -Name")
    start = lines.index("Start-Process")
    assert guard < start, "the launcher starts vmrest before checking whether it is up"
    assert "$ProcessName" in lines and "exit 0" in lines
    assert "-MultipleInstances IgnoreNew" in _code()


def test_the_launcher_diagnoses_an_immediate_exit():
    lines = _launcher_lines()
    assert "-PassThru" in lines and "HasExited" in lines, (
        "an immediate exit must be detected — it is the credential case")
    assert "$proc.ExitCode" in lines
    assert "Write-Log" in lines, "a hidden process has nowhere else to report from"


def test_the_launcher_has_no_backtick_inside_a_double_quoted_string():
    """`v in a double-quoted PowerShell string is a vertical tab, so a generated
    Write-Log "run `vmrest -C`" logs control characters. Single quotes, or nothing."""
    for line in _launcher_lines().splitlines():
        body = line.strip()
        if not body.startswith("'") and not body.startswith("("):
            continue
        for quoted in re.findall(r'"([^"]*)"', body):
            assert "`" not in quoted, f"backtick inside a double-quoted line: {body}"


def test_nothing_is_generated_inside_the_repository():
    # The launcher and its log follow the host, not the checkout: a repo path breaks the
    # task the moment the clone moves, and a log file in a working tree gets committed.
    code = _code()
    assert "$env:LOCALAPPDATA" in code
    assert "$PSScriptRoot" not in code


# ── Triggers ─────────────────────────────────────────────────────────────────

def test_there_are_both_a_logon_trigger_and_a_repeat():
    """Logon alone misses a mid-session crash; a repeat alone leaves a gap of up to
    CheckIntervalMinutes after every reboot, which is when someone opens the page."""
    code = _code()
    assert "-AtLogOn" in code
    assert "-RepetitionInterval" in code


def test_the_repeat_survives_an_older_build():
    """[TimeSpan]::MaxValue is how "repeat indefinitely" is expressed, and older
    Windows rejects it — falling back to nothing would mean a repetition that expires
    silently after its default duration."""
    code = _code()
    assert "[TimeSpan]::MaxValue" in code
    fallback = code[code.index("New-RepeatTrigger"):]
    assert "-Days 3650" in fallback, "no fallback duration for a build that refuses MaxValue"


# ── Undo ─────────────────────────────────────────────────────────────────────

def test_unregister_removes_the_task_and_the_launcher():
    body = _code()
    body = body[body.index("function Remove-Autostart"):body.index("function Register-Autostart")]
    assert "Unregister-ScheduledTask" in body and "-Confirm:$false" in body, (
        "an unattended session cannot answer a confirmation prompt")
    assert "Remove-Item" in body, "the generated launcher is left behind"
    assert "Stop-Process" not in body.replace("Stop-Process -Name vmrest", ""), (
        "removing the autostart must not kill a running vmrest")


# ── The surfaces that point at it ────────────────────────────────────────────

def test_the_operator_guide_documents_it():
    with open(_DOC, encoding="utf-8") as fh:
        doc = fh.read()
    assert "Enable-VmrestAutostart.ps1" in doc
    assert "../../scripts/Enable-VmrestAutostart.ps1" in doc, (
        "the script is named but not linked, so nobody can find the file")
    heading = [l for l in doc.splitlines() if l.startswith("### 1a.")]
    assert heading, f"the {_ANCHOR} section is gone — three surfaces link to it"


def test_the_agents_refusal_points_at_that_section():
    """"could not reach vmrest" is the message an operator sees at the moment this
    happens to them, and after a reboot it is nearly always this and not the network."""
    with open(os.path.join(_ROOT, "runners", "agent", "agent.py"), encoding="utf-8") as fh:
        agent = fh.read()
    assert "could not reach vmrest" in agent
    assert _ANCHOR in agent, "the refusal no longer names the autostart section"


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
