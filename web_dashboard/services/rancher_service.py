"""Rancher management-plane client — direct HTTPS OR in-cloud runner transport.

The central Rancher server runs as a single privileged container on a PUBLIC
(source-restricted) GCE COS VM (see ``gcp_service.run_gce_rancher`` /
``services/rancher_node_service.py``). By default the dashboard calls the Rancher
v3 API directly over HTTPS with the stored API token (httpx).

**Transport** (``rancher_api_transport``): ``direct`` (default) | ``runner``.
Corp networks with TLS inspection (e.g. Cloudflare Gateway) kill the direct path
at the PROXY's origin-side cert verification — the node's self-signed cert is
rejected in transit, and client-side ``verify=False`` can't bypass that. The
``runner`` transport executes each call as ``curl`` in a one-shot GCP Cloud Run
job targeting the node's INTERNAL URL (``rancher_internal_url``, captured at
deploy) — see ``rancher_api_runner`` for the mechanics.

Connection resolves config_service-first (``rancher_server_url`` /
``rancher_api_token`` / ``rancher_verify_tls``), env fallback. The node ships a
self-signed cert, so ``verify`` defaults to ``False`` (the runner path is always
``--insecure`` for the same reason).

⚠️  VERIFICATION GATE: the Rancher v3 API paths/payloads below are the documented
shapes but have NOT been exercised against a live Rancher in this environment.
Confirm each against the target Rancher version before relying on it — they're
isolated here on purpose so corrections are one-liners.
"""
import functools
import json as _jsonlib
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


# ── Transport plumbing ────────────────────────────────────────────────────────

def _transport() -> str:
    """``direct`` (httpx to the public URL) | ``runner`` (curl in a Cloud Run job
    to the internal URL — the corp-TLS-inspection escape hatch). Exception-safe:
    an unresolvable config stack (bare test envs) means the default, direct."""
    try:
        return (_cfg("rancher_api_transport") or "direct").strip().lower()
    except Exception:
        return "direct"


def _server_url(explicit: str = "") -> str:
    url = explicit or _cfg("rancher_server_url")
    if not url:
        raise RancherNotConfigured(
            "Rancher server URL is not configured — stand up the Rancher node on the Containers page.")
    return url.rstrip("/")


def _runner_base_url() -> str:
    """The node's INTERNAL URL — the only address the Cloud Run runner can reach
    (its VPC-connector egress is private-ranges-only). Captured at deploy."""
    url = _cfg("rancher_internal_url")
    if not url:
        raise RancherNotConfigured(
            "rancher_api_transport=runner needs the node's internal URL "
            "(rancher_internal_url) — redeploy the Rancher node so it is captured.")
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


async def _call(method: str, path: str, *, token: str = "", base_url: str = "",
                json=None) -> tuple:
    """One Rancher API call over the configured transport → ``(status, body)``
    where ``body`` is a dict when the response parses as JSON, else raw text.

    On the runner transport the explicit ``base_url`` is IGNORED for addressing —
    the runner can only reach the internal URL — but callers that pin the public
    ``server-url`` still pass it in their JSON payloads, which is unaffected."""
    if _transport() == "runner":
        from . import rancher_api_runner
        url = f"{_runner_base_url()}{path}"
        try:
            status, text = await rancher_api_runner.request(
                method, url, token=token, json_body=json)
        except rancher_api_runner.RancherRunnerError as exc:
            raise RancherError(str(exc)) from exc
        try:
            body = _jsonlib.loads(text) if text.strip() else {}
        except ValueError:
            body = text
        return status, body
    async with _client(token, base_url=base_url) as c:
        r = await c.request(method, path, json=json)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return r.status_code, body


def _raise_status(status: int, body, context: str) -> None:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("detail") or str(body)[:300]
    else:
        msg = str(body or "")[:300] or f"HTTP {status}"
    raise RancherError(f"{context}: {msg}")


# ── Rancher v3 API ────────────────────────────────────────────────────────────

