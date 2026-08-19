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


# ── Every adapter answers when its settings are missing ──────────────────────
#
# The bug this pins, in the shape it shipped in: each adapter resolved its config at
# the TOP of handle(), before routing. A function deployed without that config raised
# on every path, dispatch turned the raise into ``500 {"error": "internal error"}``,
# and /check_config — the one route whose job is to name what is missing — was the
# loudest casualty. Held across all three adapters because all three had it.

def _adapters():
    """Imported here rather than at module scope: portainer_access and
    azure_role_grant pull in their vendored rules modules, and a failure to do that
    should fail THIS test rather than the whole file."""
    from fnworkloads import azure_role_grant, db_grant, portainer_access
    return (db_grant, portainer_access, azure_role_grant)


def _unset_fn_settings():
    """Drop every FN_* setting except the ones the runtime itself owns, so each
    adapter sees exactly what a hand-deploy with no configuration gives it."""
    keep = {"FN_WORKLOAD", "FN_CLOUD", "FN_REGION", "FN_NAME", "FN_SHARED_SECRET",
            "FN_NETWORK_MODE", "FN_NETWORK", "FN_VPC_EGRESS", "FN_DEBUG",
            "FN_LOG_BODY", "FN_EGRESS_PROBE", "FN_AUTH_HEADER", "FN_AUTH_PREFIX"}
    removed = {k: v for k, v in os.environ.items()
               if k.startswith("FN_") and k not in keep}
    for key in removed:
        del os.environ[key]
    return removed


def test_an_unconfigured_adapter_names_the_problem_instead_of_500ing_opaquely():
    saved = _unset_fn_settings()
    try:
        for module in _adapters():
            resp = module.handle(
                Request(method="POST", path="/give_access", headers={}, query={},
                        body=b"{}", source="aws_function_url"),
                _ctx(module.NAME))
            assert isinstance(resp, Response), (module.NAME, resp)
            assert resp.status == 500, (module.NAME, resp.status)
            assert resp.body["error"] == "function not configured", (module.NAME, resp.body)
            # The whole point: a settings error the operator can act on, not
            # dispatch's opaque generic body.
            assert resp.body.get("problem"), (module.NAME, resp.body)
            assert resp.body["problem"] != "internal error", (module.NAME, resp.body)
    finally:
        os.environ.update(saved)


def test_an_unconfigured_adapter_still_answers_check_config():
    saved = _unset_fn_settings()
    try:
        for module in _adapters():
            resp = module.handle(
                Request(method="POST", path="/check_config", headers={}, query={},
                        body=b"{}", source="aws_function_url"),
                _ctx(module.NAME))
            assert resp.status == 200, (module.NAME, resp.status, resp.body)
            data = resp.body["data"]
            assert data["valid"] is False, (module.NAME, data)
            assert data["problems"] and all(data["problems"]), (module.NAME, data)
    finally:
        os.environ.update(saved)


def test_an_unconfigured_adapter_still_lists_its_routes_on_a_bad_path():
    saved = _unset_fn_settings()
    try:
        for module in _adapters():
            resp = module.handle(
                Request(method="POST", path="/", headers={}, query={},
                        body=b"{}", source="aws_function_url"),
                _ctx(module.NAME))
            assert resp.status == 404, (module.NAME, resp.status)
            assert "POST /check_config" in resp.body["routes"], (module.NAME, resp.body)
    finally:
        os.environ.update(saved)


def test_every_adapter_declares_what_it_cannot_run_without():
    """REQUIRED_ENV is what stops the deploy form producing an inert function; an
    adapter that reads settings but declares none is the gap reopening."""
    for module in _adapters():
        required = getattr(module, "REQUIRED_ENV", ())
        assert required, f"{module.NAME} declares no REQUIRED_ENV"
        for entry in required:
            assert entry.startswith("FN_"), (module.NAME, entry)
            # "A|B" means either satisfies it; every alternative must be a real name.
            assert all(alt.strip() for alt in entry.split("|")), (module.NAME, entry)


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
