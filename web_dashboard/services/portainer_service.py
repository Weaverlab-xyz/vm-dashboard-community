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


# ── Access management: users, teams, memberships ─────────────────────────────
#
# The building blocks for just-in-time Portainer access (Cloud Functions Phase 2,
# integration #3): grant someone membership of a team that already has access to an
# environment, then take it away again. Everything above this line manages
# WORKLOADS; this section manages WHO CAN REACH THEM, which is a different concern
# and a much more sensitive one.
#
# Access itself is NOT granted per-user here, deliberately. In Portainer an access
# policy attaches to an environment or an environment group and names teams; an
# operator sets that up once, and a grant is then just a team membership. That keeps
# the reversible, per-request operation (add/remove one membership row) separate
# from the standing configuration, so a revoke can never leave a half-dismantled
# access policy behind.
#
# ⚠️  ROLE IDS ARE NUMERIC AND EASY TO INVERT. Portainer uses 1 = administrator and
#     2 = standard for USERS, and 1 = team LEADER, 2 = team member for MEMBERSHIPS.
#     Both are "1 is the powerful one", and both are silently accepted if swapped —
#     a JIT grant that hands out administrator is not an error the API reports. The
#     constants below exist so no call site writes a bare integer.

# Re-exported from portainer_access_rules, which is the single copy the Cloud
# Functions adapter also uses (it is vendored into the function's zip). Keeping the
# names here means no call site changed.
from . import portainer_access_rules as _rules  # noqa: E402
from .portainer_access_rules import (  # noqa: E402
    USER_ROLE_ADMIN, USER_ROLE_STANDARD, TEAM_ROLE_LEADER, TEAM_ROLE_MEMBER,
    PortainerRuleError,
)


async def _api(method: str, path: str, *, json_body: dict = None,
               ok: tuple = (200, 201, 204), context: str = ""):
    """One request against the CONFIGURED Portainer, over whichever transport this
    deployment uses.

    The methods above predate this and each carry their own automation/local branch;
    this collapses the pair for the access-management calls rather than doubling
    eight more methods. Returns the decoded body, or ``None`` for an empty response.
    """
    context = context or f"{method} {path}"
    if _EXECUTION_MODE == "automation":
        url, headers = await _portainer_url_and_headers()
        # The proxy takes the body as an already-serialized STRING (it is base64'd
        # into a runbook parameter), not as an object — passing a dict here would
        # be silently dropped and read at the far end as a Portainer validation
        # error rather than a missing payload.
        result = await _proxy_request(
            method, f"{url}{path}", headers,
            body=json.dumps(json_body) if json_body is not None else "")
        status = result.get("status_code", 0)
        if status not in ok:
            raise PortainerError(f"{context}: unexpected status {status}")
        return result.get("body")

    async with await _client() as client:
        resp = await client.request(method, path, json=json_body)
        if resp.status_code not in ok and not resp.is_success:
            _raise(resp, context)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None


@_wrap_transport_errors
async def list_users() -> list:
    """Every Portainer user (GET /api/users)."""
    data = await _api("GET", "/api/users", ok=(200,), context="list_users")
    return data if isinstance(data, list) else []


async def find_user(username: str) -> dict:
    """The user with this username, or ``{}``.

    Case-insensitive: Portainer stores the name as given but treats logins
    case-insensitively, so a case-sensitive lookup here would happily create a
    second "Alice" alongside "alice" and grant access to the wrong one.
    """
    return _rules.match_user(await list_users(), username)


@_wrap_transport_errors
async def create_user(username: str, password: str,
                      role: int = USER_ROLE_STANDARD) -> dict:
    """Create a user (POST /api/users). Defaults to STANDARD, never administrator."""
    try:
        role = _rules.validate_user_role(role)
    except PortainerRuleError as exc:
        raise PortainerError(str(exc)) from exc
    body = await _api("POST", "/api/users", context=f"create_user({username})",
                      json_body={"Username": username, "Password": password,
                                 "Role": role})
    logger.info("Portainer user '%s' created (role %s)", username, role)
    return body or {}


