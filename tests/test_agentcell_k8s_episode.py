"""The cluster-access episode: ask, wait for a person, prove the scope, give it back.

The agent cell's second demo, and the one whose closing beat is an agent that **cannot
authorise its own access**. Three properties carry it, and each one has a way of failing
that leaves the demo *looking* like it works — which is the failure this whole cell exists
to argue against:

  * **Waiting is visible.** An agent that silently slept until approval would look
    identical to one that was never gated. The state is named and reported.
  * **Nothing is checked in while pending.** `ps_api_service._request_credential` checks
    in when the credential does not come back — right for an auto-release policy, and
    exactly wrong here: it would cancel the very request a human is being asked to
    approve. But the slot *is* returned on timeout, or the next attempt trips the
    concurrent cap and reports the cap instead of the approval it waited on.
  * **The refusal must refuse with 403.** A wrong API server, an expired token or a typo
    also fail, so a probe asserting only "it failed" would pass on any of them. The
    shipped plays encode this trap; so does this.

Runs under pytest, or standalone:
    python tests/test_agentcell_k8s_episode.py
"""
import http.server
import importlib.util
import json
import os
import re
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-episode")

_WORKER = os.path.join(_ROOT, "examples", "playbooks", "agent", "files", "mcp_agent.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "agentcell.py")
_DB = os.path.join(_ROOT, "web_dashboard", "database.py")

from web_dashboard.services import agentcell_service as A  # noqa: E402

_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW06c2E6YXBwOmNpIn0.sigsig"
_SOFT = "It was not possible to get a credential for Request ID: 77"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path):
    """Source with docstrings and comments stripped. These files argue for their own
    design at length, so an absence check on the raw text finds the prose."""
    src = re.sub(r'"""[\s\S]*?"""', "", _read(path))
    return "\n".join(ln for ln in src.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))


