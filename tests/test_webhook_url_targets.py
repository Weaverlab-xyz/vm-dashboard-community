"""A notification webhook can only point at the outside world, unless an admin says otherwise.

Before: anyone holding ``notifications:write`` (a delegable grant) could set a webhook to
``http://169.254.169.254/...`` or ``http://localhost:8080/...`` and press Test; the
dashboard POSTed from inside its own network and returned the reply body verbatim. That
is a proxy onto the cloud metadata service and every internal port. Now
(services/outbound_url.py):

  * the host is resolved and every address it resolves to is checked, when the URL is
    saved and again at every send (DNS can change; rows saved earlier are not trusted);
  * loopback, link-local (cloud metadata), unspecified, multicast and reserved addresses
    are always refused, and so is the EC2 IPv6 metadata address inside the ULA range;
  * private ranges (RFC 1918, CGNAT, ULA) are refused unless an administrator turns on
    "Allow internal webhook targets" (``notifications_allow_private_targets``);
  * the remote's reply body reaches administrators only -- in the Test result and in an
    endpoint's ``last_error``.

No DNS and no network: the resolver and httpx are replaced.

Runs standalone:
    python tests/test_webhook_url_targets.py
"""
import asyncio
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="webhook-targets-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "webhook-targets-test-secret")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import notifications as api  # noqa: E402
from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import config_service, notify_transports  # noqa: E402
from web_dashboard.services import outbound_url  # noqa: E402

Base.metadata.create_all(bind=engine)

PUBLIC = "93.184.215.14"
DNS = {
    "hooks.example.com": [PUBLIC],
    "metadata.example.com": ["169.254.169.254"],
    "local.example.com": ["127.0.0.1"],
    "v6local.example.com": ["::1"],
    "v6linklocal.example.com": ["fe80::1"],
    "ec2v6.example.com": ["fd00:ec2::254"],
    "mapped.example.com": ["::ffff:127.0.0.1"],
    "zero.example.com": ["0.0.0.0"],
    "internal.example.com": ["10.0.0.5"],
    "cgnat.example.com": ["100.64.1.1"],
    "ula.example.com": ["fd12:3456::1"],
    "mappedpriv.example.com": ["::ffff:10.0.0.5"],
    "split.example.com": [PUBLIC, "127.0.0.1"],     # one good answer is not enough
}


def _resolve(host, port):
    if host in DNS:
        return [(10 if ":" in a else 2, 1, 6, "", (a, port)) for a in DNS[host]]
    # Literal addresses resolve to themselves, as getaddrinfo does.
    import ipaddress
    try:
        a = str(ipaddress.ip_address(host))
    except ValueError:
        import socket
        raise socket.gaierror("no such host") from None
    return [(10 if ":" in a else 2, 1, 6, "", (a, port))]


outbound_url._resolve = _resolve

ALWAYS_REFUSED = ("metadata", "local", "v6local", "v6linklocal", "ec2v6", "mapped", "zero",
                  "split")
PRIVATE = ("internal", "cgnat", "ula", "mappedpriv")


def _url(name):
    return f"https://{name}.example.com/hook"


def _refused(url, allow_private=False):
    try:
        outbound_url.check_webhook_url(url, allow_private=allow_private)
    except outbound_url.OutboundURLRefused as exc:
        return str(exc)
    return ""


def test_internal_targets_are_always_refused():
    for name in ALWAYS_REFUSED:
        for allow in (False, True):
            assert _refused(_url(name), allow), f"{name} allowed (allow_private={allow})"
    for literal in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8080/",
                    "http://[::1]/", "http://[fd00:ec2::254]/"):
        assert _refused(literal, True), literal


def test_private_ranges_need_the_admin_setting():
    for name in PRIVATE:
        why = _refused(_url(name))
        assert "private address" in why and "administrator" in why, (name, why)
        assert not _refused(_url(name), allow_private=True), name


def test_a_public_host_is_accepted_and_junk_is_not():
    assert not _refused(_url("hooks"))
    for bad in ("ftp://hooks.example.com/x", "https:///nohost", "https://nowhere.invalid/x",
                "https://hooks.example.com:99999/x"):
        assert _refused(bad), bad


def test_saving_an_endpoint_checks_where_it_points():
    config_service.set("notifications_allow_private_targets", "false")
    for name in ALWAYS_REFUSED + PRIVATE:
        try:
            api._validate(_url(name), "custom")
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"saved a webhook pointing at {name}")
    api._validate(_url("hooks"), "custom")
    config_service.set("notifications_allow_private_targets", "true")
    try:
        api._validate(_url("internal"), "custom")
    finally:
        config_service.set("notifications_allow_private_targets", "false")


class _NoClient:
    """httpx.AsyncClient that must never be built."""
    built = 0

    def __init__(self, **_k):
        _NoClient.built += 1
        raise AssertionError("the dashboard opened a connection to a refused target")


def test_sending_rechecks_so_old_rows_and_rebinding_do_not_get_through():
    real = notify_transports.httpx.AsyncClient
    notify_transports.httpx.AsyncClient = _NoClient
    try:
        for name in ("metadata", "internal"):
            try:
                asyncio.run(notify_transports.post(_url(name), "custom", {"a": 1}))
            except notify_transports.NotifyError as exc:
                assert "will not send" in str(exc) or "private address" in str(exc), exc
            else:
                raise AssertionError(f"posted to {name}")
        assert _NoClient.built == 0
    finally:
        notify_transports.httpx.AsyncClient = real


def _user(admin):
    return types.SimpleNamespace(username="admin" if admin else "delegate",
                                 is_admin=admin, is_effective_admin=admin)


def test_the_test_button_shows_the_reply_body_to_admins_only():
    body = "HTTP 500: {\"AccessKeyId\": \"ASIA-SECRET\"}"

    async def _fake_send(db, endpoint_id, actor=""):
        return {"ok": False, "error": body, "endpoint": "e", "fmt": "custom"}
    real = api.notification_service.test_send
    api.notification_service.test_send = _fake_send
    api._audit = lambda *a, **k: None
    try:
        seen = asyncio.run(api.test_endpoint("e1", db=None, current_user=_user(False)))
        assert "ASIA-SECRET" not in str(seen) and seen["error"].startswith("HTTP 500"), seen
        seen = asyncio.run(api.test_endpoint("e1", db=None, current_user=_user(True)))
        assert "ASIA-SECRET" in seen["error"], seen
    finally:
        api.notification_service.test_send = real


def test_last_error_on_the_endpoint_list_is_trimmed_for_non_admins():
    real = api.notification_service.endpoint_public
    api.notification_service.endpoint_public = lambda ep: {
        "id": "e1", "last_error": "Slack rejected the payload: internal-page-text"}
    try:
        assert "internal-page-text" not in str(api._public(object(), _user(False)))
        assert "internal-page-text" in api._public(object(), _user(True))["last_error"]
    finally:
        api.notification_service.endpoint_public = real
    assert api._without_body("rate limited (HTTP 429): slow down") == (
        "rate limited (HTTP 429) (response body visible to administrators)")
    assert api._without_body("ConnectError: refused") == "ConnectError: refused"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
