"""Unit tests for the dashboard's published outbound pool (``egress_pool_service``)
and how the managed-node allow-list uses it.

The failure this exists for (live 2026-09-29, job 57cd8880): the dashboard ran on an
Azure Container Apps environment with no NAT gateway, whose ~400-address SNAT pool
picks an address per DESTINATION. The echo service kept answering 135.237.231.126, the
node's NSG admitted exactly that /32, and the node saw a different address -- readiness
passed, the bootstrap was dropped, and re-detecting "produced no change".

Stubs config_service with a dict and never touches the network: the Azure calls go
to a fake client. Runs under pytest or standalone:

    python tests/test_egress_pool_service.py
"""
import asyncio
import base64
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_settings = types.SimpleNamespace(portainer_ready_timeout_s=300)
_cfg_mod = types.ModuleType("web_dashboard.config")
_cfg_mod.settings = _settings
sys.modules.setdefault("web_dashboard.config", _cfg_mod)

_CONFIG: dict = {}
_cfgsvc = types.ModuleType("web_dashboard.services.config_service")
_cfgsvc.get = lambda key, default=None: _CONFIG.get(key, "")
_cfgsvc.set = lambda key, val: _CONFIG.__setitem__(key, val)
_cfgsvc.get_bool = lambda key, default=False: str(_CONFIG.get(key, default)).lower() in ("1", "true", "yes")
sys.modules["web_dashboard.services.config_service"] = _cfgsvc

try:
    import httpx  # noqa: F401  -- third-party, probed by name
except ImportError:
    print("SKIP: httpx not installed")
    sys.exit(0)

from web_dashboard.services import egress_pool_service as eps   # noqa: E402
from web_dashboard.services import managed_node_service as mns  # noqa: E402

_ACA_ENV = {"CONTAINER_APP_NAME": "dash-worker", "IDENTITY_ENDPOINT": "http://localhost:42356/msi/token",
            "IDENTITY_HEADER": "hdr"}
_RID = ("/subscriptions/s/resourceGroups/RG-CWeaver/providers/Microsoft.App/"
        "containerapps/dash-worker")


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Client:
    """Answers the identity endpoint, then ARM or Resource Graph, from a script."""

    def __init__(self, *, rows=None, arm=None, token_status=200):
        self.rows = rows
        self.arm = arm
        self.token_status = token_status
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params, headers))
        if url == _ACA_ENV["IDENTITY_ENDPOINT"]:
            return _Resp(self.token_status, {"access_token": _jwt({"oid": "oid-123"})})
        return self.arm

    async def post(self, url, params=None, headers=None, json=None):
        self.calls.append(("POST", url, params, json))
        return _Resp(200, {"data": self.rows or []})


def _run(coro):
    return asyncio.run(coro)


def _reset(**cfg):
    _CONFIG.clear()
    _CONFIG.update(cfg)


# ── pure pieces ────────────────────────────────────────────────────────────────

def test_only_container_apps_has_a_pool_platform():
    assert eps.hosting_platform({"CONTAINER_APP_NAME": "dash"}) == eps.PLATFORM_ACA
    assert eps.hosting_platform({}) == ""
    assert eps.hosting_platform({"CONTAINER_APP_NAME": "  "}) == ""
    # A NAT-gatewayed environment turns it off; the next refresh then clears the pool.
    assert eps.hosting_platform({"CONTAINER_APP_NAME": "dash", eps.DISABLE_ENV: "off"}) == ""


def test_the_pool_is_ipv4_slash_32s_deduplicated_and_sorted():
    got = eps.parse_pool(["20.7.131.26", "4.153.75.54", "20.7.131.26", "junk", "2001:db8::1", ""])
    assert got == ["4.153.75.54/32", "20.7.131.26/32"], got


def test_the_token_oid_is_read_for_the_grant_hint_only():
    assert eps._token_oid(_jwt({"oid": "abc"})) == "abc"
    assert eps._token_oid("not-a-jwt") == ""


# ── discovery ──────────────────────────────────────────────────────────────────

def test_off_container_apps_discovery_is_a_no_op():
    assert _run(eps.discover(env={}, client=_Client())) == ("", [])


def test_resource_graph_finds_the_app_by_name_and_returns_its_pool():
    client = _Client(rows=[{"id": _RID, "ips": ["135.237.231.126", "172.193.115.158"]}])
    source, cidrs = _run(eps.discover(env=_ACA_ENV, client=client))
    assert source == f"{eps.PLATFORM_ACA}:{_RID}", source
    assert cidrs == ["135.237.231.126/32", "172.193.115.158/32"], cidrs
    token_call = client.calls[0]
    assert token_call[3] == {"X-IDENTITY-HEADER": "hdr"}, token_call
    assert "client_id" not in token_call[2], "a blank client_id asks for the WRONG identity"
    query = client.calls[1][3]["query"]
    assert "name =~ 'dash-worker'" in query, query


def test_no_rows_means_the_grant_is_missing_and_the_message_names_the_command():
    client = _Client(rows=[])
    try:
        _run(eps.discover(env=_ACA_ENV, client=client))
    except eps.EgressPoolError as exc:
        msg = str(exc)
    else:
        raise AssertionError("expected EgressPoolError")
    assert "az role assignment create" in msg and "oid-123" in msg and "Reader" in msg, msg