def _worker():
    spec = importlib.util.spec_from_file_location("mcp_agent", _WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _FakePS(http.server.BaseHTTPRequestHandler):
    """Password Safe, enough of it. `pending_polls` decides how long approval takes."""
    pending_polls = 2
    state = None

    def _send(self, code, body=b"{}"):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.endswith("Auth/Connect/Token"):
            return self._send(200, json.dumps({"access_token": "B"}).encode())
        if self.path.endswith("Auth/SignAppIn"):
            return self._send(200)
        if self.path.endswith("/Requests"):
            return self._send(201, b"77")
        self._send(404)

    def do_GET(self):
        if "Credentials/" in self.path:
            self.state["polls"] += 1
            if self.state["polls"] <= self.pending_polls:
                return self._send(200, json.dumps(_SOFT).encode())
            return self._send(200, json.dumps(_JWT).encode())
        self._send(404)

    def do_PUT(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.state["checkins"].append(self.path)
        self._send(200)

    def log_message(self, *a):
        pass


def _serve(handler_cls, state):
    cls = type("H", (handler_cls,), {"state": state})
    srv = http.server.HTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


# -- trap 1: "awaiting approval" is a sentence, not a status code --------------

def test_the_soft_failure_sentence_is_read_as_not_yet():
    """services/btapi_service learned this the hard way: Password Safe can exit
    successfully and return "It was not possible to get a credential for Request ID: N"
    in the credential position. Treating that as a failure would make an approval-gated
    request unwaitable; treating it as a credential would hand the sentence to an API
    server as a bearer token."""
    m = _worker()
    state = {"polls": 0, "checkins": []}
    base, _ = _serve(_FakePS, state)
    token, _, _, rid, _polls = m.password_safe_episode(
        api_url=base, client_id="c", client_secret="s", account_id=42,
        reason="test", max_wait_seconds=60, poll_seconds=0)
    assert token == _JWT, "the credential did not come back after approval"
    assert rid == 77
    assert state["polls"] > 2, "it did not wait — the pending polls were not seen"


def test_waiting_is_reported_on_every_poll():
    """A silent sleep looks identical to never having been gated. The waiting IS the
    demo, so it has to reach the journal."""
    m = _worker()
    state = {"polls": 0, "checkins": []}
    base, _ = _serve(_FakePS, state)
    seen = []
    m.password_safe_episode(api_url=base, client_id="c", client_secret="s",
                            account_id=42, reason="test", max_wait_seconds=60,
                            poll_seconds=0, on_wait=seen.append)
    assert len(seen) == 2, f"expected a line per pending poll, got {seen}"
    runner = _code(_WORKER).split("def run_k8s_episode(", 1)[1].split("\ndef ")[0]
    assert "WAITING" in runner and "cannot authorise its own access" in runner, \
        "the worker does not say what the waiting means"


# -- trap 2: never check in while a human is being asked -----------------------

def test_nothing_is_checked_in_while_the_request_is_pending():
    """The one that would quietly ruin it: checking in cancels the very request somebody
    is being asked to approve, and the agent would then report a denial that never
    happened."""
    m = _worker()
    state = {"polls": 0, "checkins": []}
    base, _ = _serve(_FakePS, state)
    m.password_safe_episode(api_url=base, client_id="c", client_secret="s",
                            account_id=42, reason="test", max_wait_seconds=60,
                            poll_seconds=0)
    assert state["checkins"] == [], (
        "the episode checked in while pending — that cancels the request a human is "
        f"being asked to approve: {state['checkins']}")


def test_the_slot_is_returned_when_approval_never_comes():
    """And it must give up. An abandoned request holds the account's concurrent slot for
    its whole duration, so the next attempt fails on the cap (4035) reporting a cap
    problem instead of the approval it was waiting on."""
    m = _worker()
    state = {"polls": 0, "checkins": []}
    never = type("Never", (_FakePS,), {"pending_polls": 10 ** 6})
    base, _ = _serve(never, state)
    token, _, _, rid, _polls = m.password_safe_episode(
        api_url=base, client_id="c", client_secret="s", account_id=42,
        reason="test", max_wait_seconds=0, poll_seconds=0)
    assert token == "", "a token came back from a request nobody approved"
    assert len(state["checkins"]) == 1, \
        "the timeout did not return the slot — the next attempt will trip the cap"


def test_the_episode_gives_up_rather_than_waiting_forever():
    m = _worker()
    body = _code(_WORKER).split("def password_safe_episode(", 1)[1].split("\ndef ")[0]
    assert "max_wait_seconds" in body and "waited >= max_wait_seconds" in body, \
        "there is no bound on the wait"
    assert A.MAX_WAIT_MINUTES > 0 and A.DEFAULT_DURATION_MINUTES > 0


# -- trap 3: the credential is a JWT, not a PAT --------------------------------

def test_a_service_account_token_is_not_rejected_for_not_being_a_pat():
    m = _worker()
    assert m._looks_like_jwt(_JWT)
    assert not m._looks_like_jwt("vmcli_" + "a" * 64)
    assert not m._looks_like_jwt(_SOFT)


def test_the_episode_refuses_something_that_is_not_a_token():
    """If Password Safe releases the soft-failure sentence as though it were a value, the
    episode must stop rather than hand it to an API server and report a 401 as a scope
    result."""
    m = _worker()
    state = {"polls": 0, "checkins": []}

    class Bad(_FakePS):
        pending_polls = 0

        def do_GET(self):
            if "Credentials/" in self.path:
                return self._send(200, json.dumps("not-a-token").encode())
            self._send(404)

    base, _ = _serve(Bad, state)
    try:
        m.password_safe_episode(api_url=base, client_id="c", client_secret="s",
                                account_id=42, reason="test", max_wait_seconds=0,
                                poll_seconds=0)
    except SystemExit as exc:
        assert "ServiceAccount token" in str(exc)
    else:
        raise AssertionError("a non-token was accepted and would be spent as a bearer")
    assert len(state["checkins"]) == 1, "the slot was not returned on a bad release"


# -- the probes prove SCOPE, not that the token works --------------------------

class _Cluster(http.server.BaseHTTPRequestHandler):
    allow_path = "/api/v1/namespaces/app/pods"

    def do_GET(self):
        code = 200 if self.path == self.allow_path else 403
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def test_the_probe_proves_the_scope():
    m = _worker()
    base, _ = _serve(_Cluster, {})
    r = m.k8s_probe(api_server=base, token=_JWT, profile="deployer",
                    namespace="app", other_namespace="kube-system")
    assert r["allow_status"] == 200 and r["deny_status"] == 403
    assert r["proved"] is True
    assert "refused in kube-system" in m.probe_summary(r)


def test_a_refusal_that_does_not_refuse_is_the_loudest_failure():
    """The one outcome that would otherwise look like success: the token is broader than
    the profile claims, every request returns 200, and a weaker probe would report a
    passing demonstration of nothing."""
    m = _worker()

    class AllOpen(_Cluster):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    base, _ = _serve(AllOpen, {})
    r = m.k8s_probe(api_server=base, token=_JWT, profile="deployer",
                    namespace="app", other_namespace="kube-system")
    assert r["proved"] is False
    assert "THE REFUSAL DID NOT REFUSE" in m.probe_summary(r)


def test_a_broken_setup_is_not_reported_as_a_proved_scope():
    """A wrong API server or an expired token fails BOTH reads. A probe asserting only
    "the second one failed" would call that a pass."""
    m = _worker()

    class AllClosed(_Cluster):
        def do_GET(self):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"{}")

    base, _ = _serve(AllClosed, {})
    r = m.k8s_probe(api_server=base, token=_JWT, profile="deployer",
                    namespace="app", other_namespace="kube-system")
    assert r["proved"] is False
    assert "proves nothing" in m.probe_summary(r)


def test_a_non_403_failure_on_the_deny_path_proves_nothing():
    """The trap docs/integrations/workload-kubernetes.md records the plays encoding: a
    refusal assertion that accepts *any* failure passes on an expired token, a wrong
    path or an unreachable server. Only 403 means "RBAC refused this". A 401 here means
    the token is bad, which makes the whole probe meaningless — and a weaker check would
    call it a proved scope."""
    m = _worker()

    class Unauthorized(_Cluster):
        def do_GET(self):
            code = 200 if self.path == self.allow_path else 401
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"{}")

    base, _ = _serve(Unauthorized, {})
    r = m.k8s_probe(api_server=base, token=_JWT, profile="deployer",
                    namespace="app", other_namespace="kube-system")
    assert r["allow_status"] == 200 and r["deny_status"] == 401
    assert r["proved"] is False, (
        "a 401 on the deny path was accepted as a refusal — that is a broken token, "
        "not RBAC working")


