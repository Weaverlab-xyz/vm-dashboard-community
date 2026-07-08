"""
Portainer CE REST API wrapper — a single Portainer connection (community edition).

Connection settings resolve config_service-first (Settings → Integrations →
Portainer CE; encrypted in the DB, vault refs like bt_safe:// resolved
transparently), then fall back to env vars (PORTAINER_URL / PORTAINER_PAT).
Installs that predate the Settings PAT field can still hold the token in
BeyondTrust Password Safe under `portainer_pat_secret_title`.

Auth header: X-API-Key: <pat>

Execution mode (POWERSHELL_EXECUTION_MODE env var):
  "local"      — direct httpx to Portainer (development on local network)
  "automation" — proxy through Azure Automation Hybrid Worker (cloud deployment)

Key API paths:
  GET  /api/endpoints                                     — list environments
  GET  /api/endpoints/{id}/docker/containers/json         — list containers
  POST /api/endpoints/{id}/docker/containers/create       — create container
  POST /api/endpoints/{id}/docker/containers/{cid}/start  — start
  POST /api/endpoints/{id}/docker/containers/{cid}/stop   — stop
  DELETE /api/endpoints/{id}/docker/containers/{cid}      — remove
  GET  /api/stacks                                        — list stacks
  POST /api/stacks/create/standalone/string               — deploy compose stack
"""
import base64
import functools
import json
import logging
import os

import httpx

from ..config import settings
from .btapi_service import get_ps_secret
from . import cache_service

logger = logging.getLogger(__name__)

# Execution mode: "local" (direct httpx) or "automation" (Hybrid Worker proxy)
_EXECUTION_MODE = os.getenv("POWERSHELL_EXECUTION_MODE", "local").lower()

# Runbook name for the generic HTTP proxy
_PORTAINER_RUNBOOK = "Invoke-PortainerProxy"


class PortainerError(Exception):
    pass


class PortainerNotConfigured(PortainerError):
    """URL or API token missing — an expected state, not a connection failure."""


