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

def test_entitle_echo_normalizes_a_give_access_payload():
    resp = entitle_webhook_echo.handle(_req({
        "action": "Give Access", "userEmail": "alice@example.com",
        "resource": "prod-mysql", "role": "readonly",
        "durationSeconds": 3600, "requestId": "req-1",
    }), _ctx())
    got = resp.body["received"]
    assert got["action"] == "grant"
    assert got["user_email"] == "alice@example.com"
    assert got["duration_seconds"] == 3600
    assert resp.body["ok"] is True and resp.body["problems"] == []


def test_entitle_echo_normalizes_revoke_and_snake_case_spellings():
    resp = entitle_webhook_echo.handle(_req({
        "operation": "revoke_access", "user_email": "bob@example.com",
        "resource_name": "prod-mssql", "ttl_seconds": "900",
    }), _ctx())
    got = resp.body["received"]
    assert got["action"] == "revoke"
    assert got["user_email"] == "bob@example.com"
    assert got["duration_seconds"] == 900
    assert got["matched_keys"]["action"] == "operation"


def test_entitle_echo_falls_back_to_the_path_for_the_verb():
    req = Request(method="POST", path="/revoke-access", headers={}, query={},
                  body=json.dumps({"email": "c@example.com", "target": "db"}).encode(),
                  source="aws_function_url")
    resp = entitle_webhook_echo.handle(req, _ctx())
    assert resp.body["received"]["action"] == "revoke"
    assert resp.body["received"]["matched_keys"]["action"] == "<path>"


def test_entitle_echo_flattens_a_nested_user_object():
    resp = entitle_webhook_echo.handle(_req({
        "action": "grant", "user": {"email": "d@example.com"}, "resource": "r"}), _ctx())
    assert resp.body["received"]["user_email"] == "d@example.com"


def test_entitle_echo_reports_problems_but_still_returns_200():
    """A 4xx would make Entitle retry and hide the diagnostic — which is the only
    thing this workload exists to produce."""
    resp = entitle_webhook_echo.handle(_req({"nothing": "useful"}), _ctx())
    assert resp.status == 200
    assert resp.body["ok"] is False
    assert len(resp.body["problems"]) == 3
    assert resp.body["raw_keys"] == ["nothing"]


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