def test_both_profiles_have_a_refusal_to_prove():
    """Per docs/integrations/workload-kubernetes.md: steps 3 and 4 — the refusals — are
    the ones that prove something. A profile with only a success path proves the token
    works, which is the half that was never in doubt."""
    m = _worker()
    for profile in ("deployer", "reader"):
        spec = m.PROBES[profile]
        assert spec["allow"] and spec["deny"], f"{profile} has no refusal to prove"
        assert spec["allow"] != spec["deny"]
    assert "secrets" in m.PROBES["reader"]["deny"], \
        "the reader profile does not probe the Secret refusal `view` omits by design"


# -- the row records the request, never the credential -------------------------

def test_the_agent_row_holds_no_credential():
    """The rule the four lab models state, extended to the row that now fetches one.
    Checked on the COLUMN DECLARATIONS: the docstring explains at length why there is no
    credential column, so scanning the prose finds the words it promises the absence of.
    """
    db = _read(_DB)
    start = db.index("class AgentCell(Base):")
    end = db.index(chr(10) + "class ", start + 10)
    columns = [ln.strip() for ln in db[start:end].splitlines() if "= Column(" in ln]
    assert columns, "no column declarations found"
    declared = "\n".join(columns).lower()
    for banned in ("password", "kubeconfig", "bearer", "secret", "credential_value"):
        assert banned not in declared, (
            f"AgentCell declares a column matching {banned!r} — the row names what was "
            "requested, Password Safe holds the credential")
    assert "episode_state" in declared, "the row cannot report an episode at all"
    # And the two that are deliberately absent, because nothing can fill them: the
    # WORKER opens the request and runs the probes, so the dashboard never learns the id
    # or the outcome. A structurally always-NULL column reads as "nothing happened yet"
    # on a row where plenty did, and invites granting the worker authority to fill it.
    for absent in ("episode_request_id", "episode_result"):
        assert absent not in declared, (
            f"{absent} is back — nothing can write it, so it can only mislead")


