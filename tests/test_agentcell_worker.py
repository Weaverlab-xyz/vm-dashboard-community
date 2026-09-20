"""The worker's contract: what it logs, what it holds, and how it ends.

The worker is the only part of this feature an audience actually looks at, and three of
its properties carry the whole demo. Each is easy to lose to a well-meaning edit:

  * **One line, both halves.** The SPIFFE ID it proved and the token it spent, together.
    Split them across two lines and the point — that these are two different things —
    stops being visible.
  * **The token is never logged in full.** A hint locates the row in Settings → API
    Tokens; the value would hand it to anyone watching the screen share.
  * **A refusal ends the process.** Exit 2 on 401, and a unit that does not restart on
    it. A worker that logged a warning and kept polling would turn the demo's closing
    beat into a scroll of failures.

Also pinned: the worker re-fetches its SVID rather than caching it, because "an attested
workload holds nothing" is the claim being made, and a cached ID would survive the
registration entry being deleted.

Runs under pytest, or standalone:
    python tests/test_agentcell_worker.py
"""
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-worker")

_DIR = os.path.join(_ROOT, "examples", "playbooks", "agent")
_WORKER = os.path.join(_DIR, "files", "mcp_agent.py")
_INSTALL = os.path.join(_DIR, "agent-install.yml")
_ENTRY = os.path.join(_DIR, "agent-spiffe-entry.yml")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path):
    """Source with docstrings and comments stripped — the worker explains its own design
    at length, so an absence check on the raw file finds the prose."""
    src = re.sub(r'"""[\s\S]*?"""', "", _read(path))
    return "\n".join(ln for ln in src.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#"))


def _yaml_code(path):
    """A playbook with its comment lines dropped.

    Same trap as _code, in YAML. These playbooks argue for their own settings in comments
    — "NOT Restart=always", "an Environment= line is world-readable" — so a raw scan finds
    the argument and reports it as the violation it is arguing against.
    """
    return "\n".join(ln for ln in _read(path).splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#"))


# -- it runs at all ------------------------------------------------------------

def test_the_worker_parses_and_selftests():
    r = subprocess.run([sys.executable, _WORKER, "--selftest"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"--selftest failed: {r.stderr}"
    assert "selftest ok" in r.stdout


def test_the_worker_refuses_to_start_without_a_url():
    r = subprocess.run([sys.executable, _WORKER], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0, "the worker started with no MCP endpoint"
    assert "--url" in (r.stderr + r.stdout)


# -- the log line is the demo --------------------------------------------------

def test_the_log_line_carries_both_the_identity_and_the_token():
    code = _code(_WORKER)
    line = [ln for ln in code.splitlines() if "spiffe_id" in ln and "hint" in ln]
    assert line, (
        "no log statement carries both the SPIFFE ID and the token hint. Splitting them "
        "across lines hides that identity and authorization are two different things, "
        "which is the whole argument of the cell")


def test_the_token_is_never_logged_in_full():
    # Scoped to PRINT statements. The worker legitimately interpolates the token into an
    # Authorization header; what must never happen is it reaching stdout.
    prints = [ln for ln in _code(_WORKER).splitlines() if "print(" in ln]
    assert prints, "the worker prints nothing at all"
    for ln in prints:
        assert "{token}" not in ln and "token)" not in ln.replace("token_file)", ""), \
            f"a print statement carries the raw token: {ln.strip()}"
    assert "def token_hint" in _code(_WORKER), \
        "the worker no longer truncates the token for logs"


def test_the_hint_is_too_short_to_use():
    sys.path.insert(0, os.path.dirname(_WORKER))
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_agent", _WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    raw = "vmcli_" + ("a" * 64)
    hint = m.token_hint(raw)
    assert raw not in hint, "the hint contains the whole token"
    assert len(hint) < 20, f"the hint is long enough to be worth guessing from: {hint}"
    assert hint.startswith("vmcli_"), "the hint does not identify the token's kind"


# -- identity is re-proved, never cached --------------------------------------

def test_the_svid_is_fetched_every_loop():
    code = _code(_WORKER)
    loop = code.split("def run(", 1)[1]
    assert "fetch_spiffe_id" in loop, (
        "the worker no longer fetches its SVID inside the loop. A cached ID would "
        "survive its registration entry being deleted, which is the opposite of what "
        "'an attested workload holds nothing' means")


def test_an_unattested_worker_says_so_rather_than_guessing():
    code = _code(_WORKER)
    assert '"unattested"' in code, \
        "the worker no longer reports being unattested; it would log a blank or a stale ID"


# -- the ending ----------------------------------------------------------------

def test_a_refused_token_ends_the_process():
    code = _code(_WORKER)
    run_body = code.split("def run(", 1)[1]
    assert "return 2" in run_body, (
        "the worker no longer exits on a refused token — the revoke would become a "
        "scroll of failures rather than the demo's closing beat")
    assert "401" in run_body, "the worker does not recognise a 401"


def test_the_unit_does_not_restart_on_a_revoke():
    unit = _yaml_code(_INSTALL)
    assert "SuccessExitStatus=2" in unit, (
        "the systemd unit does not treat exit 2 as a stop, so a revoked worker would be "
        "restarted in a loop and the demo's ending would be invisible")
    assert "Restart=always" not in unit, "the unit restarts unconditionally"


def test_the_unit_keeps_tmp_shared():
    """The SPIRE workload API socket lives under /tmp — the trap spire-agent-install.yml
    documents for the agent itself."""
    assert "PrivateTmp=no" in _yaml_code(_INSTALL), \
        "the unit gets a private /tmp, so the SPIRE socket would be invisible"


# -- the token's handling on the host -----------------------------------------

def test_the_token_lands_in_a_file_not_the_environment():
    unit = _yaml_code(_INSTALL)
    assert "mode: \"0600\"" in unit, "the token file is not 0600"
    assert "no_log: true" in unit, "the token would be echoed in the Ansible run output"
    assert "Environment=" not in unit, (
        "the token is passed through the systemd environment, which is readable via "
        "`systemctl show` and /proc/<pid>/environ")


def test_the_install_refuses_without_a_token_or_a_url():
    unit = _read(_INSTALL)
    assert "assert" in unit and "agent_token is defined" in unit, \
        "the install playbook does not refuse a run that would produce a useless worker"


# -- the registration entry ----------------------------------------------------

def test_the_entry_is_selected_by_uid_not_by_path():
    entry = _yaml_code(_ENTRY)
    assert "unix:uid:" in entry, "the registration entry no longer selects on uid"
    assert "unix:path:" not in entry, (
        "the entry selects on a path, so anything able to write there inherits the "
        "worker's identity")


def test_the_entry_play_is_rerunnable():
    entry = _read(_ENTRY)
    assert "similar entry already exists" in entry, \
        "a second run of the entry play would fail on a duplicate rather than no-op"


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
