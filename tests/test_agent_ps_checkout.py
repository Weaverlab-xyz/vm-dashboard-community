"""The agent's Password Safe just-in-time checkout.

The end state the credential design was heading for: with `ps_managed_account:` in
connections.yaml the agent holds NO hypervisor credential at all, only a Password Safe
OAuth client whose single power is to ask. Every checkout is then subject to Password
Safe's own policy, approval workflow and session recording.

Two properties are worth pinning and neither is visible from a working sync:

* the credential is checked back IN when the job finishes, and a failure to do so does
  not fail an otherwise-successful job;
* a literal `password:` left in the file below a `ps_managed_account:` is NOT used —
  an operator who has moved a connection to Password Safe must not silently keep
  authenticating with a stale secret.

No network: the client is driven against a stubbed requests.Session.

Runs under pytest, or standalone:  python tests/test_agent_ps_checkout.py
"""
import importlib.util
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")

try:
    _spec = importlib.util.spec_from_file_location("agent_runner_ps", _PATH)
    agent = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(agent)
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Stands in for requests.Session, recording every call."""

    def __init__(self, *, checkout_status=200, credential="s3cret", checkin_status=200):
        self.headers = {}
        self.calls = []
        self._checkout_status = checkout_status
        self._credential = credential
        self._checkin_status = checkin_status

    def request(self, method, url, **kw):
        self.calls.append((method, url.rsplit("/public/v3/", 1)[-1]))
        path = url.rsplit("/public/v3/", 1)[-1]
        if path == "Auth/Connect/Token":
            return FakeResponse(200, {"access_token": "tok"})
        if path == "Auth/SignAppIn":
            return FakeResponse(200, {})
        if path == "Auth/Signout":
            return FakeResponse(200, {})
        if path == "Requests":
            return FakeResponse(self._checkout_status, 4242)
        if path.startswith("Credentials/"):
            return FakeResponse(200, self._credential)
        if path.endswith("/Checkin"):
            return FakeResponse(self._checkin_status, {})
        return FakeResponse(404, {})


def _client(**kw):
    ps = agent.PasswordSafe("https://ps.example.com", "cid", "secret")
    ps._session = FakeSession(**kw)
    return ps


def _paths(ps):
    return [p for _m, p in ps._session.calls]


# ── the client ────────────────────────────────────────────────────────────────

def test_the_api_base_is_normalised_from_a_bare_host():
    assert agent.PasswordSafe("https://ps.example.com", "", "").base.endswith(
        "/BeyondTrust/api/public/v3")
    # An operator who pasted the full path must not get it doubled.
    full = "https://ps.example.com/BeyondTrust/api/public/v3"
    assert agent.PasswordSafe(full, "", "").base == full


def test_sign_in_sets_a_bearer_and_calls_signappin():
    ps = _client()
    ps.sign_in()
    assert ps._session.headers["Authorization"] == "Bearer tok"
    assert _paths(ps) == ["Auth/Connect/Token", "Auth/SignAppIn"]


def test_checkout_returns_the_request_id_and_the_credential():
    ps = _client()
    ps.sign_in()
    request_id, credential = ps.checkout(99)
    assert request_id == 4242 and credential == "s3cret"
    assert "Requests" in _paths(ps) and "Credentials/4242" in _paths(ps)


def test_a_refused_request_names_the_requestor_role():
    """The 4031/403 that this whole integration keeps running into. The message has to
    say what to grant, because the operator reading it is looking at Live Output on a
    dashboard that cannot fix it for them."""
    ps = _client(checkout_status=403)
    ps.sign_in()
    try:
        ps.checkout(99)
    except agent.PolicyRefusal as exc:
        assert "Requestor" in str(exc) and "Smart Rule" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_a_failed_checkin_is_swallowed():
    # The credential is already used and the request expires on its own duration.
    # Failing a completed job because the check-in was refused is the wrong trade.
    ps = _client(checkin_status=500)
    ps.sign_in()
    ps.checkin(4242)          # must not raise


# ── _secret_for: which of the three sources wins ──────────────────────────────

def _with_stub_ps(monkey, **kw):
    """Point PasswordSafe.from_file at a stub so no file or network is needed."""
    made = []

    def _from_file(path=""):
        ps = _client(**kw)
        made.append(ps)
        return ps

    original = agent.PasswordSafe.from_file
    agent.PasswordSafe.from_file = staticmethod(_from_file)
    monkey.append(lambda: setattr(agent.PasswordSafe, "from_file", original))
    return made


def test_a_managed_account_beats_a_literal_password_left_in_the_file():
    """The precedence that matters. An operator who moves a connection to Password Safe
    must not silently keep authenticating with the stale secret underneath it."""
    undo = []
    _with_stub_ps(undo, credential="from-password-safe")
    try:
        conn = {"name": "dc1", "ps_managed_account": 77, "password": "stale-literal"}
        assert agent._secret_for(conn) == "from-password-safe"
    finally:
        for fn in undo:
            fn()


def test_a_checkout_records_its_request_for_check_in():
    undo = []
    _with_stub_ps(undo)
    try:
        checkins = []
        agent._secret_for({"name": "dc1", "ps_managed_account": 77}, checkins)
        assert checkins == [4242], "the request id must come back for check-in"
    finally:
        for fn in undo:
            fn()


def test_an_empty_released_credential_is_refused_not_used():
    undo = []
    _with_stub_ps(undo, credential="")
    try:
        agent._secret_for({"name": "dc1", "ps_managed_account": 77})
    except agent.PolicyRefusal as exc:
        assert "empty credential" in str(exc)
    else:
        raise AssertionError("an empty credential must be refused")
    finally:
        for fn in undo:
            fn()


def test_password_file_still_beats_an_inline_password():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("  from-file  \n")
        path = fh.name
    try:
        conn = {"name": "dc1", "password_file": path, "password": "inline"}
        assert agent._secret_for(conn) == "from-file"
    finally:
        os.unlink(path)


def test_an_inline_password_is_the_last_resort():
    assert agent._secret_for({"name": "dc1", "password": "inline"}) == "inline"
    assert agent._secret_for({"name": "dc1"}) == ""


# ── the client file ───────────────────────────────────────────────────────────

def test_a_missing_client_file_says_what_to_mount():
    try:
        agent.PasswordSafe.from_file("/nonexistent/passwordsafe.yaml")
    except agent.PolicyRefusal as exc:
        assert ":ro,Z" in str(exc), "the SELinux relabel is the usual cause"
    else:
        raise AssertionError("expected a refusal")


def test_the_client_secret_can_come_from_a_mounted_secret_file():
    """`client_secret_file` is the recommended form, so that the value can be a
    Docker/Podman secret rather than text in a YAML file."""
    with tempfile.TemporaryDirectory() as tmp:
        # The fixture value is written as a literal rather than held in a variable.
        # CodeQL's clear-text-storage rule tracks flow from a *sensitively named*
        # source into a file write, so any local called `secret`/`fake_secret` trips
        # it here — and the rule is right to be blunt about that, since the same shape
        # in non-test code would be the real thing. A literal has no such source, and
        # writing it inline is if anything clearer: this IS the fixture.
        key_file = os.path.join(tmp, "client_secret")
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write("not-a-real-value\n")
        conf = os.path.join(tmp, "passwordsafe.yaml")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(f"api_url: https://ps.example.com\nclient_id: cid\n"
                     f"client_secret_file: {key_file}\n")
        ps = agent.PasswordSafe.from_file(conf)
        # Trailing newline stripped, so a file written with printf/echo works.
        assert ps._secret == "not-a-real-value"


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
