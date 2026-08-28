"""The Skytap connection check, and the shape that keeps it out of a stack trace.

Until now the only way to find out whether Skytap credentials worked was to open the POV
page and read a 502 — which is how a blank POV page once cost an afternoon before the real
cause turned out to be somewhere else entirely. `configured()` is a presence check that
never contacts Skytap, so it cannot tell you anything about the token.

What is pinned here:

  * **`verify()` returns `(ok, message)` and does not raise for an expected outcome.** That
    is structural, not stylistic: CodeQL `py/stack-trace-exposure` fires on any
    `str(caught_exception)` reaching a response body, and it took three rounds on #646/#647
    to learn that a refusal has to be a RETURN VALUE. A test that only checked the message
    would not notice someone "simplifying" this back into raising.
  * **An account that authenticates but exposes no templates is RED.** A POV cannot be
    created from a catalogue of zero, so green there is the false positive a verify exists
    to prevent.
  * **It reads ONE page.** `_client().list()` would walk the whole catalogue at 100 rows a
    page to answer a yes/no question.
  * **A transport failure never echoes the exception's text**, only its type — a caught
    exception's `str()` can carry local paths and resolved addresses, and this one lands in
    an HTTP response body.

Uses httpx.MockTransport against the `skytap_service._client` seam. No network, no app,
no database.

Runs under pytest, or standalone:
    python tests/test_skytap_verify.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-skytap-verify")

try:
    import httpx
except ImportError:  # pragma: no cover
    print("SKIP: httpx not installed")
    sys.exit(0)

from web_dashboard.services import skytap_service as sk  # noqa: E402


class _Recorder:
    def __init__(self):
        self.requests = []


def _patch(handler, *, project="", configured=True):
    """Point the adapter at a canned handler and a chosen project id.

    Patches `_cfg` rather than writing config rows: the suite shares a real `vm_cli.db` and
    the developer's `.env`, so a test that writes config mutates the dev install — the trap
    `test_pov_broker` fell into by pinning a fake `agent_base_url`.
    """
    rec = _Recorder()

    def _wrapped(request):
        rec.requests.append(request)
        return handler(request)

    original_cfg, original_cls = sk._cfg, sk.SkytapClient

    def _cfg(key):
        if key == "skytap_project_id":
            return project
        if not configured:
            return ""
        return {"skytap_username": "u", "skytap_api_token": "t",
                "skytap_base_url": "https://skytap.test"}.get(key, "")

    async def _sleep(_s):
        pass

    def _cls(creds, **kw):
        kw.pop("transport", None)
        kw.setdefault("sleep", _sleep)
        return original_cls(creds, transport=httpx.MockTransport(_wrapped), **kw)

    sk._cfg, sk.SkytapClient = _cfg, _cls
    return rec, (original_cfg, original_cls)


def _restore(saved):
    sk._cfg, sk.SkytapClient = saved


def _verify(handler, **kw):
    rec, saved = _patch(handler, **kw)
    try:
        return asyncio.run(sk.verify()), rec
    finally:
        _restore(saved)


# ── the outcomes ─────────────────────────────────────────────────────────────

def test_an_unconfigured_skytap_is_an_answer_not_an_exception():
    """`SkytapClient.__init__` raises on invalid credentials, so this has to be checked
    BEFORE a client is built — otherwise the most ordinary state on a fresh install
    arrives as a traceback."""
    (ok, msg), _ = _verify(lambda r: httpx.Response(200, json=[]), configured=False)
    assert ok is False
    assert "not configured" in msg.lower()


def test_a_401_names_the_api_token_rather_than_the_password():
    """The single most common Skytap mistake. The client already authored this message;
    verify passes it through because it is a typed, fully-authored error."""
    (ok, msg), _ = _verify(lambda r: httpx.Response(401, json={}))
    assert ok is False
    assert "api token" in msg.lower() and "password" in msg.lower()


def test_a_403_is_reported_as_a_credential_problem():
    (ok, msg), _ = _verify(lambda r: httpx.Response(403, json={}))
    assert ok is False and "credential" in msg.lower()


def test_an_account_that_can_see_no_templates_is_not_green():
    """A POV cannot be built from zero templates — `api/pov.provision` refuses before
    anything exists. Reporting green here is the false positive the check exists to
    prevent."""
    (ok, msg), _ = _verify(lambda r: httpx.Response(200, json=[]))
    assert ok is False, "an empty catalogue was reported as a working connection"
    assert "no templates" in msg.lower()
    assert "credentials work" in msg.lower(), "it should say the token itself was fine"


def test_a_visible_template_is_green():
    (ok, msg), _ = _verify(
        lambda r: httpx.Response(200, json=[{"id": "1", "name": "poc-base"}]))
    assert ok is True, msg
    assert "connected" in msg.lower()


def test_a_rate_limited_account_says_rate_limited_and_not_broken():
    """423 is normal on Skytap and means "try again", not "your token is wrong"."""
    (ok, msg), rec = _verify(
        lambda r: httpx.Response(423, headers={"Retry-After": "1"}))
    assert ok is False
    assert "rate-limited" in msg.lower()
    # max_retries=1, so one retry and no more. The provisioning default of six would sit
    # for minutes behind a Settings spinner.
    assert len(rec.requests) == 2, f"expected 1 retry, got {len(rec.requests)} requests"


def test_a_dns_failure_names_the_cause_without_echoing_the_exception():
    """A caught exception's str() can carry local paths and the resolved address behind a
    hostname. Only the TYPE is diagnostic; the rest belongs in the log."""
    secret = "connect-failed-to-10.1.2.3-from-/home/someone/app"

    def handler(_r):
        raise httpx.ConnectError(secret)

    (ok, msg), _ = _verify(handler)
    assert ok is False
    assert secret not in msg, "the exception's own text reached the response body"
    assert "could not be reached" in msg.lower()


def test_a_timeout_is_distinguished_from_a_refused_connection():
    def handler(_r):
        raise httpx.ConnectTimeout("slow")

    (ok, msg), _ = _verify(handler)
    assert ok is False and "did not answer in time" in msg.lower()


# ── the shape ────────────────────────────────────────────────────────────────

def test_verify_never_raises_for_any_credential_outcome():
    """The structural rule. Every one of these is an ANSWER, and an answer is a return
    value — a handler that has to catch is a handler one edit away from stringifying."""
    cases = {
        "401": lambda r: httpx.Response(401, json={}),
        "403": lambda r: httpx.Response(403, json={}),
        "423": lambda r: httpx.Response(423, headers={"Retry-After": "1"}),
        "500": lambda r: httpx.Response(500, text="boom"),
        "empty": lambda r: httpx.Response(200, json=[]),
        "garbage": lambda r: httpx.Response(200, text="not json"),
    }
    for label, handler in cases.items():
        try:
            (ok, msg), _ = _verify(handler)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{label} escaped as {type(exc).__name__}: {exc}")
        assert ok is False and msg, f"{label} produced no message"

    def _boom(_r):
        raise httpx.ConnectError("x")

    try:
        (ok, _), _ = _verify(_boom)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"a transport failure escaped as {type(exc).__name__}")
    assert ok is False


def test_verify_reads_one_page_and_not_the_whole_catalogue():
    """The guard against someone simplifying this to `_client().list()`, which paginates
    to the end at 100 rows a page — nine GETs to answer a yes/no question."""
    (ok, _), rec = _verify(
        lambda r: httpx.Response(200, json=[{"id": "1", "name": "t"}]))
    assert ok is True
    assert len(rec.requests) == 1, f"expected exactly one request, got {len(rec.requests)}"
    assert "count=1" in str(rec.requests[0].url), rec.requests[0].url


def test_verify_carries_keep_idle_like_every_other_read():
    """A GET without it resets the environment idle timer, and the only symptom is the
    invoice. A new read must not be the one that forgets."""
    _, rec = _verify(lambda r: httpx.Response(200, json=[{"id": "1"}]))
    assert "keep_idle=true" in str(rec.requests[0].url), rec.requests[0].url


def test_verify_hits_the_template_catalogue_the_real_work_uses():
    """A check that authenticates differently from the work can pass while the work
    fails."""
    _, rec = _verify(lambda r: httpx.Response(200, json=[{"id": "1"}]))
    assert rec.requests[0].url.path == "/v2/templates"


# ── with a project configured ────────────────────────────────────────────────

def test_verify_checks_the_configured_project_too():
    """One call then proves the token, what it can see, AND that the Project ID is real."""
    _, rec = _verify(lambda r: httpx.Response(200, json=[{"id": "1"}]), project="123456")
    assert rec.requests[0].url.path == "/v2/projects/123456/templates"


def test_a_project_skytap_cannot_see_says_to_clear_it():
    """This is the difference between "your token is wrong" and "your scope is wrong",
    and they have completely different fixes."""
    (ok, msg), _ = _verify(lambda r: httpx.Response(404, text="not found"),
                           project="999999")
    assert ok is False
    assert "999999" in msg and "Project ID" in msg
    assert "credentials work" in msg.lower(), "it should exonerate the token"


def test_an_empty_project_blames_the_project_not_the_account():
    (ok, msg), _ = _verify(lambda r: httpx.Response(200, json=[]), project="123456")
    assert ok is False
    assert "project 123456" in msg and "widen the scope" in msg


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
