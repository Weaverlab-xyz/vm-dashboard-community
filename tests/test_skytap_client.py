"""The Skytap client's three easy-to-get-wrong behaviours.

Each of these is silent when wrong, which is why they are pinned before anything is built
on top of them:

  * **423 Locked is normal.** It is how Skytap says "busy, or rate-limited", with a
    Retry-After. Treating it as a failure reports a broken integration during ordinary
    concurrent use.
  * **Every GET carries keep_idle=true.** Without it, *reading* an environment resets its
    idle timer — so polling holds every environment awake and defeats suspend_on_idle. The
    only symptom is the bill.
  * **Collections paginate by count/offset.** A single GET returns a first page that looks
    exactly like a complete answer.

Runs against httpx.MockTransport: no network, no app, no database. Sleep is injected, so
proving the retry waits costs no wall-clock time.

Runs under pytest, or standalone:
    python tests/test_skytap_client.py
"""
import asyncio
import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-skytap-client")

try:
    import httpx
except ImportError:  # pragma: no cover
    print("SKIP: httpx not installed")
    sys.exit(0)

from web_dashboard.services.skytap_client import (  # noqa: E402
    SkytapAuthError, SkytapClient, SkytapCreds, SkytapError,
)

CREDS = SkytapCreds(username="se@example.com", api_token="tok-123",
                    base_url="https://cloud.skytap.test")


