"""Cloud Functions: the Phase 1 workload catalog.

Network-touching assertions are limited to targets that cannot leave the host
(a closed localhost port, an unresolvable name), so this runs offline and in CI
without flaking on DNS.
"""
import json
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime.contract import Context, Request, Response
from fnworkloads import echo_diag, entitle_webhook_echo


def _req(body=None, query=None, headers=None):
    return Request(method="POST", path="/", headers=headers or {}, query=query or {},
                   body=json.dumps(body or {}).encode(), source="aws_function_url")


def _ctx(workload=""):
    return Context.from_env(workload=workload)


def _closed_port() -> int:
    """A port on localhost with nothing listening (bind, read, release)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ── echo_diag ─────────────────────────────────────────────────────────────────

def test_echo_diag_reports_placement_and_redacts_headers():
    resp = echo_diag.handle(
        _req({"egress": False}, headers={"authorization": "Bearer topsecret"}),
        _ctx("echo_diag"))
    assert isinstance(resp, Response) and resp.status == 200
    assert resp.body["request"]["headers"]["authorization"] == "***"
    assert "topsecret" not in json.dumps(resp.body)
    assert "placement" in resp.body and "hostname" in resp.body["placement"]


def test_echo_diag_distinguishes_refused_from_timeout():
    """`refused` means the network path WORKED and nothing was listening — the
    single most useful distinction this workload draws."""
    resp = echo_diag.handle(
        _req({"probe": [{"host": "127.0.0.1", "port": _closed_port()}], "egress": False}),
        _ctx())
    probe = resp.body["probes"][0]
    assert probe["dns"] == "ok", probe
    assert probe["connect"] == "refused", probe
    assert "connect_ms" in probe and "dns_ms" in probe


def test_echo_diag_reports_dns_failure_separately_from_connect():
    resp = echo_diag.handle(
        _req({"probe": [{"host": "no-such-host.invalid", "port": 5432}], "egress": False}),
        _ctx())
    probe = resp.body["probes"][0]
    assert probe["dns"] in ("failed", "timeout"), probe
    assert "connect" not in probe, "must not claim a connect result without an address"


def test_echo_diag_accepts_the_host_port_shorthand():
    port = _closed_port()
    resp = echo_diag.handle(_req({"probe": f"127.0.0.1:{port}", "egress": False}), _ctx())
    assert resp.body["probes"][0]["port"] == port


def test_echo_diag_caps_probe_count_and_timeout():
    targets = [{"host": "127.0.0.1", "port": _closed_port()}] * 50
    resp = echo_diag.handle(_req({"probe": targets, "timeout": 999, "egress": False}), _ctx())
    assert len(resp.body["probes"]) <= 20


def test_echo_diag_ignores_a_garbage_timeout():
    resp = echo_diag.handle(_req({"timeout": "abc", "egress": False}), _ctx())
    assert resp.status == 200


# ── entitle_webhook_echo ──────────────────────────────────────────────────────

def test_entitle_echo_serves_every_contract_route():
    """The point of this workload: prove each *_path in the integration config maps
    to a route that exists, before any target system is involved."""
    for method, route in entitle_webhook_echo._ROUTES:
        path = route.replace("{asset_identifier}", "demo:asset:1")
        resp = entitle_webhook_echo.handle(
            Request(method=method, path=path, headers={}, query={},
                    body=b"{}", source="aws_function_url"), _ctx())
        assert resp.status == 200, (method, path, resp.body)


def test_entitle_echo_returns_the_contract_envelopes():
    """Entitle rejects a malformed body, so the empty responses still have to be
    the right SHAPE or the integration cannot even be saved."""
    def _get(path):
        return entitle_webhook_echo.handle(
            Request(method="GET", path=path, headers={}, query={}, body=b"{}",
                    source="aws_function_url"), _ctx()).body

    assets = _get("/get_assets")
    assert "next" in assets and "assets" in assets["data"]
    assert assets["data"]["assets"][0]["role_options"], "no role for Entitle to offer"
    perms = _get("/get_all_permissions")
    assert "actors_permissions" in perms["data"] and "assets_permissions" in perms["data"]


def test_entitle_echo_reports_which_contract_fields_arrived():
    """The diagnostic: what Entitle actually sent, against what the contract says."""
    resp = entitle_webhook_echo.handle(
        Request(method="POST", path="/give_access", headers={}, query={},
                body=json.dumps({"asset": {"identifier": "x"},
                                 "actor_identifier": "jit_a_1",
                                 "role_code": "read"}).encode(),
                source="aws_function_url"), _ctx())
    observed = resp.body["observed"]
    assert observed["missing_keys"] == []
    assert observed["received_keys"] == ["actor_identifier", "asset", "role_code"]


def test_entitle_echo_flags_a_payload_that_misses_contract_fields():
    resp = entitle_webhook_echo.handle(
        Request(method="POST", path="/give_access", headers={}, query={},
                body=json.dumps({"nothing": "useful"}).encode(),
                source="aws_function_url"), _ctx())
    # Still 200: a 4xx would make Entitle retry and hide the diagnostic, which is
    # the only thing this workload exists to produce.
    assert resp.status == 200
    assert set(resp.body["observed"]["missing_keys"]) == {
        "asset", "actor_identifier", "role_code"}


def test_entitle_echo_never_grants_anything():
    for path in ("/give_access", "/create_actor", "/revoke_access", "/delete_actor"):
        resp = entitle_webhook_echo.handle(
            Request(method="POST", path=path, headers={}, query={}, body=b"{}",
                    source="aws_function_url"), _ctx())
        assert resp.body["data"]["granted"] is False, path


def test_entitle_echo_404s_an_unknown_route_and_lists_the_real_ones():
    """Entitle's paths are configurable, so this is the most likely setup mistake."""
    resp = entitle_webhook_echo.handle(
        Request(method="POST", path="/grant", headers={}, query={}, body=b"{}",
                source="aws_function_url"), _ctx())
    assert resp.status == 404
    assert any("/give_access" in route for route in resp.body["routes"])


def test_entitle_echo_failure_injection():
    for mode, expected in (("401", 401), ("403", 403), ("404", 404), ("500", 500)):
        resp = entitle_webhook_echo.handle(_req({}, query={"fail": mode}), _ctx())
        assert resp.status == expected, (mode, resp.status)
        assert resp.body["injected"] is True
    unknown = entitle_webhook_echo.handle(_req({}, query={"fail": "nonsense"}), _ctx())
    assert unknown.status == 400


def test_every_workload_exposes_the_contract():
    for module in (echo_diag, entitle_webhook_echo):
        assert isinstance(getattr(module, "NAME", None), str) and module.NAME
        assert isinstance(getattr(module, "DESCRIPTION", None), str) and module.DESCRIPTION
        assert callable(getattr(module, "handle", None))


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