def _wrap_transport_errors(fn):
    """Convert httpx transport failures (unreachable host, TLS, timeout) into
    PortainerError so every caller sees one error contract instead of raw 500s."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPError as exc:
            raise PortainerError(f"Cannot reach Portainer: {exc}") from exc
    return wrapper


# ── Shared helpers ────────────────────────────────────────────────────────────

_NOT_CONFIGURED_URL = (
    "Portainer URL is not configured. Add it in Settings → Integrations → Portainer CE."
)
_NOT_CONFIGURED_PAT = (
    "Portainer API token is not configured. Add it in Settings → Integrations → Portainer CE."
)


async def _resolve_connection() -> tuple[str, str, bool]:
    """Return (base_url, pat, verify_ssl) from config_service with env fallback."""
    from . import config_service

    url = config_service.get("portainer_url") or settings.portainer_url
    if not url:
        raise PortainerNotConfigured(_NOT_CONFIGURED_URL)
    verify = config_service.get_bool("portainer_verify_ssl", settings.portainer_verify_ssl)

    # config_service.get resolves vault refs (bt_safe://, aws_sm://, …) transparently
    pat = config_service.get("portainer_pat") or settings.portainer_pat
    if not pat:
        # Legacy fallback: PAT held in BeyondTrust Password Safe under a secret
        # title — only attempted when ps-cli credentials are actually configured.
        pscli_ready = bool(config_service.get("pscli_api_url") or settings.pscli_api_url)
        if pscli_ready and settings.portainer_pat_secret_title:
            try:
                pat = await get_ps_secret(settings.portainer_pat_secret_title)
            except Exception as exc:
                raise PortainerError(
                    f"Portainer API token lookup from Password Safe failed: {exc}"
                ) from exc
    if not pat:
        raise PortainerNotConfigured(_NOT_CONFIGURED_PAT)
    return url.rstrip("/"), pat, verify


async def _portainer_url_and_headers() -> tuple[str, dict]:
    """Return (base_url, headers_dict) for the configured Portainer instance."""
    url, pat, _ = await _resolve_connection()
    return url, {"X-API-Key": pat}


# ── Automation mode: proxy through Hybrid Worker ─────────────────────────────

async def _proxy_request(
    method: str,
    url: str,
    headers: dict,
    body: str = "",
    content_type: str = "application/json",
    form_data: str = "",
) -> dict:
    """Route an HTTP request through the Hybrid Worker via Azure Automation.

    Parameters are base64-encoded by Python so the PS5.1 runbook never needs
    ConvertTo-Json (which has a known serialization bug in Hybrid Worker
    environments when a parameter value itself contains a JSON string).
    """
    from . import automation_service

    params_json = json.dumps({
        "Method": method,
        "Url": url,
        "Headers": json.dumps(headers),
        "Body": body,
        "ContentType": content_type,
        "FormData": form_data,
    })
    params_b64 = base64.b64encode(params_json.encode()).decode()

    result = await automation_service.execute(
        action="",
        params={},
        runbook=_PORTAINER_RUNBOOK,
        raw_params={"ParamsB64": params_b64},
    )
    if not result.get("success"):
        status = result.get("status_code", 0)
        error = result.get("error", "Unknown proxy error")
        raise PortainerError(f"Portainer proxy error (HTTP {status}): {error}")
    return result


# ── Local mode: direct httpx client ──────────────────────────────────────────

async def _client() -> httpx.AsyncClient:
    """Build an authenticated async httpx client for the Portainer API."""
    url, pat, verify = await _resolve_connection()
    return httpx.AsyncClient(
        base_url=url,
        headers={"X-API-Key": pat},
        timeout=30.0,
        verify=verify,
    )


def _raise(resp: httpx.Response, context: str) -> None:
    """Raise PortainerError with a meaningful message from a non-2xx response."""
    try:
        detail = resp.json()
        msg = detail.get("message") or detail.get("details") or resp.text
    except Exception:
        msg = resp.text or f"HTTP {resp.status_code}"
    raise PortainerError(f"{context}: {msg}")


# ── Environments ──────────────────────────────────────────────────────────────

@_wrap_transport_errors
async def list_endpoints() -> list[dict]:
    """Return all Portainer environments (GET /api/endpoints)."""
    if _EXECUTION_MODE == "automation":
        cache_key = cache_service.key_param("portainer_endpoints")
        ttl = cache_service.TTL["portainer_endpoints"]

        async def _fetch():
            url, headers = await _portainer_url_and_headers()
            result = await _proxy_request("GET", f"{url}/api/endpoints", headers)
            data = result["body"]
            return data if isinstance(data, list) else data.get("results", data) if isinstance(data, dict) else []

        data, _ = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        return data

    async with await _client() as client:
        resp = await client.get("/api/endpoints")
        if not resp.is_success:
            _raise(resp, "list_endpoints")
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("results", data)


# ── Containers ────────────────────────────────────────────────────────────────

@_wrap_transport_errors
async def list_containers(endpoint_id: int, all_containers: bool = True) -> list[dict]:
    """
    List containers on a Docker endpoint (GET /api/endpoints/{id}/docker/containers/json).
    all_containers=True includes stopped containers.
    """
    if _EXECUTION_MODE == "automation":
        cache_key = cache_service.key_param(
            "portainer_containers", endpoint_id=str(endpoint_id), all=str(all_containers),
        )
        ttl = cache_service.TTL["portainer_containers"]

        async def _fetch():
            url, headers = await _portainer_url_and_headers()
            all_param = "1" if all_containers else "0"
            result = await _proxy_request(
                "GET", f"{url}/api/endpoints/{endpoint_id}/docker/containers/json?all={all_param}", headers,
            )
            body = result["body"]
            if isinstance(body, list):
                return body
            # PowerShell's ConvertTo-Json can unwrap a single-element array to a plain
            # dict — treat that as a 1-container list rather than an empty list.
            if isinstance(body, dict):
                # A Portainer error dict has a "message" key; a real container has "Id".
                if "Id" in body:
                    logger.warning(
                        "list_containers: PS unwrapped single-element array for endpoint %d; re-wrapping",
                        endpoint_id,
                    )
                    return [body]
                raise PortainerError(
                    f"list_containers: unexpected dict response for endpoint {endpoint_id}: "
                    f"{body.get('message', body)}"
                )
            raise PortainerError(
                f"list_containers: unexpected response type {type(body).__name__} for endpoint {endpoint_id}"
            )

        data, _ = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        return data

    async with await _client() as client:
        resp = await client.get(
            f"/api/endpoints/{endpoint_id}/docker/containers/json",
            params={"all": 1 if all_containers else 0},
        )
        if not resp.is_success:
            _raise(resp, f"list_containers(endpoint={endpoint_id})")
        return resp.json()


@_wrap_transport_errors
async def start_container(endpoint_id: int, container_id: str) -> None:
    """Start a container (POST .../start). 204=started, 304=already running — both ok."""
    if _EXECUTION_MODE == "automation":
        url, headers = await _portainer_url_and_headers()
        result = await _proxy_request(
            "POST", f"{url}/api/endpoints/{endpoint_id}/docker/containers/{container_id}/start", headers,
        )
        status = result.get("status_code", 0)
        if status not in (200, 204, 304):
            raise PortainerError(f"start_container: unexpected status {status}")
        await cache_service.invalidate_prefix("portainer_containers")
        return

    async with await _client() as client:
        resp = await client.post(
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/start"
        )
        if resp.status_code not in (204, 304):
            _raise(resp, f"start_container({container_id[:12]})")


@_wrap_transport_errors
async def stop_container(endpoint_id: int, container_id: str) -> None:
    """Stop a container (POST .../stop). 204=stopped, 304=already stopped — both ok."""
    if _EXECUTION_MODE == "automation":
        url, headers = await _portainer_url_and_headers()
        result = await _proxy_request(
            "POST", f"{url}/api/endpoints/{endpoint_id}/docker/containers/{container_id}/stop", headers,
        )
        status = result.get("status_code", 0)
        if status not in (200, 204, 304):
            raise PortainerError(f"stop_container: unexpected status {status}")
        await cache_service.invalidate_prefix("portainer_containers")
        return

    async with await _client() as client:
        resp = await client.post(
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/stop"
        )
        if resp.status_code not in (204, 304):
            _raise(resp, f"stop_container({container_id[:12]})")


@_wrap_transport_errors
async def remove_container(endpoint_id: int, container_id: str, force: bool = True) -> None:
    """Remove a container (DELETE .../containers/{id}?force=true)."""
    if _EXECUTION_MODE == "automation":
        url, headers = await _portainer_url_and_headers()
        force_param = "true" if force else "false"
        result = await _proxy_request(
            "DELETE",
            f"{url}/api/endpoints/{endpoint_id}/docker/containers/{container_id}?force={force_param}",
            headers,
        )
        status = result.get("status_code", 0)
        if status not in (200, 204):
            raise PortainerError(f"remove_container: unexpected status {status}")
        await cache_service.invalidate_prefix("portainer_containers")
        return

    async with await _client() as client:
        resp = await client.delete(
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}",
            params={"force": "true" if force else "false"},
        )
        if resp.status_code != 204:
            _raise(resp, f"remove_container({container_id[:12]})")


@_wrap_transport_errors
async def deploy_container(
    endpoint_id: int,
    name: str,
    image: str,
    ports: list[dict],      # [{"host": 8080, "container": 80, "protocol": "tcp"}]
    env: list[dict],        # [{"key": "K", "value": "V"}]
    restart_policy: str,    # "unless-stopped" | "always" | "no" | "on-failure"
) -> dict:
    """
    Create then immediately start a container.
    Returns {"container_id": <full id>, "name": <name>}.
    """
    # Build Docker API create body
    exposed = {f"{p['container']}/{p.get('protocol', 'tcp')}": {} for p in ports}
    bindings = {
        f"{p['container']}/{p.get('protocol', 'tcp')}": [{"HostPort": str(p["host"])}]
        for p in ports
        if p.get("host")
    }
    env_list = [f"{e['key']}={e['value']}" for e in env if e.get("key")]

    body = {
        "Image": image,
        "ExposedPorts": exposed,
        "Env": env_list,
        "HostConfig": {
            "PortBindings": bindings,
            "RestartPolicy": {"Name": restart_policy},
        },
    }

    if _EXECUTION_MODE == "automation":
        url, headers = await _portainer_url_and_headers()
        # Step 1: Create
        create_result = await _proxy_request(
            "POST", f"{url}/api/endpoints/{endpoint_id}/docker/containers/create?name={name}",
            headers, body=json.dumps(body),
        )
        container_id = create_result["body"]["Id"]
        # Step 2: Start
        await _proxy_request(
            "POST", f"{url}/api/endpoints/{endpoint_id}/docker/containers/{container_id}/start",
            headers,
        )
        await cache_service.invalidate_prefix("portainer_containers")
        logger.info("Deployed container %s (%s) on endpoint %d via proxy", name, container_id[:12], endpoint_id)
        return {"container_id": container_id, "name": name}

    async with await _client() as client:
        # Step 1: Create
        create_resp = await client.post(
            f"/api/endpoints/{endpoint_id}/docker/containers/create",
            params={"name": name},
            json=body,
        )
        if not create_resp.is_success:
            _raise(create_resp, f"deploy_container create({name})")

        container_id = create_resp.json()["Id"]

        # Step 2: Start
        start_resp = await client.post(
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/start"
        )
        if start_resp.status_code not in (204, 304):
            _raise(start_resp, f"deploy_container start({name})")

    logger.info("Deployed container %s (%s) on endpoint %d", name, container_id[:12], endpoint_id)
    return {"container_id": container_id, "name": name}


# ── Stacks ────────────────────────────────────────────────────────────────────

@_wrap_transport_errors
async def list_stacks(endpoint_id: int) -> list[dict]:
    """List stacks filtered to a specific endpoint (GET /api/stacks?filters=...)."""
    if _EXECUTION_MODE == "automation":
        cache_key = cache_service.key_param("portainer_stacks", endpoint_id=str(endpoint_id))
        ttl = cache_service.TTL["portainer_stacks"]

        async def _fetch():
            url, headers = await _portainer_url_and_headers()
            filters = json.dumps({"EndpointID": endpoint_id})
            result = await _proxy_request(
                "GET", f"{url}/api/stacks?filters={filters}", headers,
            )
            return result["body"] if isinstance(result["body"], list) else []

        data, _ = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        return data

    async with await _client() as client:
        resp = await client.get(
            "/api/stacks",
            params={"filters": json.dumps({"EndpointID": endpoint_id})},
        )
        if not resp.is_success:
            _raise(resp, f"list_stacks(endpoint={endpoint_id})")
        return resp.json() or []


# NOTE: the k8s management plane moved from Portainer (agent + endpoint
# registration) to Rancher — the former ``check_agent_health`` /
# ``add_agent_endpoint`` helpers were removed with that switch. This module now
# serves only the non-k8s Containers page (Docker-host / container management).


@_wrap_transport_errors
async def deploy_stack(
    endpoint_id: int,
    name: str,
    compose_content: str,
    env: list[dict] | None = None,  # [{"key": "K", "value": "V"}]
) -> dict:
    """
    Deploy a new standalone Docker Compose stack.
    POST /api/stacks/create/standalone/string?endpointId={id}
    """
    body = {
        "name": name,
        "stackFileContent": compose_content,
        "env": [{"name": e["key"], "value": e["value"]} for e in (env or []) if e.get("key")],
    }

    if _EXECUTION_MODE == "automation":
        url, headers = await _portainer_url_and_headers()
        result = await _proxy_request(
            "POST", f"{url}/api/stacks/create/standalone/string?endpointId={endpoint_id}",
            headers, body=json.dumps(body),
        )
        await cache_service.invalidate_prefix("portainer_stacks")
        logger.info("Deployed stack %s on endpoint %d via proxy", name, endpoint_id)
        return result["body"]

    async with await _client() as client:
        resp = await client.post(
            "/api/stacks/create/standalone/string",
            params={"endpointId": endpoint_id},
            json=body,
        )
        if not resp.is_success:
            _raise(resp, f"deploy_stack({name})")
    logger.info("Deployed stack %s on endpoint %d", name, endpoint_id)
    return resp.json()