class _Recorder:
    """Collects every request and replies from a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(200, json=[])
        nxt = self.responses.pop(0)
        return nxt(request) if callable(nxt) else nxt


def _client(responses, slept=None):
    rec = _Recorder(responses)

    async def _sleep(seconds):
        if slept is not None:
            slept.append(seconds)

    return SkytapClient(CREDS, transport=httpx.MockTransport(rec), sleep=_sleep), rec


def _run(coro):
    return asyncio.run(coro)


# ── auth ─────────────────────────────────────────────────────────────────────

def test_basic_auth_is_username_and_api_token():
    """Skytap uses an API token, not the account password, and Basic auth — not a bearer."""
    cli, rec = _client([httpx.Response(200, json={"ok": True})])
    _run(cli.get("/v2/templates"))
    sent = rec.requests[0].headers["Authorization"]
    assert sent.startswith("Basic "), f"expected Basic auth, got {sent.split()[0]!r}"
    decoded = base64.b64decode(sent.split(" ", 1)[1]).decode()
    assert decoded == "se@example.com:tok-123"


def test_the_token_is_not_in_the_repr():
    """These get passed through log-heavy call paths."""
    assert "tok-123" not in repr(CREDS)
    assert "redacted" in repr(CREDS)


def test_unconfigured_credentials_are_refused_with_a_useful_message():
    for bad in (SkytapCreds(username="", api_token="t"),
                SkytapCreds(username="u", api_token="")):
        try:
            SkytapClient(bad)
        except SkytapError as exc:
            assert "Settings" in str(exc), "the message should say where to fix it"
        else:  # pragma: no cover
            raise AssertionError("an unconfigured client was constructed")


def test_a_401_is_its_own_error_type():
    """The one failure an operator fixes in Settings rather than by retrying."""
    cli, _ = _client([httpx.Response(401, json={"error": "nope"})])
    try:
        _run(cli.get("/v2/templates"))
    except SkytapAuthError as exc:
        assert "API token" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a 401 did not raise SkytapAuthError")


# ── keep_idle ────────────────────────────────────────────────────────────────

def test_every_get_carries_keep_idle():
    """The cost leak with no symptom. See the module docstring."""
    cli, rec = _client([httpx.Response(200, json={})])
    _run(cli.get("/v2/configurations/42"))
    assert "keep_idle=true" in str(rec.requests[0].url), \
        f"GET went out without keep_idle: {rec.requests[0].url}"


def test_keep_idle_survives_caller_supplied_params():
    cli, rec = _client([httpx.Response(200, json={})])
    _run(cli.get("/v2/configurations", params={"scope": "me"}))
    url = str(rec.requests[0].url)
    assert "keep_idle=true" in url and "scope=me" in url


def test_a_caller_may_override_keep_idle_deliberately():
    """setdefault, not force: a caller that genuinely wants to touch the timer can, and
    has to say so at the call site where it will be read."""
    cli, rec = _client([httpx.Response(200, json={})])
    _run(cli.get("/v2/configurations/42", params={"keep_idle": "false"}))
    assert "keep_idle=false" in str(rec.requests[0].url)


def test_writes_do_not_carry_keep_idle():
    """It is a read-side concern; a PUT that changes runstate has no idle timer to spare."""
    cli, rec = _client([httpx.Response(200, json={})])
    _run(cli.request("PUT", "/v2/configurations/42", json={"runstate": "running"}))
    assert "keep_idle" not in str(rec.requests[0].url)


# ── 423 / 429 retry ──────────────────────────────────────────────────────────

def test_a_423_is_retried_and_then_succeeds():
    slept = []
    cli, rec = _client([
        httpx.Response(423, headers={"Retry-After": "2"}),
        httpx.Response(423, headers={"Retry-After": "3"}),
        httpx.Response(200, json={"id": "42"}),
    ], slept=slept)
    assert _run(cli.get("/v2/configurations/42")) == {"id": "42"}
    assert len(rec.requests) == 3, "the call was not retried"
    assert slept == [2.0, 3.0], f"Retry-After was not honoured: {slept}"


def test_a_429_is_retried_too():
    slept = []
    cli, rec = _client([httpx.Response(429, headers={"Retry-After": "1"}),
                        httpx.Response(200, json={})], slept=slept)
    _run(cli.get("/v2/templates"))
    assert len(rec.requests) == 2
    assert slept == [1.0]


def test_a_missing_retry_after_falls_back_rather_than_hammering():
    slept = []
    cli, _ = _client([httpx.Response(423), httpx.Response(200, json={})], slept=slept)
    _run(cli.get("/v2/templates"))
    assert slept and slept[0] > 0, "a 423 without Retry-After must still back off"


def test_an_absurd_retry_after_is_clamped():
    """A one-hour Retry-After inside a single HTTP call is worse than a clear failure."""
    slept = []
    cli, _ = _client([httpx.Response(423, headers={"Retry-After": "3600"}),
                      httpx.Response(200, json={})], slept=slept)
    _run(cli.get("/v2/templates"))
    assert slept[0] <= 30.0, f"unclamped Retry-After: {slept[0]}"


def test_a_garbage_retry_after_does_not_crash():
    slept = []
    cli, _ = _client([httpx.Response(423, headers={"Retry-After": "later please"}),
                      httpx.Response(200, json={})], slept=slept)
    _run(cli.get("/v2/templates"))
    assert slept and slept[0] > 0


def test_retries_are_bounded_and_the_error_says_why():
    slept = []
    cli, rec = _client([httpx.Response(423, headers={"Retry-After": "1"})] * 20,
                       slept=slept)
    try:
        _run(cli.get("/v2/templates"))
    except SkytapError as exc:
        assert "rate-limited" in str(exc), f"unhelpful message: {exc}"
    else:  # pragma: no cover
        raise AssertionError("an endless 423 did not eventually fail")
    assert len(rec.requests) < 20, "the retry loop is unbounded"


# ── decoding ─────────────────────────────────────────────────────────────────

def test_a_204_decodes_to_none_rather_than_exploding():
    cli, _ = _client([httpx.Response(204)])
    assert _run(cli.request("DELETE", "/v2/configurations/42")) is None


def test_a_non_json_body_is_reported_as_such():
    cli, _ = _client([httpx.Response(200, text="<html>maintenance</html>")])
    try:
        _run(cli.get("/v2/templates"))
    except SkytapError as exc:
        assert "non-JSON" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an HTML body decoded as success")


def test_a_500_carries_the_body_into_the_message():
    """A failed job surfaces only its error_message, so the cause has to be inside it."""
    cli, _ = _client([httpx.Response(500, text="template 999 is not yours")])
    try:
        _run(cli.get("/v2/templates/999"))
    except SkytapError as exc:
        assert "not yours" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a 500 did not raise")


# ── pagination ───────────────────────────────────────────────────────────────

def test_a_full_page_is_followed_to_the_end():
    page1 = [{"id": str(i)} for i in range(100)]
    page2 = [{"id": "100"}]
    cli, rec = _client([httpx.Response(200, json=page1),
                        httpx.Response(200, json=page2)])
    got = _run(cli.list("/v2/templates"))
    assert len(got) == 101, f"pagination stopped early: {len(got)}"
    assert "offset=0" in str(rec.requests[0].url)
    assert "offset=100" in str(rec.requests[1].url)


def test_a_short_page_ends_the_walk():
    cli, rec = _client([httpx.Response(200, json=[{"id": "1"}])])
    assert len(_run(cli.list("/v2/templates"))) == 1
    assert len(rec.requests) == 1, "a short page should not trigger another request"


def test_paginated_reads_also_carry_keep_idle():
    cli, rec = _client([httpx.Response(200, json=[])])
    _run(cli.list("/v2/configurations"))
    assert "keep_idle=true" in str(rec.requests[0].url)


def test_a_single_object_where_a_list_was_expected_is_not_swallowed():
    cli, _ = _client([httpx.Response(200, json={"id": "42"})])
    assert _run(cli.list("/v2/configurations")) == [{"id": "42"}]


def test_pagination_is_bounded():
    """A server that keeps returning full pages must not loop forever."""
    full = [{"id": str(i)} for i in range(100)]
    cli, rec = _client([httpx.Response(200, json=full)] * 500)
    got = _run(cli.list("/v2/templates"))
    assert len(rec.requests) <= 100, "the pagination loop is unbounded"
    assert len(got) == len(rec.requests) * 100


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
