"""Rancher management-plane client — **direct HTTPS** to the public COS node.

The central Rancher server runs as a single privileged container on a PUBLIC
(source-restricted) GCE COS VM (see ``gcp_service.run_gce_rancher`` /
``services/rancher_node_service.py``). Because it is publicly reachable, the
dashboard calls the Rancher v3 API directly over HTTPS with the stored API token
(httpx) — no in-cluster runner / ``kubectl run … curl`` pods.

Connection resolves config_service-first (``rancher_server_url`` /
``rancher_api_token`` / ``rancher_verify_tls``), env fallback. The node ships a
self-signed cert, so ``verify`` defaults to ``False``.

⚠️  VERIFICATION GATE: the Rancher v3 API paths/payloads below are the documented
shapes but have NOT been exercised against a live Rancher in this environment.
Confirm each against the target Rancher version before relying on it — they're
isolated here on purpose so corrections are one-liners.
"""
import functools
import logging

import httpx

logger = logging.getLogger(__name__)


class RancherError(Exception):
    """Raised when a Rancher API call fails or returns junk."""


class RancherNotConfigured(RancherError):
    """server_url / api_token missing — an expected state, not a failure."""


def _wrap_transport_errors(fn):
    """Convert httpx transport failures into RancherError for a single contract."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPError as exc:
            raise RancherError(f"Cannot reach Rancher: {exc}") from exc
    return wrapper


def _cfg(key: str, default: str = "") -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, default) or default


# ── Direct-HTTPS API ──────────────────────────────────────────────────────────

def _server_url(explicit: str = "") -> str:
    url = explicit or _cfg("rancher_server_url")
    if not url:
        raise RancherNotConfigured(
            "Rancher server URL is not configured — stand up the Rancher node on the Containers page.")
    return url.rstrip("/")


def _verify_tls() -> bool:
    try:
        from . import config_service
        from ..config import settings
        return config_service.get_bool("rancher_verify_tls", settings.rancher_verify_tls)
    except Exception:
        from ..config import settings
        return getattr(settings, "rancher_verify_tls", False)


def _api_token(explicit: str = "") -> str:
    tok = explicit or _cfg("rancher_api_token")
    if not tok:
        raise RancherNotConfigured(
            "Rancher API token is not configured — deploy/bootstrap the Rancher node first.")
    return tok


def _client(token: str = "", *, base_url: str = "") -> httpx.AsyncClient:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=_server_url(base_url), headers=headers, timeout=30.0, verify=_verify_tls())


def _raise(resp: httpx.Response, context: str) -> None:
    try:
        detail = resp.json()
        msg = detail.get("message") or detail.get("detail") or resp.text
    except Exception:
        msg = resp.text or f"HTTP {resp.status_code}"
    raise RancherError(f"{context}: {msg}")


@_wrap_transport_errors
async def bootstrap_direct(*, bootstrap_password: str, server_url: str) -> str:
    """First-run bootstrap over HTTPS: log in with the bootstrap password, pin the
    public ``server-url`` (what Rancher hands to imported cluster-agents), and mint
    a non-expiring API token. Returns ``token-xxxxx:yyyyy``. ``server_url`` is
    passed explicitly because config may not be set yet during first deploy."""
    async with _client(base_url=server_url) as c:
        r = await c.post("/v3-public/localProviders/local?action=login",
                         json={"username": "admin", "password": bootstrap_password,
                               "responseType": "json"})
        if r.status_code >= 300:
            _raise(r, "Rancher bootstrap login failed")
        login_token = r.json().get("token")
        if not login_token:
            raise RancherError("Rancher bootstrap login returned no token")

    async with _client(login_token, base_url=server_url) as c:
        r = await c.put("/v3/settings/server-url",
                        json={"name": "server-url", "value": server_url})
        if r.status_code >= 300:
            _raise(r, "Rancher set server-url failed")
        r = await c.post("/v3/token",
                         json={"type": "token", "description": "vm-dashboard", "ttl": 0})
        if r.status_code >= 300:
            _raise(r, "Rancher token mint failed")
        api_token = r.json().get("token")
        if not api_token:
            raise RancherError("Rancher token mint returned no token")
        return api_token


@_wrap_transport_errors
async def set_server_url_direct(*, server_url: str, api_token: str) -> None:
    """(Re-)pin the Rancher ``server-url`` using the API token. Used when a reused
    node's ephemeral IP changed after a stop/start (state on disk survives, so the
    token is still valid but the server-url is stale — agents dial the new IP)."""
    async with _client(api_token, base_url=server_url) as c:
        r = await c.put("/v3/settings/server-url",
                        json={"name": "server-url", "value": server_url})
        if r.status_code >= 300:
            _raise(r, "Rancher set server-url failed")


@_wrap_transport_errors
async def create_import_cluster_direct(*, name: str, api_token: str = "",
                                       server_url: str = "") -> tuple:
    """Create an *imported* cluster in Rancher + fetch its registration manifest
    URL. Returns ``(rancher_cluster_id, manifest_url)``. The caller applies the
    manifest into the downstream cluster (cattle-cluster-agent dials out)."""
    token = _api_token(api_token)
    async with _client(token, base_url=server_url) as c:
        r = await c.post("/v3/cluster", json={"type": "cluster", "name": name})
        if r.status_code >= 300:
            _raise(r, "Rancher cluster create failed")
        cluster_id = r.json().get("id")
        if not cluster_id:
            raise RancherError("Rancher cluster create returned no id")
        r = await c.post("/v3/clusterregistrationtoken",
                         json={"type": "clusterRegistrationToken", "clusterId": cluster_id})
        if r.status_code >= 300:
            _raise(r, "Rancher registration token failed")
        body = r.json()
        manifest_url = body.get("manifestUrl") or body.get("manifest_url")
        if not manifest_url:
            raise RancherError(
                f"Rancher registration token for {cluster_id} had no manifestUrl: {str(body)[:200]}")
        return cluster_id, manifest_url


@_wrap_transport_errors
async def delete_cluster_direct(*, cluster_id: str, api_token: str = "",
                                server_url: str = "") -> None:
    """Remove an imported cluster from Rancher (best-effort; caller logs errors)."""
    token = _api_token(api_token)
    async with _client(token, base_url=server_url) as c:
        r = await c.delete(f"/v3/cluster/{cluster_id}")
        if r.status_code >= 300 and r.status_code != 404:
            _raise(r, "Rancher cluster delete failed")