@_wrap_transport_errors
async def delete_user(user_id: int) -> None:
    """Delete a user (DELETE /api/users/{id}). A missing user is success, not 404:
    a revoke that errors on an already-removed account would leave the caller
    retrying forever and the access looking un-revoked."""
    await _api("DELETE", f"/api/users/{int(user_id)}",
               ok=(200, 204, 404), context=f"delete_user({user_id})")
    logger.info("Portainer user id %s deleted", user_id)


@_wrap_transport_errors
async def list_teams() -> list:
    """Every team (GET /api/teams)."""
    data = await _api("GET", "/api/teams", ok=(200,), context="list_teams")
    return data if isinstance(data, list) else []


async def find_team(name: str) -> dict:
    """The team with this name, or ``{}`` (case-insensitive, as for users)."""
    return _rules.match_team(await list_teams(), name)


@_wrap_transport_errors
async def create_team(name: str) -> dict:
    """Create a team (POST /api/teams)."""
    body = await _api("POST", "/api/teams", context=f"create_team({name})",
                      json_body={"Name": name})
    logger.info("Portainer team '%s' created", name)
    return body or {}


@_wrap_transport_errors
async def list_team_memberships() -> list:
    """Every membership (GET /api/team_memberships). Portainer has no filtered
    variant, so callers filter client-side."""
    data = await _api("GET", "/api/team_memberships", ok=(200,),
                      context="list_team_memberships")
    return data if isinstance(data, list) else []


async def find_membership(user_id: int, team_id: int) -> dict:
    """The membership joining this user to this team, or ``{}``."""
    return _rules.match_membership(await list_team_memberships(), user_id, team_id)


@_wrap_transport_errors
async def add_team_member(user_id: int, team_id: int,
                          role: int = TEAM_ROLE_MEMBER) -> dict:
    """Add a user to a team (POST /api/team_memberships).

    Idempotent: an existing membership is returned rather than duplicated, because
    Portainer will happily create a second row and then the FIRST revoke looks
    successful while access remains.
    """
    try:
        role = _rules.validate_team_role(role)
    except PortainerRuleError as exc:
        raise PortainerError(str(exc)) from exc
    existing = await find_membership(user_id, team_id)
    if existing:
        return existing
    body = await _api("POST", "/api/team_memberships",
                      context=f"add_team_member({user_id}→{team_id})",
                      json_body={"UserID": int(user_id), "TeamID": int(team_id),
                                 "Role": role})
    logger.info("Portainer user %s added to team %s (role %s)", user_id, team_id, role)
    return body or {}


@_wrap_transport_errors
async def remove_team_member(user_id: int, team_id: int) -> bool:
    """Remove a user from a team. ``True`` if a membership was removed, ``False`` if
    there was nothing to remove — both are success, for the same reason
    :func:`delete_user` treats 404 as success."""
    membership = await find_membership(user_id, team_id)
    if not membership:
        logger.info("Portainer user %s was not a member of team %s", user_id, team_id)
        return False
    membership_id = int(membership.get("Id", 0))
    await _api("DELETE", f"/api/team_memberships/{membership_id}",
               ok=(200, 204, 404),
               context=f"remove_team_member({user_id}→{team_id})")
    logger.info("Portainer user %s removed from team %s", user_id, team_id)
    return True


# ── Registries ───────────────────────────────────────────────────────────────
#
# Read + create only, and deliberately no password parameter. A registry's
# credential never survives a migration: Portainer does not return it from
# GET /api/registries, and the migration bundle scrubs credential-shaped fields on
# the way out anyway. So a registry created here is UNAUTHENTICATED, and the caller
# has to say so — a registry that looks configured but cannot pull is worse than an
# absent one.


