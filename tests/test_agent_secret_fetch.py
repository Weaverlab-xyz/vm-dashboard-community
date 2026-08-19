"""The agent side of a dashboard-held credential: precedence, memoisation, and scrubbing.

The sibling of ``test_agent_ps_checkout.py``, which pins the precedence chain as it was.
This one covers the branch added for ``dashboard_secret`` and the three properties that
make it safe to add a *fourth* credential source to a function that thirteen call sites
reach:

  * **A remote source never silently falls back to a local literal.** That rule already
    existed for ``ps_managed_account``; breaking it for the new source would leave an
    operator who deleted their password still authenticating with one they thought was
    gone — or worse, believing the credential moved when it did not.
  * **One fetch per job.** ``_vmrest`` calls ``_secret_for`` on *every* HTTP call, so a
    Workstation job reads the credential many times over. Unmemoised, each of those is
    another Password Safe request for the dashboard to open and close.
  * **The credential never leaves the process.** ``execute`` ships ``str(exc)`` into the job
    row, where it is the only text a failed job renders, and an arbitrary library's
    exception message is not something this agent can vouch for.

The agent module is loaded by file path, like the other agent-side tests. Runs under
pytest, or standalone:
    python tests/test_agent_secret_fetch.py
"""
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.join(_ROOT, "runners", "agent", "agent.py")
_SEALING = os.path.join(_ROOT, "web_dashboard", "services", "agent_sealing.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    agent = _load("agent_runner", _AGENT)
    sealing = _load("agent_sealing", _SEALING)
except Exception as exc:  # pragma: no cover — deps missing
    try:
        import pytest
        pytest.skip(f"modules not importable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

AGENT_ID = "ag-1"
AUDIENCE = "https://agents.example.com"
SECRET = "vCenter!Adm1n#2026"


class _Identity:
    agent_id = AGENT_ID
    audience = AUDIENCE


class _FakeDashboard:
    """A Dashboard whose transport is replaced but whose real redaction is kept.

    The scrubbing methods are bound off the real class rather than reimplemented — a copy
    here could drift from the code under test and quietly stop asserting anything.
    """

    def __init__(self, *, secret=SECRET, status=200, detail=""):
        self.identity = _Identity()
        self._held = set()
        self.secret = secret
        self.status = status
        self.detail = detail
        self.fetches = []
        for name in ("hold_secret", "redact", "release_secrets", "_scrub"):
            setattr(self, name, getattr(agent.Dashboard, name).__get__(self))

    def job_secret(self, job_id, ref):
        """Mirrors the real method: seal with the SERVER implementation, open with the
        agent's vendored one, so the cross-implementation path is what gets exercised."""
        self.fetches.append((job_id, ref))
        if self.status != 200:
            raise agent.PolicyRefusal(
                f"the dashboard refused to release the credential for {ref!r} "
                f"({self.status}): {self.detail} — this is a dashboard-side authorization "
                f"or configuration refusal, not a policy.yaml or connections.yaml problem.")
        private, public = agent.generate_reply_keypair()
        envelope = sealing.seal(public, self.secret, agent_id=AGENT_ID,
                               audience=AUDIENCE, job_id=job_id, ref=ref)
        secret = agent.open_sealed(private, envelope, agent_id=AGENT_ID,
                                   audience=AUDIENCE, job_id=job_id, ref=ref)
        self.hold_secret(secret)
        return secret


def _ctx(dash, *, job_id="job-1", ref="dc1-vcenter"):
    return agent.JobSecrets(job_id=job_id, dashboard=dash, ref=ref)


# ── precedence ────────────────────────────────────────────────────────────────

def test_a_dashboard_secret_is_fetched_and_returned():
    dash = _FakeDashboard()
    conn = {"name": "dc1-vcenter", "dashboard_secret": True}
    assert agent._secret_for(conn, _ctx(dash)) == SECRET
    assert dash.fetches == [("job-1", "dc1-vcenter")]


def test_a_dashboard_secret_beats_a_literal_left_in_the_file():
    """The same rule `ps_managed_account` already follows. An operator who moved a
    connection to the dashboard must not keep authenticating with the stale password
    underneath it."""
    dash = _FakeDashboard()
    for stale in ({"password": "stale-local"}, {"password_file": "/run/secrets/hv"}):
        conn = {"name": "dc1-vcenter", "dashboard_secret": True, **stale}
        assert agent._secret_for(conn, _ctx(dash)) == SECRET


def test_two_remote_authorities_is_a_refusal_not_a_precedence():
    """`ps_managed_account` and `dashboard_secret` are different authorities — one has this
    agent ask Password Safe, the other has the dashboard do it. There is no stale-leftover
    story that makes picking one kind, and picking quietly leaves nobody able to say which
    credential a job actually used."""
    dash = _FakeDashboard()
    conn = {"name": "dc1-vcenter", "dashboard_secret": True, "ps_managed_account": 7}
    try:
        agent._secret_for(conn, _ctx(dash))
    except agent.PolicyRefusal as exc:
        assert "two different authorities" in str(exc)
        assert "remove one from connections.yaml" in str(exc)
        assert dash.fetches == [], "refused before any network call"
        return
    raise AssertionError("the agent chose between two remote credential authorities")


def test_a_falsy_dashboard_secret_is_treated_as_absent():
    """A YAML `dashboard_secret: false` — or an empty value — must fall through to the
    local credential rather than half-enabling the branch."""
    dash = _FakeDashboard()
    for falsy in (False, "", None, 0):
        conn = {"name": "c", "dashboard_secret": falsy, "password": "local"}
        assert agent._secret_for(conn, _ctx(dash)) == "local"
    assert dash.fetches == []


def test_the_local_chain_is_unchanged_when_the_key_is_absent():
    """The regression guard for the other thirteen call sites: adding a branch must not
    have disturbed the ordering `test_agent_ps_checkout` pins."""
    dash = _FakeDashboard()
    assert agent._secret_for({"name": "c", "password": "inline"}, _ctx(dash)) == "inline"
    # No credential at all is a refusal rather than `""` — see
    # tests/test_agent_local_seal.py for why an empty password is the dangerous answer.
    try:
        agent._secret_for({"name": "c"}, _ctx(dash))
    except agent.PolicyRefusal as exc:
        assert "declares no credential" in str(exc), exc
    else:
        raise AssertionError("an empty password must not be sent")


# ── memoisation ───────────────────────────────────────────────────────────────

def test_one_fetch_per_job_however_many_times_it_is_read():
    """`_vmrest` calls `_secret_for` on every HTTP call, so a Workstation job reads the
    credential repeatedly. Each unmemoised read would be another Password Safe request."""
    dash = _FakeDashboard()
    ctx = _ctx(dash)
    conn = {"name": "dc1-vcenter", "dashboard_secret": True}
    for _ in range(8):
        assert agent._secret_for(conn, ctx) == SECRET
    assert len(dash.fetches) == 1


def test_a_new_job_fetches_again():
    """Memoisation is per JobSecrets, which is per job — not process-wide. Module-level
    state here would also be a data race: jobs run concurrently up to
    `policy.limits.max_concurrency`."""
    dash = _FakeDashboard()
    conn = {"name": "dc1-vcenter", "dashboard_secret": True}
    agent._secret_for(conn, _ctx(dash, job_id="job-1"))
    agent._secret_for(conn, _ctx(dash, job_id="job-2"))
    assert [j for j, _ in dash.fetches] == ["job-1", "job-2"]


# ── refusals that have to name the remedy ─────────────────────────────────────

def test_a_dashboard_refusal_says_which_file_is_not_the_problem():
    """Every other refusal this agent produces points at policy.yaml or connections.yaml.
    Without this, the operator re-reads their own configuration first."""
    dash = _FakeDashboard(status=409, detail="the dashboard holds no credential for it")
    conn = {"name": "dc1-vcenter", "dashboard_secret": True}
    try:
        agent._secret_for(conn, _ctx(dash))
    except agent.PolicyRefusal as exc:
        assert "not a policy.yaml or connections.yaml problem" in str(exc)
        assert "the dashboard holds no credential" in str(exc)
        return
    raise AssertionError("a dashboard refusal did not surface")


def test_no_job_context_refuses_rather_than_falling_back():
    """Reached only through a code path with no leased job. Falling through to a local
    password here would be the silent-wrong-credential case."""
    conn = {"name": "c", "dashboard_secret": True, "password": "local"}
    try:
        agent._secret_for(conn, agent.JobSecrets())
    except agent.PolicyRefusal as exc:
        assert "outside a leased job" in str(exc)
        return
    raise AssertionError("fell back to a local password with no job context")


def test_a_plain_list_context_refuses_rather_than_falling_back():
    """The thirteen call sites thread `checkins`, which is a JobSecrets at runtime. If a
    future call site ever passes a bare list again, that must be loud — the alternative is
    silently using a local password the operator believes they deleted."""
    conn = {"name": "c", "dashboard_secret": True, "password": "local"}
    try:
        agent._secret_for(conn, [])
    except agent.PolicyRefusal as exc:
        assert "agent bug" in str(exc)
        return
    raise AssertionError("a bare list silently fell back to the local password")


def test_jobsecrets_is_still_a_list_so_the_checkin_path_works():
    """The liberty this design takes. Subclassing `list` is what keeps
    `checkins.append(request_id)`, `_checkin_all(checkins)` and thirteen signatures — and
    fourteen precedence tests — unchanged."""
    ctx = _ctx(_FakeDashboard())
    assert isinstance(ctx, list)
    ctx.append(4242)
    assert list(ctx) == [4242]
    agent._checkin_all([])          # the empty case must stay a cheap no-op


# ── scrubbing ─────────────────────────────────────────────────────────────────

def test_the_credential_is_scrubbed_from_everything_outbound():
    """`execute` ships `str(exc)` into the job row, and that is the only text a failed job
    renders. A URL with embedded credentials, or an XML-RPC fault echoing the request it
    failed on, both arrive there — so the filter is at the exit, not at each use site."""
    dash = _FakeDashboard()
    agent._secret_for({"name": "dc1-vcenter", "dashboard_secret": True}, _ctx(dash))

    leaky = {
        "status": "failed",
        "error": f"RuntimeError: 401 for https://administrator:{SECRET}@vc/api/session",
        "result": {"lines": [f"auth header built from {SECRET}"],
                   "nested": [{"deep": SECRET}]},
    }
    scrubbed = json.dumps(dash._scrub(leaky))
    assert SECRET not in scrubbed
    assert "redacted" in scrubbed
    # Everything else survives, or the redaction would be destroying diagnostics.
    assert "RuntimeError" in scrubbed and "vc/api/session" in scrubbed


def test_emitted_lines_are_redacted_before_they_are_logged_or_sent():
    dash = _FakeDashboard()
    agent._secret_for({"name": "dc1-vcenter", "dashboard_secret": True}, _ctx(dash))

    reporter = agent.Reporter.__new__(agent.Reporter)
    reporter.dashboard = dash
    reporter.job_id = "job-1"
    reporter.lines = []
    import threading
    reporter._lock = threading.Lock()
    reporter.emit(f"connecting as administrator with {SECRET}")
    assert reporter.lines and SECRET not in reporter.lines[0]


def test_the_progress_line_states_the_fact_and_not_the_value():
    """What the operator sees in Live Output while this happens."""
    src = open(_AGENT, encoding="utf-8").read()
    assert "nothing is stored on this host" in src
    assert "fetching the credential for" in src


def test_release_clears_the_scrub_set_between_jobs():
    """Scoped to the job, like the credential. Nothing here pretends to wipe it from
    memory — a Python str cannot be zeroed, and claiming otherwise would be theatre."""
    dash = _FakeDashboard()
    agent._secret_for({"name": "dc1-vcenter", "dashboard_secret": True}, _ctx(dash))
    assert SECRET not in dash.redact(f"x {SECRET} y")
    dash.release_secrets()
    assert dash.redact(f"x {SECRET} y") == f"x {SECRET} y"


def test_a_very_short_secret_is_not_registered_for_scrubbing():
    """A two-character credential would turn redaction into a search-and-destroy across
    every log line. Such a password cannot be protected this way and pretending otherwise
    would corrupt output; it is out of scope, not silently handled."""
    dash = _FakeDashboard(secret="ab")
    agent._secret_for({"name": "c", "dashboard_secret": True}, _ctx(dash))
    assert dash.redact("a table of absolutes") == "a table of absolutes"


def test_the_outbound_error_string_is_bounded_and_typed():
    """`execute` sends the exception type plus a shortened message, not the raw str()."""
    src = open(_AGENT, encoding="utf-8").read()
    assert 'error=f"{type(exc).__name__}: {str(exc)[:400]}"' in src
    assert 'error=str(exc)[:1000]' not in src, "the unbounded raw message is back"


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
