"""The agent's half of a dashboard-held SMB password: `dashboard_secret: true` in shares.yaml.

docs/design/dashboard-workload-identity.md, Slice 5. The agent fetches the password through
the same sealed per-job fetch a connection uses (``JobSecrets.dashboard_secret``). Pinned:

  * a share declaring it registers its SMB session with the FETCHED password, and a
    password left in shares.yaml is ignored — warned about, never a fallback;
  * one job is one fetch, however many SMB calls it makes;
  * a share without the key is untouched and fetches nothing;
  * a local path or a missing username refuses rather than guessing;
  * a quoted "false" in shares.yaml is a load error, not a truthy string;
  * the fetched password is registered for outbound redaction.

Runs under pytest, or standalone:
    python tests/test_agent_share_credential.py
"""
import importlib.util
import logging
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.join(_ROOT, "runners", "agent", "agent.py")
_SEALING = os.path.join(_ROOT, "web_dashboard", "services", "agent_sealing.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    agent = _load("agent_runner_share", _AGENT)
    sealing = _load("agent_sealing_share", _SEALING)
except ModuleNotFoundError as exc:  # pragma: no cover — deps missing
    try:
        import pytest
        pytest.skip(f"modules not importable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

AGENT_ID = "ag-1"
AUDIENCE = "https://agents.example.com"
PASSWORD = "Smb!Fetched#2026"
LEFTOVER = "old-local-password"
UNC = "\\\\fs01.corp.example.com\\automation\\playbooks"


class _Identity:
    agent_id = AGENT_ID
    audience = AUDIENCE


class _FakeDashboard:
    """Real redaction methods, transport replaced; seals with the server implementation
    and opens with the agent's, so the cross-implementation path is what runs."""

    def __init__(self):
        self.identity = _Identity()
        self._held = set()
        self.fetches = []
        for name in ("hold_secret", "redact", "release_secrets", "_scrub"):
            setattr(self, name, getattr(agent.Dashboard, name).__get__(self))

    def job_secret(self, job_id, ref):
        self.fetches.append((job_id, ref))
        private, public = agent.generate_reply_keypair()
        envelope = sealing.seal(public, PASSWORD, agent_id=AGENT_ID, audience=AUDIENCE,
                                job_id=job_id, ref=ref)
        secret = agent.open_sealed(private, envelope, agent_id=AGENT_ID, audience=AUDIENCE,
                                   job_id=job_id, ref=ref)
        self.hold_secret(secret)
        return secret


class _FakeSmb(types.ModuleType):
    """Stands in for smbclient: records every session registration, serves one file."""

    def __init__(self):
        super().__init__("smbclient")
        self.sessions = []

    def register_session(self, server, username=None, password=None):
        self.sessions.append((server, username, password))

    def scandir(self, base):
        return iter(())


class _Policy:
    def check_share(self, name, write=False):
        return None


def _shares_file(entry_yaml: str) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="shares-"), "shares.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("shares:\n" + entry_yaml)
    return path


def _run(entry_yaml: str, *, dash=None, job_id="job-1", op="list"):
    """Run a storage job against one shares.yaml entry named `s`, with a fake smbclient."""
    smb = _FakeSmb()
    sys.modules["smbclient"] = smb
    real = agent.SHARES_FILE
    agent.SHARES_FILE = _shares_file(entry_yaml)
    try:
        dash = dash or _FakeDashboard()
        agent.run_storage({"op": op, "share": "s"}, _Policy(), lambda line: None,
                          lambda: False, job_id, dash)
        return smb, dash
    finally:
        agent.SHARES_FILE = real
        sys.modules.pop("smbclient", None)


_UNC_ENTRY = (f"  - name: s\n    path: '{UNC}'\n    username: svc-dashboard\n"
              f"    dashboard_secret: true\n")


def _refused(entry_yaml: str) -> str:
    try:
        _run(entry_yaml)
    except agent.PolicyRefusal as exc:
        return str(exc)
    raise AssertionError("ran instead of refusing")


# ── the fetch ─────────────────────────────────────────────────────────────────

def test_the_session_uses_the_fetched_password():
    smb, dash = _run(_UNC_ENTRY)
    assert smb.sessions == [("fs01.corp.example.com", "svc-dashboard", PASSWORD)]
    assert dash.fetches == [("job-1", "s")], "the ref is the share's name"


def test_a_leftover_password_is_ignored_and_warned_about():
    records = []

    class _Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Catch()
    agent.log.addHandler(handler)
    try:
        smb, _ = _run(_UNC_ENTRY + f"    password: {LEFTOVER}\n")
    finally:
        agent.log.removeHandler(handler)
    assert smb.sessions[0][2] == PASSWORD and LEFTOVER not in str(smb.sessions)
    assert any("IGNORED" in r for r in records)


def test_one_job_is_one_fetch():
    dash = _FakeDashboard()
    secrets = agent.JobSecrets(job_id="job-1", dashboard=dash, ref="s")
    share = {"name": "s", "path": UNC, "username": "u", "dashboard_secret": True}
    agent._share_credential(share, secrets)
    agent._share_credential(share, secrets)
    assert len(dash.fetches) == 1


def test_a_new_job_fetches_again():
    dash = _FakeDashboard()
    _run(_UNC_ENTRY, dash=dash, job_id="job-1")
    _run(_UNC_ENTRY, dash=dash, job_id="job-2")
    assert [j for j, _ in dash.fetches] == ["job-1", "job-2"]


def test_the_fetched_password_is_held_for_redaction():
    _, dash = _run(_UNC_ENTRY)
    assert PASSWORD not in dash.redact(f"error: logon failed for {PASSWORD}")


# ── unchanged without the key ─────────────────────────────────────────────────

def test_a_share_without_the_key_uses_its_own_password_and_fetches_nothing():
    smb, dash = _run(f"  - name: s\n    path: '{UNC}'\n    username: u\n"
                     f"    password: {LEFTOVER}\n")
    assert smb.sessions == [("fs01.corp.example.com", "u", LEFTOVER)]
    assert dash.fetches == []


# ── refusals ──────────────────────────────────────────────────────────────────

def test_a_local_path_refuses_the_key():
    msg = _refused("  - name: s\n    path: /srv/playbooks\n    dashboard_secret: true\n")
    assert "local" in msg and "dashboard_secret" in msg


def test_no_username_refuses():
    msg = _refused(f"  - name: s\n    path: '{UNC}'\n    dashboard_secret: true\n")
    assert "username" in msg


def test_a_quoted_false_is_a_load_error_not_a_truthy_string():
    try:
        _run(f"  - name: s\n    path: '{UNC}'\n    username: u\n    dashboard_secret: 'false'\n")
        raise AssertionError("a quoted 'false' was accepted")
    except agent.AgentFatal as exc:
        assert "unquoted" in str(exc)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