@_wrap_transport_errors
async def list_registries() -> list:
    """Every registry (GET /api/registries)."""
    data = await _api("GET", "/api/registries", ok=(200,), context="list_registries")
    return data if isinstance(data, list) else []


async def find_registry(name: str) -> dict:
    """The registry with this name, or ``{}`` (case-insensitive, as for users/teams)."""
    target = (name or "").strip().lower()
    if not target:
        return {}
    for reg in await list_registries():
        if str(reg.get("Name") or "").strip().lower() == target:
            return reg
    return {}


@_wrap_transport_errors
async def create_registry(name: str, url: str, registry_type: int = 3) -> dict:
    """Create an UNAUTHENTICATED registry (POST /api/registries).

    ``registry_type`` is Portainer's numeric kind (3 = custom, which is the only one
    that is meaningful without credentials). Anything else is passed through as given
    — Portainer validates it — because a Docker Hub or ECR entry still carries a
    useful name and URL for the operator to finish by hand.
    """
    body = await _api("POST", "/api/registries",
                      context=f"create_registry({name})",
                      json_body={"Name": name, "URL": url,
                                 "Type": int(registry_type),
                                 "Authentication": False})
    logger.info("Portainer registry '%s' created (unauthenticated)", name)
    return body or {}


# ── First-run bootstrap for a dashboard-DEPLOYED node ────────────────────────
# These helpers target a specific base_url and do NOT go through _client() /
# _resolve_connection(): a freshly launched node has no PAT yet — minting one is
# the whole point. They are used only by ``portainer_node_service.run_deploy``;
# the configured-server functions above are untouched.
#
# The server presents a SELF-SIGNED cert on :9443, so these default to
# verify=False. That is safe here in a way it wouldn't be generally: the node was
# just created by us, and the firewall restricts who can reach it.


class PortainerAlreadyInitialized(PortainerError):
    """The node already has an admin user, so first-run init is closed.

    Portainer only allows ``POST /api/users/admin/init`` while no admin exists. Hit
    when redeploying onto a reused VM — recoverable by supplying an existing PAT in
    Settings, so the caller reports it rather than failing the deploy."""


class PortainerInitWindowClosed(PortainerError):
    """Portainer fenced off its API with "Administrator initialization timeout".

    A DIFFERENT failure from :class:`PortainerAlreadyInitialized`, and the two were
    conflated: Portainer shuts the admin-init window a short time after the container
    starts, and once it does, EVERY endpoint answers with this error and no admin
    exists — so there is no password to log in with and no PAT to mint. The node is
    unusable until the container restarts, which is not something a new PAT in
    Settings can fix, so the caller must not report it as a benign note."""


async def wait_ready(base_url: str, timeout_s: int = 300, poll_s: int = 5,
                     verify: bool = False) -> bool:
    """Poll ``GET /api/system/status`` until the node serves, or the timeout lapses.

    Returns True when Portainer answered. Connection errors are expected while the
    VM boots and COS pulls the image, so they're swallowed until the deadline.

    Raises :class:`PortainerInitWindowClosed` as soon as the node reports its
    administrator-initialization timeout: it is serving, but has fenced off its whole
    API, so polling to the deadline would only turn a diagnosable state into a
    misleading "did not start serving"."""
    import asyncio as _asyncio
    import time as _time

    deadline = _time.monotonic() + max(1, timeout_s)
    url = base_url.rstrip("/")
    attempt = 0
    while _time.monotonic() < deadline:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=verify) as client:
                resp = await client.get(f"{url}/api/system/status")
                if resp.is_success:
                    logger.info("Portainer at %s is serving (after %d attempt(s))", url, attempt)
                    return True
                if _is_init_timeout(resp):
                    raise PortainerInitWindowClosed(
                        f"Portainer at {url} is running but locked: its administrator-"
                        f"initialization window closed with no admin user, so the whole API "
                        f"answers 'administrator initialization timeout'. The container has "
                        f"to restart before anyone can log in.")
        except httpx.HTTPError:
            pass  # still booting
        await _asyncio.sleep(poll_s)
    logger.warning("Portainer at %s did not become ready within %ss", url, timeout_s)
    return False


