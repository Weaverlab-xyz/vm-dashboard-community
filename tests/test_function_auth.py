"""Cloud Functions: shared-secret auth, redaction, and dispatch.

These are the security-critical properties of the runtime. The three that matter
most and are easiest to break in a two-line change:

  * auth fails CLOSED when FN_SHARED_SECRET is unset (500, never 200)
  * "no credential" and "wrong credential" are byte-identical 401s
  * the credential never reaches a log line

Stdlib only — runs with nothing installed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import auth, dispatch, logs
from fnruntime.contract import Context, Request, Response

_ENV_KEYS = ("FN_SHARED_SECRET", "FN_AUTH_HEADER", "FN_AUTH_PREFIX",
             "FN_DEBUG", "FN_LOG_BODY")


def _reset_env(**overrides):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    for key, val in overrides.items():
        os.environ[key] = val


def _req(headers=None, body=b"", source="aws_function_url"):
    return Request(method="POST", path="/", headers=headers or {},
                   query={}, body=body, source=source)


# ── Fail closed ───────────────────────────────────────────────────────────────

def test_missing_secret_fails_closed_with_500():
    """The single worst bug this feature could ship: no configured secret must
    NEVER produce a working open endpoint."""
    _reset_env()
    resp = auth.verify(_req({"authorization": "Bearer anything"}))
    assert resp is not None, "unconfigured function authorized a request"
    assert resp.status == 500, resp.status


def test_blank_secret_also_fails_closed():
    _reset_env(FN_SHARED_SECRET="   ")
    resp = auth.verify(_req({"authorization": "Bearer    "}))
    assert resp is not None and resp.status == 500, resp


# ── Accept / reject ───────────────────────────────────────────────────────────

def test_correct_bearer_is_authorized():
    _reset_env(FN_SHARED_SECRET="topsecret")
    assert auth.verify(_req({"authorization": "Bearer topsecret"})) is None


def test_missing_and_wrong_credentials_are_indistinguishable():
    """Probing must not tell an attacker whether a credential was recognised."""
    _reset_env(FN_SHARED_SECRET="topsecret")
    missing = auth.verify(_req({}))
    wrong = auth.verify(_req({"authorization": "Bearer nope"}))
    assert missing.status == wrong.status == 401
    assert missing.rendered()[0] == wrong.rendered()[0], "401 bodies differ"


def test_near_miss_secrets_are_rejected():
    _reset_env(FN_SHARED_SECRET="topsecret")
    for bad in ("topsecre", "topsecrett", "TOPSECRET", "", "Bearer topsecret"):
        resp = auth.verify(_req({"authorization": f"Bearer {bad}"}))
        assert resp is not None and resp.status == 401, f"accepted {bad!r}"


def test_custom_header_and_prefix():
    _reset_env(FN_SHARED_SECRET="abc", FN_AUTH_HEADER="X-Dashboard-Secret",
               FN_AUTH_PREFIX="")
    assert auth.verify(_req({"x-dashboard-secret": "abc"})) is None
    assert auth.verify(_req({"authorization": "Bearer abc"})) is not None


def test_direct_invoke_may_carry_the_secret_in_the_body():
    """Scheduled invokes never traverse the HTTP front door, so they have no
    headers at all — but ONLY that source may use the body."""
    _reset_env(FN_SHARED_SECRET="sched")
    body = json.dumps({"secret": "sched"}).encode()
    assert auth.verify(_req(body=body, source="aws_direct")) is None
    # An HTTP caller cannot smuggle it in the body.
    resp = auth.verify(_req(body=body, source="aws_function_url"))
    assert resp is not None and resp.status == 401


# ── Redaction ─────────────────────────────────────────────────────────────────

def test_redact_masks_exact_and_substring_keys():
    out = logs.redact({
        "authorization": "Bearer abc", "db_password": "hunter2",
        "azure_client_secret": "s", "bearer_token": "t", "credential": "c",
        "username": "alice", "public_key": "ssh-rsa AAA", "port": 5432,
    })
    for key in ("authorization", "db_password", "azure_client_secret",
                "bearer_token", "credential"):
        assert out[key] == "***", f"{key} was not redacted: {out[key]!r}"
    # Must NOT over-redact, or people learn to ignore the output.
    assert out["username"] == "alice"
    assert out["public_key"] == "ssh-rsa AAA"
    assert out["port"] == 5432


def test_redact_reaches_into_nested_structures():
    out = logs.redact({"outer": {"inner": [{"password": "p"}]}})
    assert out["outer"]["inner"][0]["password"] == "***"


def test_redact_truncates_and_never_raises():
    assert "truncated" in logs.redact("x" * 5000)
    deep = cur = {}
    for _ in range(50):
        cur["next"] = {}
        cur = cur["next"]
    logs.redact(deep)                       # must not blow the stack
    assert logs.redact(b"1234") == "<4 bytes>"
    assert len(logs.redact(list(range(500)))) <= 51


def test_redact_does_not_mutate_the_input():
    original = {"password": "p", "nested": {"token": "t"}}
    logs.redact(original)
    assert original == {"password": "p", "nested": {"token": "t"}}


# ── Dispatch ──────────────────────────────────────────────────────────────────

class _Workload:
    NAME = "fake"

    @staticmethod
    def handle(req, ctx):
        return Response(200, {"seen": req.json()})


class _Exploding:
    NAME = "boom"

    @staticmethod
    def handle(req, ctx):
        raise RuntimeError("secret-bearing failure: password=hunter2")


class _BareDict:
    NAME = "bare"

    @staticmethod
    def handle(req, ctx):
        return {"not": "a Response"}


def test_dispatch_denies_before_running_the_workload():
    _reset_env()  # unconfigured → 500 from auth, workload must not run
    called = []

    class _Tracker:
        NAME = "t"

        @staticmethod
        def handle(req, ctx):
            called.append(1)
            return Response(200, {})

    resp = dispatch.handle_request(_req({"authorization": "Bearer x"}), _Tracker)
    assert resp.status == 500 and not called, "workload ran despite auth failure"


def test_dispatch_runs_the_workload_when_authorized():
    _reset_env(FN_SHARED_SECRET="ok")
    resp = dispatch.handle_request(
        _req({"authorization": "Bearer ok"}, body=b'{"a":1}'), _Workload)
    assert resp.status == 200 and resp.body == {"seen": {"a": 1}}


def test_dispatch_converts_a_workload_crash_into_a_clean_500():
    """The caller gets a request id, never the exception text."""
    _reset_env(FN_SHARED_SECRET="ok")
    resp = dispatch.handle_request(_req({"authorization": "Bearer ok"}), _Exploding)
    assert resp.status == 500
    rendered = resp.rendered()[0]
    assert "hunter2" not in rendered and "RuntimeError" not in rendered, rendered
    assert "request_id" in resp.body


def test_dispatch_wraps_a_bare_return_value():
    _reset_env(FN_SHARED_SECRET="ok")
    resp = dispatch.handle_request(_req({"authorization": "Bearer ok"}), _BareDict)
    assert isinstance(resp, Response) and resp.status == 200


def test_the_secret_never_reaches_a_log_line():
    """End-to-end over the real emit() path, capturing stdout."""
    import io
    import contextlib
    _reset_env(FN_SHARED_SECRET="topsecret", FN_LOG_BODY="1")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        dispatch.handle_request(
            _req({"authorization": "Bearer topsecret"},
                 body=json.dumps({"password": "hunter2", "user": "alice"}).encode()),
            _Workload)
        dispatch.handle_request(_req({"authorization": "Bearer topsecret-wrong"}),
                                _Workload)
    output = buf.getvalue()
    assert output.strip(), "nothing was logged"
    assert "topsecret" not in output, output
    assert "hunter2" not in output, output
    assert "alice" in output, "redaction ate the useful fields too"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