def test_an_open_episode_blocks_a_second_and_an_unlink():
    class Row:
        episode_state = "waiting"
        linked_mechanism = "kubernetes"
    assert "already has a cluster-access request open" in A.episode_problem(Row())
    api = _code(_API)
    unlink = api.split("def unlink_agent(", 1)[1].split("\ndef ")[0]
    assert "episode_problem(" in unlink, \
        "unlinking mid-episode would orphan a request against an account the agent is " \
        "no longer answerable for"
    for closed in A.EPISODE_CLOSED:
        Row.episode_state = closed
        assert A.episode_problem(Row()) == "", f"{closed} should free the agent"


def test_the_row_does_not_pretend_to_track_the_workers_progress():
    """The worker cannot call back — its PAT belongs to a non-admin user and the episode
    routes need `config_mgmt:write`. Granting it that so it could file progress would
    give a non-human principal more authority than this cell argues for, which is a bad
    trade for a status field. So the row holds two states and the journal holds the rest,
    and the code has to say which is which rather than leaving the field looking live."""
    assert A.ROW_STATES == ("requested", "released")
    assert set(A.WORKER_STATES) == set(A.EPISODE_STATES) - set(A.ROW_STATES)
    api = _code(_API)
    written = {ln.split("=", 1)[1].strip().strip('"\'')
               for ln in api.splitlines() if "row.episode_state =" in ln}
    assert written == set(A.ROW_STATES), (
        f"the API writes {written}, which is not the set the row claims to hold — one of "
        "the two is wrong")
    db = _read(_DB)
    assert "the worker cannot report them back" in db or "cannot call back" in db or \
        "ROW_STATES" in db, "the column does not say why it holds only two states"


def test_an_episode_needs_a_spendable_link():
    class NoLink:
        linked_mechanism = ""
    class Cloud:
        linked_mechanism = "cloud"
    assert "not linked" in A.episode_link_problem(NoLink())
    msg = A.episode_link_problem(Cloud())
    assert "no worker can spend" in msg, \
        "a cloud link must not read as something the agent could request"


def test_the_request_reason_names_what_is_asking():
    """The audit row is the one place a human approving this sees WHAT is asking. A
    reason of "automated" would make the approval a rubber stamp."""
    reason = A.episode_reason("mcp-reader", "spiffe://weaverlab.test/agent/mcp-reader")
    assert "spiffe://" in reason
    assert A.episode_reason("mcp-reader", "") == "mcp-agent cluster access — mcp-reader"


def test_the_states_an_operator_has_to_tell_apart():
    for state, needle in (("waiting", "cannot authorise its own access"),
                          ("denied", "the mechanism working"),
                          ("expired", "the slot was given back")):
        class Row:
            episode_state = state
        assert needle in A.episode_summary(Row()), \
            f"the {state} summary does not say what that state means"