@_wrap_transport_errors
async def init_admin(base_url: str, username: str, password: str,
                     verify: bool = False) -> None:
    """Create the initial admin user (POST /api/users/admin/init).

    Distinguishes the two ways this can be refused, because they need opposite
    handling: an existing admin (409, or a 403 that isn't the timeout) is benign and
    raises :class:`PortainerAlreadyInitialized`; a closed init window raises
    :class:`PortainerInitWindowClosed`, which means nobody can log in at all. Portainer
    reports the latter as "Administrator initialization timeout" — on the init endpoint
    and, from then on, on every other one too."""
    url = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        resp = await client.post(
            f"{url}/api/users/admin/init",
            json={"Username": username, "Password": password},
        )
        if _is_init_timeout(resp):
            raise PortainerInitWindowClosed(
                f"Portainer at {url} closed its administrator-initialization window "
                f"before the deploy could create the admin user (HTTP "
                f"{resp.status_code}: administrator initialization timeout). No admin "
                f"exists and the API is fenced off until the container restarts.")
        if resp.status_code in (403, 409):
            raise PortainerAlreadyInitialized(
                f"Portainer at {url} already has an admin user (HTTP {resp.status_code}) — "
                f"first-run initialization is closed.")
        if not resp.is_success:
            _raise(resp, "init_admin")
    logger.info("Portainer admin user '%s' initialized at %s", username, url)


def _is_init_timeout(resp) -> bool:
    """True when a response is Portainer's "Administrator initialization timeout".

    Matched on the message rather than the status code: Portainer serves it as 403 from
    the init endpoint and as a 303 redirect from everything else, so the code alone
    can't tell it apart from an ordinary refusal."""
    try:
        body = (resp.text or "")[:400].lower()
    except Exception:  # noqa: BLE001 — a body we can't read is not a timeout
        return False
    return "administrator initialization timeout" in body or "admin init timeout" in body


@_wrap_transport_errors
async def login(base_url: str, username: str, password: str,
                verify: bool = False) -> str:
    """Authenticate and return a short-lived JWT (POST /api/auth)."""
    url = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        resp = await client.post(
            f"{url}/api/auth",
            json={"Username": username, "Password": password},
        )
        if not resp.is_success:
            _raise(resp, "login")
        jwt = (resp.json() or {}).get("jwt") or ""
    if not jwt:
        raise PortainerError("Portainer login succeeded but returned no JWT")
    return jwt


@_wrap_transport_errors
async def create_access_token(base_url: str, jwt: str, password: str,
                              description: str = "vm-dashboard",
                              verify: bool = False) -> str:
    """Mint a personal API access token for the authenticated admin.

    Resolves the caller's own user id via ``GET /api/users/me`` and then
    ``POST /api/users/{id}/tokens`` (Portainer re-checks the password). Returns the
    RAW key — Portainer shows it exactly once, so the caller must persist it."""
    url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {jwt}"}
    async with httpx.AsyncClient(timeout=30.0, verify=verify) as client:
        me = await client.get(f"{url}/api/users/me", headers=headers)
        if not me.is_success:
            _raise(me, "create_access_token(whoami)")
        user_id = (me.json() or {}).get("Id")
        if user_id is None:
            raise PortainerError("Portainer /api/users/me returned no user Id")

        resp = await client.post(
            f"{url}/api/users/{user_id}/tokens", headers=headers,
            json={"description": description, "password": password},
        )
        if not resp.is_success:
            _raise(resp, "create_access_token")
        body = resp.json() or {}
    raw = body.get("rawAPIKey") or body.get("rawApiKey") or ""
    if not raw:
        raise PortainerError("Portainer token creation returned no rawAPIKey")
    logger.info("Portainer API token '%s' minted for user %s at %s",
                description, user_id, url)
    return raw