@_wrap_transport_errors
async def bootstrap_direct(*, bootstrap_password: str, server_url: str) -> str:
    """First-run bootstrap: log in with the bootstrap password, pin the public
    ``server-url`` (what Rancher hands to imported cluster-agents), and mint a
    non-expiring API token. Returns ``token-xxxxx:yyyyy``. ``server_url`` is
    passed explicitly because config may not be set yet during first deploy —
    it is the PINNED value; the transport decides how the calls travel."""
    status, body = await _call(
        "POST", "/v3-public/localProviders/local?action=login", base_url=server_url,
        json={"username": "admin", "password": bootstrap_password, "responseType": "json"})
    if status >= 300:
        _raise_status(status, body, "Rancher bootstrap login failed")
    login_token = body.get("token") if isinstance(body, dict) else None
    if not login_token:
        raise RancherError("Rancher bootstrap login returned no token")

    status, body = await _call(
        "PUT", "/v3/settings/server-url", token=login_token, base_url=server_url,
        json={"name": "server-url", "value": server_url})
    if status >= 300:
        _raise_status(status, body, "Rancher set server-url failed")
    status, body = await _call(
        "POST", "/v3/token", token=login_token, base_url=server_url,
        json={"type": "token", "description": "vm-dashboard", "ttl": 0})
    if status >= 300:
        _raise_status(status, body, "Rancher token mint failed")
    api_token = body.get("token") if isinstance(body, dict) else None
    if not api_token:
        raise RancherError("Rancher token mint returned no token")
    return api_token


@_wrap_transport_errors
async def set_server_url_direct(*, server_url: str, api_token: str) -> None:
    """(Re-)pin the Rancher ``server-url`` using the API token. Used when a reused
    node's ephemeral IP changed after a stop/start (state on disk survives, so the
    token is still valid but the server-url is stale — agents dial the new IP)."""
    status, body = await _call(
        "PUT", "/v3/settings/server-url", token=api_token, base_url=server_url,
        json={"name": "server-url", "value": server_url})
    if status >= 300:
        _raise_status(status, body, "Rancher set server-url failed")


@_wrap_transport_errors
async def create_import_cluster_direct(*, name: str, api_token: str = "",
                                       server_url: str = "") -> tuple:
    """Create an *imported* cluster in Rancher + fetch its registration manifest
    URL. Returns ``(rancher_cluster_id, manifest_url)``. The caller applies the
    manifest into the downstream cluster (cattle-cluster-agent dials out)."""
    token = _api_token(api_token)
    status, body = await _call("POST", "/v3/cluster", token=token, base_url=server_url,
                               json={"type": "cluster", "name": name})
    if status >= 300:
        _raise_status(status, body, "Rancher cluster create failed")
    cluster_id = body.get("id") if isinstance(body, dict) else None
    if not cluster_id:
        raise RancherError("Rancher cluster create returned no id")
    status, body = await _call(
        "POST", "/v3/clusterregistrationtoken", token=token, base_url=server_url,
        json={"type": "clusterRegistrationToken", "clusterId": cluster_id})
    if status >= 300:
        _raise_status(status, body, "Rancher registration token failed")
    manifest_url = (body.get("manifestUrl") or body.get("manifest_url")) if isinstance(body, dict) else None
    if not manifest_url:
        raise RancherError(
            f"Rancher registration token for {cluster_id} had no manifestUrl: {str(body)[:200]}")
    return cluster_id, manifest_url


@_wrap_transport_errors
async def delete_cluster_direct(*, cluster_id: str, api_token: str = "",
                                server_url: str = "") -> None:
    """Remove an imported cluster from Rancher (best-effort; caller logs errors)."""
    token = _api_token(api_token)
    status, body = await _call("DELETE", f"/v3/cluster/{cluster_id}",
                               token=token, base_url=server_url)
    if status >= 300 and status != 404:
        _raise_status(status, body, "Rancher cluster delete failed")