def test_a_denied_request_is_not_rendered_as_a_fault():
    """Same rule `workload_cloud_service.lease_state` states for an expired lease: the
    mechanism working must not look like something broken."""
    class Row:
        episode_state = "denied"
    assert "not a fault" in A.episode_summary(Row())


def test_the_duration_is_clamped_rather_than_honoured():
    """It is how long the account's slot stays held if nobody approves."""
    assert A.episode_duration_problem(1) == 5
    assert A.episode_duration_problem(10 ** 6) == A.MAX_WAIT_MINUTES * 2
    assert A.episode_duration_problem("nonsense") == A.DEFAULT_DURATION_MINUTES


# -- what the worker never logs ------------------------------------------------

def test_the_cluster_token_never_reaches_a_log():
    """An API server's error body is not this worker's decision either."""
    m = _worker()
    out = m.scrub(f"403 Forbidden: Authorization: Bearer {_JWT}")
    assert _JWT not in out and "redacted" in out


def test_the_episode_runner_logs_no_token():
    code = _code(_WORKER)
    runner = code.split("def run_k8s_episode(", 1)[1].split("\ndef ")[0]
    prints = [ln for ln in runner.splitlines() if "print(" in ln]
    assert prints, "the episode reports nothing"
    for ln in prints:
        assert "{token}" not in ln, f"a print carries the cluster token: {ln.strip()}"
    body = runner[runner.index("password_safe_episode"):]
    assert "{token}" not in body


def test_the_request_id_never_reaches_a_log():
    """The second time this repo has reached this conclusion — `ps_api_service._checkin`
    carries the same note, "never log the request id (CodeQL taints it)".

    The taint is real rather than pedantic: the id comes back from the same call as the
    credential, so an analyser cannot tell them apart and neither, at a glance, can a
    reader. Nothing is lost by dropping it — the handle that correlates this with the
    Password Safe audit row is the SPIFFE ID, which travels in the request's own reason
    and which Password Safe records. The log names what the other system shows rather
    than an internal id only this process can see.
    """
    code = _code(_WORKER)
    # Per episode, not across both: the slice used to run to `main` and silently widened
    # when a second episode landed between them, which turned an exact assertion into a
    # brittle one that a correct change could fail.
    for fn in ("run_k8s_episode", "run_cert_episode"):
        body = code.split(f"def {fn}(", 1)[1].split("\ndef ")[0]
        uses = [ln.strip() for ln in body.splitlines() if "request_id" in ln]
        assert uses and uses[0].endswith("password_safe_episode("), \
            f"{fn}: the request id does not come from the episode call: {uses}"
        # Everything after the call must be a check-in — the only thing that legitimately
        # SPENDS a request id. There are two now: the ungated-release refusal releases
        # the slot before it returns, which is a spend and not a log.
        assert all(u == "_checkin(base, headers, request_id, reason)" for u in uses[1:]), (
            f"{fn}: the request id is used for something other than a check-in: {uses}")
    # And nowhere in the module does one reach a print.
    for ln in code.splitlines():
        if "print(" in ln:
            assert "request_id" not in ln, f"a print carries the request id: {ln.strip()}"


def test_the_episode_says_what_the_checkin_does_not_do():
    """"Release" must not read as a revoke. Rotation does not revoke; a token already
    released lives out its TTL, and only deleting the ServiceAccount is a kill."""
    runner = _read(_WORKER)
    runner = runner[runner.index("def run_k8s_episode("):]
    runner = runner[:runner.index("\ndef main(")]
    assert "lives out its TTL" in runner and "ServiceAccount" in runner, \
        "the worker lets the check-in read as a revoke"
    api = _read(_API)
    release = api[api.index("def release_cluster_access("):]
    assert "it does not revoke anything" in release


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        # SystemExit is a BaseException, and this worker raises it as its fatal path --
        # `except Exception` would let one escape and end the run mid-file, which reads
        # as a pass because nothing prints a failure.
        except (Exception, SystemExit) as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