def test_a_missing_managed_identity_says_to_assign_one():
    env = {"CONTAINER_APP_NAME": "dash-worker"}
    try:
        _run(eps.discover(env=env, client=_Client()))
    except eps.EgressPoolError as exc:
        assert "az containerapp identity assign" in str(exc), exc
    else:
        raise AssertionError("expected EgressPoolError")


def test_an_explicit_resource_id_is_read_directly_and_a_403_names_the_grant():
    env = dict(_ACA_ENV, **{eps.RESOURCE_ID_ENV: _RID})
    ok = _Client(arm=_Resp(200, {"properties": {"outboundIpAddresses": ["20.1.250.250"]}}))
    assert _run(eps.discover(env=env, client=ok))[1] == ["20.1.250.250/32"]
    assert ok.calls[1][1].endswith(_RID), ok.calls[1]
    denied = _Client(arm=_Resp(403, {"error": {"code": "AuthorizationFailed"}}))
    try:
        _run(eps.discover(env=env, client=denied))
    except eps.EgressPoolError as exc:
        assert f"--scope {_RID}" in str(exc), exc
    else:
        raise AssertionError("expected EgressPoolError")


def test_an_unexpected_app_name_never_reaches_the_query():
    env = dict(_ACA_ENV, CONTAINER_APP_NAME="x' | project secrets //")
    client = _Client(rows=[])
    try:
        _run(eps.discover(env=env, client=client))
    except eps.EgressPoolError as exc:
        assert eps.RESOURCE_ID_ENV in str(exc), exc
    assert not [c for c in client.calls if c[0] == "POST"], client.calls


# ── refresh / persistence ──────────────────────────────────────────────────────

def test_refresh_persists_the_pool_and_clears_the_error():
    _reset(**{eps.ERROR_KEY: "old failure"})
    client = _Client(rows=[{"id": _RID, "ips": ["20.7.131.26", "4.153.75.54"]}])
    st = _run(eps.refresh(env=_ACA_ENV, client=client))
    assert st["count"] == 2 and st["error"] == "", st
    assert _CONFIG[eps.POOL_KEY] == "4.153.75.54/32,20.7.131.26/32", _CONFIG


def test_a_failed_refresh_keeps_the_last_good_pool():
    """A transient ARM error must not narrow a working allow-list back to one /32."""
    _reset(**{eps.POOL_KEY: "4.153.75.54/32,20.7.131.26/32"})
    st = _run(eps.refresh(env=_ACA_ENV, client=_Client(rows=[])))
    assert st["count"] == 2, st
    assert "az role assignment create" in st["error"], st


def test_moving_off_container_apps_clears_the_pool():
    _reset(**{eps.POOL_KEY: "4.153.75.54/32", eps.SOURCE_KEY: "azure-container-apps:x"})
    st = _run(eps.refresh(env={}, client=_Client()))
    assert st["count"] == 0 and st["source"] == "", st


# ── the managed-node allow-list ────────────────────────────────────────────────

def test_the_pool_joins_the_dashboard_sources():
    _reset(portainer_dashboard_egress_cidr="135.237.231.126/32",
           **{eps.POOL_KEY: "135.237.231.126/32,172.193.115.158/32"})
    got = mns.dashboard_cidr(mns.PORTAINER)
    assert got == ["135.237.231.126/32", "172.193.115.158/32"], got


def test_a_replaced_pin_stays_in_the_recent_set():
    """The 2026-09-29 rule held ONE /32 because the address a detection overwrote was
    dropped rather than remembered."""
    _reset(portainer_dashboard_egress_cidr="172.193.115.158/32")

    async def _detect():
        return "135.237.231.126"

    _run(mns.ensure_dashboard_egress_cidr(mns.PORTAINER, detect=_detect))
    got = mns.dashboard_cidr(mns.PORTAINER)
    assert "135.237.231.126/32" in got and "172.193.115.158/32" in got, got


def test_aws_drops_only_the_pool_when_it_does_not_fit():
    pool = [f"20.7.{i // 250}.{i % 250}/32" for i in range(400)]
    _reset(portainer_dashboard_egress_cidr=pool[5], **{eps.POOL_KEY: ",".join(pool)})
    merged = sorted(set(pool) | {"198.51.100.9/32"})
    kept, omitted = mns.fit_to_capacity("aws", mns.PORTAINER, merged)
    assert set(kept) == {"198.51.100.9/32", pool[5]}, kept
    assert omitted == len(merged) - 2, omitted
    # Azure and GCP hold the whole pool.
    for cloud in ("azure", "gcp"):
        assert mns.fit_to_capacity(cloud, mns.PORTAINER, merged) == (merged, 0), cloud


def test_error_text_summarizes_a_long_source_list():
    assert mns.summarize_cidrs(["a", "b"]) == "a, b"
    assert mns.summarize_cidrs([str(i) for i in range(10)], limit=3) == "0, 1, 2 and 7 more"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
