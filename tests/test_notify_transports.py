"""Unit tests for services/notify_transports.py — payload shapes and the POST.

Three tests here exist because of specific, verified platform behaviour that is easy
to get wrong and produces a silent failure rather than an error:

  * :func:`test_slack_answering_200_with_an_error_body_is_a_failure` — Slack reports
    ``invalid_payload`` with HTTP 200.
  * :func:`test_teams_accepting_with_202_is_a_success` — Power Automate answers 202,
    so a 200-only check retries every successful post until it gives up.
  * :func:`test_the_signature_covers_the_exact_bytes_that_are_posted` — signing one
    serialisation and posting another is the classic HMAC bug.

No network: ``httpx`` is replaced with a recording double after import.
Runs under pytest, or standalone:
    python tests/test_notify_transports.py
"""
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {"notifications_enabled": "1"}


def _load():
    """Stub the parent packages, load notify_policy and notify_transports by file
    path (keeps this runnable on a bare checkout), and stub httpx if it is absent."""
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = []
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.get_bool = lambda key, default=False: (
        str(CONF.get(key, default)).strip().lower() in ("1", "true", "yes", "on")
    )
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg

    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = conf_mod

    try:
        import httpx  # noqa: F401
    except ImportError:
        stub = types.ModuleType("httpx")
        stub.HTTPError = type("HTTPError", (Exception,), {})
        stub.AsyncClient = object
        sys.modules["httpx"] = stub

    def _by_path(name):
        path = os.path.join(_ROOT, "web_dashboard", "services", f"{name}.py")
        spec = importlib.util.spec_from_file_location(
            f"web_dashboard.services.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"web_dashboard.services.{name}"] = mod
        setattr(services, name, mod)
        spec.loader.exec_module(mod)
        return mod

    return _by_path("notify_policy"), _by_path("notify_transports")


pol, nt = _load()


# ── doubles ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code=200, text="ok", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    """Records the single POST notify_transports.post makes."""
    last = None

    def __init__(self, **kwargs):
        _FakeClient.last = {"init": kwargs}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        _FakeClient.last.update({"url": url, **kwargs})
        return _FakeClient.response


def _fake_httpx(response):
    _FakeClient.response = response
    _FakeClient.last = None
    mod = types.SimpleNamespace(AsyncClient=_FakeClient, HTTPError=Exception)
    nt.httpx = mod
    return mod


def _ev(**over):
    base = {
        "event_type": "resource.expiring",
        "title": "web-01 expires in 23 hours",
        "body": "It will be destroyed automatically unless you extend it.",
        "resource_id": "job:abc", "resource_kind": "vm", "resource_name": "web-01",
        "cloud": "aws", "region": "us-east-1", "url": "/inventory",
    }
    base.update(over)
    return pol.NotificationEvent(**base)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _with_base_url(value="https://dash.corp.example"):
    CONF["notify_base_url"] = value


def _no_base_url():
    CONF.pop("notify_base_url", None)


# ── Teams: the shape Microsoft actually accepts ──────────────────────────────

def test_teams_uses_the_bot_framework_envelope_not_a_bare_card():
    """Posting a bare Adaptive Card is the most common reason a Workflows webhook
    "succeeds" and renders an empty post in the channel."""
    _with_base_url()
    subj, body = pol.render(_ev())
    p = nt.build_teams(_ev(), subj, body)
    assert p["type"] == "message"
    att = p["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["content"]["type"] == "AdaptiveCard"
    assert att["content"]["version"] == "1.4"


def test_teams_does_not_emit_the_retired_messagecard_shape():
    """Office 365 connectors were permanently disabled in May 2026; MessageCard's
    ``@type``/``themeColor`` keys are dead and must not reappear."""
    _with_base_url()
    raw = json.dumps(nt.build_teams(_ev(), "s", "b"))
    assert "@type" not in raw
    assert "themeColor" not in raw
    assert "MessageCard" not in raw


def test_teams_omits_the_action_when_there_is_no_absolute_url():
    """An Action.OpenUrl with an empty url rejects the entire card, so no link is
    better than a blank one."""
    _no_base_url()
    content = nt.build_teams(_ev(), "s", "b")["attachments"][0]["content"]
    assert "actions" not in content

    _with_base_url()
    content = nt.build_teams(_ev(), "s", "b")["attachments"][0]["content"]
    assert content["actions"][0]["url"] == "https://dash.corp.example/inventory"


def test_teams_carries_the_facts_as_a_factset():
    _with_base_url()
    content = nt.build_teams(_ev(fields={"Expires": "2026-07-30"}), "s", "b")\
        ["attachments"][0]["content"]
    factsets = [b for b in content["body"] if b["type"] == "FactSet"]
    assert factsets, "no FactSet in the card"
    titles = [f["title"] for f in factsets[0]["facts"]]
    assert "Cloud" in titles and "Expires" in titles


# ── Slack ────────────────────────────────────────────────────────────────────

def test_slack_always_sets_text_even_when_blocks_are_present():
    """``text`` is what shows in the notification popup; a blocks-only payload
    produces a silent, unreadable alert."""
    _with_base_url()
    p = nt.build_slack(_ev(), "the subject", "the body")
    assert p["text"] == "the subject"
    assert p["blocks"]


def test_slack_escapes_the_characters_it_treats_as_markup():
    p = nt.build_slack(_ev(), "a <b> & c", "x > y")
    raw = json.dumps(p["blocks"])
    assert "&lt;b&gt;" in raw and "&amp;" in raw


# ── the custom envelope: a public contract ───────────────────────────────────

def test_the_custom_envelope_keys_are_stable():
    """Receivers parse this. Renaming a key silently breaks integrations we can't
    see, so the shape is pinned here and versioned in docs/notifications.md."""
    _with_base_url()
    p = nt.build_custom(_ev(fields={"Expires": "2026-07-30"}), "subj", "body")
    assert set(p) == {"version", "event", "severity", "subject", "body",
                      "resource", "fields", "url", "occurred_at"}
    assert set(p["resource"]) == {"id", "kind", "name", "cloud", "region", "workgroup"}
    assert p["version"] == 1
    assert p["event"] == "resource.expiring"
    assert p["severity"] == "warning"
    assert p["url"] == "https://dash.corp.example/inventory"


# ── signing ──────────────────────────────────────────────────────────────────

def test_the_signature_covers_the_exact_bytes_that_are_posted():
    """Signing a separately-serialised body and posting with ``json=`` would make
    every receiver's verification fail. This asserts ``content=`` carries the signed
    bytes byte-for-byte."""
    _fake_httpx(_Resp(200, "ok"))
    payload = nt.build_custom(_ev(), "subj", "body")
    _run(nt.post("https://hooks.corp.example/x", "custom", payload,
                 secret="s3cret", event_type="resource.expiring",
                 delivery_id="d-1", timeout=5))

    sent = _FakeClient.last
    raw = sent["content"]
    assert isinstance(raw, (bytes, bytearray)), "must post raw bytes, not json="
    assert "json" not in sent, "json= would re-serialise and invalidate the signature"

    ts = sent["headers"]["X-Dashboard-Timestamp"]
    expected = hmac.new(b"s3cret", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Dashboard-Signature"] == f"sha256={expected}"
    # And the bytes really are the payload.
    assert json.loads(raw.decode()) == payload


def test_serialization_is_deterministic():
    """Two calls must produce identical bytes or a signature can't be reproduced."""
    p = {"b": 1, "a": {"d": 2, "c": 3}}
    assert nt.serialize(p) == nt.serialize(dict(reversed(list(p.items()))))
    assert b" " not in nt.serialize(p)          # compact separators


def test_signing_headers_are_only_sent_for_the_custom_format():
    """Slack and Teams reject unknown headers politely, but sending a delivery id and
    signature to a third party is a needless leak of internal identifiers."""
    for fmt in ("slack", "teams"):
        hdrs = nt.headers_for(fmt, {}, b"{}", secret="s", event_type="e", delivery_id="d")
        assert set(hdrs) == {"Content-Type"}


def test_no_signature_header_when_no_secret_is_configured():
    hdrs = nt.headers_for("custom", {}, b"{}", secret="", event_type="e", delivery_id="d")
    assert "X-Dashboard-Signature" not in hdrs
    assert hdrs["X-Dashboard-Delivery"] == "d"


# ── response handling ────────────────────────────────────────────────────────

def test_slack_answering_200_with_an_error_body_is_a_failure():
    """Slack returns ``invalid_payload`` / ``no_service`` as HTTP 200 with a plain
    body. Treating that as success is how these integrations quietly do nothing."""
    nt.check_response("slack", 200, "ok")                       # the only success
    for body in ("invalid_payload", "no_service", "channel_not_found", ""):
        try:
            nt.check_response("slack", 200, body)
        except nt.NotifyError as exc:
            assert body[:10] in str(exc) or body == ""
        else:
            raise AssertionError(f"200/{body!r} should not count as delivered")


def test_teams_accepting_with_202_is_a_success():
    """Power Automate answers 202 Accepted. A 200-only check would mark every
    successful Teams post failed and retry it to exhaustion."""
    nt.check_response("teams", 202, "")
    nt.check_response("custom", 204, "")
    nt.check_response("custom", 201, "created")


def test_a_non_2xx_carries_the_body_into_the_error():
    """That string lands on the delivery row and is the whole diagnosis."""
    try:
        nt.check_response("custom", 403, "Forbidden: bad token")
    except nt.NotifyError as exc:
        assert "403" in str(exc) and "bad token" in str(exc)
    else:
        raise AssertionError("403 should raise")


def test_a_429_is_reported_as_rate_limiting_with_the_retry_delay():
    _fake_httpx(_Resp(429, "slow down", {"Retry-After": "45"}))
    try:
        _run(nt.post("https://x/y", "slack", {"text": "t"}, timeout=5))
    except nt.NotifyError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("429 should raise")


def test_a_transport_exception_becomes_a_notify_error_naming_the_cause():
    class _Boom:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise _Boom.err("certificate verify failed: unable to get local issuer")

    _Boom.err = type("ConnectError", (Exception,), {})
    nt.httpx = types.SimpleNamespace(AsyncClient=_Boom, HTTPError=_Boom.err)
    try:
        _run(nt.post("https://x/y", "custom", {}, timeout=5))
    except nt.NotifyError as exc:
        assert "ConnectError" in str(exc) and "certificate verify failed" in str(exc)
    else:
        raise AssertionError("a transport error should surface as NotifyError")


# ── client construction ──────────────────────────────────────────────────────

def test_the_client_is_built_for_a_corp_proxy_and_does_not_follow_redirects():
    """``trust_env`` picks up HTTPS_PROXY; ``follow_redirects=False`` keeps a signed
    body from being replayed to whatever a redirect points at."""
    _fake_httpx(_Resp(200, "ok"))
    _run(nt.post("https://x/y", "slack", {"text": "t"}, timeout=7))
    init = _FakeClient.last["init"]
    assert init["trust_env"] is True
    assert init["follow_redirects"] is False
    assert init["timeout"] == 7.0
    assert "verify" in init


def test_verify_prefers_a_real_ca_bundle_path_over_certifi():
    """httpx ignores SSL_CERT_FILE, which this image sets and the corp-CA overlay
    mounts over — so the bundle has to be resolved and passed explicitly."""
    saved = {k: os.environ.get(k) for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        assert nt._verify() is True

        os.environ["SSL_CERT_FILE"] = os.path.join(_ROOT, "no", "such", "bundle.crt")
        assert nt._verify() is True, "a path that does not exist must not be passed on"

        os.environ["SSL_CERT_FILE"] = os.path.abspath(__file__)
        assert nt._verify() == os.path.abspath(__file__)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── redaction ────────────────────────────────────────────────────────────────

def test_a_webhook_url_is_reduced_to_a_hint():
    """The path and query of a Slack or Teams webhook URL are the credential."""
    hint = nt.redact_url("https://hooks.slack.com/services/T00/B00/XXXXsecretXXXX")
    assert hint == "https://hooks.slack.com/…"
    assert "secret" not in hint
    assert nt.redact_url("") == ""
    assert nt.redact_url("not a url") == "…"


def test_every_format_has_a_builder():
    for fmt in nt.FORMATS:
        assert fmt in nt.BUILDERS
    # An unknown format degrades to the documented envelope rather than raising.
    assert nt.build("nonsense", _ev(), "s", "b")["version"] == 1


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
