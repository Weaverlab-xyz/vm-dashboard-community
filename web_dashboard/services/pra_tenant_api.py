"""The PRA Configuration API, spoken to a *named tenant* rather than to the singletons.

``pra_api_service`` talks to the one appliance an install is configured for, which is the
right shape on a demo instance. A POV instance holds a registry of many, so the same calls
need a ``bt_tenant_service.Tenant`` instead of ``bt_api_host`` — and the handshake itself
must not be written a second time, because a token request that drifts from the one the
real work makes turns a green check into a lie.

So this module owns the tenant-scoped half and nothing else: one token, and the reads the
POV feature needs. ``bt_tenant_verify`` uses the token function rather than its own copy,
which is what keeps "Verify said yes" and "the Gateway install worked" answering about the
same call.

Deliberately not a rewrite of ``pra_api_service``. That module keeps its singleton callers
— every demo-instance path — and this one exists alongside it until something needs both.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# A config-API read is an interactive operation somebody is waiting on, not a provision.
_TIMEOUT_S = 20.0

# The Config API path for Gateways. BeyondTrust still serves this at `/jumpoint` — the
# product was renamed to Gateway, the path was not, and renaming it here would 404. See
# tests/test_gateway_terminology.py for the rule.
_GATEWAY_PATH = "/api/config/v1/jumpoint"


class PRATenantError(Exception):
    """A refusal naming the tenant and the likely cause."""


async def get_token(client: httpx.AsyncClient, tenant) -> str:
    """OAuth2 client credentials against this tenant's appliance.

    Mirrors ``pra_api_service._token``, against a Tenant rather than the config keys. A
    401 here has one common cause worth naming: the OAuth client in PRA is a separate
    object from the account an operator logs in with.
    """
    if not tenant.client_id or not tenant.secret:
        raise PRATenantError(
            f"tenant {tenant.name!r} has no OAuth client id and secret. PRA authenticates "
            f"with an API account created under Management > API Configuration, not with "
            f"a user login.")
    resp = await client.post(
        f"{tenant.api_base}/oauth2/token",
        auth=(tenant.client_id, tenant.secret),
        data={"grant_type": "client_credentials"})
    if resp.status_code in (400, 401, 403):
        raise PRATenantError(
            f"PRA rejected tenant {tenant.name!r}'s credentials ({resp.status_code}). The "
            f"client id and secret come from an API account in PRA "
            f"(Management > API Configuration).")
    if resp.status_code != 200:
        raise PRATenantError(
            f"PRA token request for tenant {tenant.name!r} failed ({resp.status_code}).")
    token = (resp.json() or {}).get("access_token", "")
    if not token:
        raise PRATenantError(
            f"PRA answered 200 for tenant {tenant.name!r} with no access_token in the body")
    return token


async def list_gateways(tenant) -> list[dict]:
    """Every Gateway this tenant's appliance knows about.

    Normalised to ``{id, name, connected, nodes}``. ``nodes`` matters more here than it
    looks: a Gateway is a *cluster*, and re-installing a POV's Gateway on a rebuilt broker
    VM adds a node rather than replacing one — PRA parks the dead node and the Gateway
    keeps the same name. So "is it there?" is not the question worth asking; "is a node of
    it connected?" is.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S,
                                     headers={"Accept": "application/json"}) as client:
            token = await get_token(client, tenant)
            resp = await client.get(f"{tenant.api_base}{_GATEWAY_PATH}",
                                    headers={"Authorization": f"Bearer {token}"})
    except PRATenantError:
        raise
    except Exception as exc:  # noqa: BLE001
        # The exception itself is logged, never carried outward — see
        # bt_tenant_verify._http_reason for why a caught exception's text does not belong
        # in anything a user reads.
        logger.warning("PRA gateway list for tenant %s failed", tenant.name, exc_info=True)
        raise PRATenantError(
            f"could not reach PRA at {tenant.api_base} ({type(exc).__name__}) — check the "
            f"hostname, DNS and any firewall.") from None

    if resp.status_code != 200:
        raise PRATenantError(
            f"PRA refused the Gateway list for tenant {tenant.name!r} "
            f"({resp.status_code}). The API account needs permission to read Jumpoints.")
    items = resp.json()
    if not isinstance(items, list):
        raise PRATenantError(
            f"PRA returned an unexpected Gateway list for tenant {tenant.name!r}")
    return [_gateway(it) for it in items if isinstance(it, dict)]


def _gateway(raw: dict) -> dict:
    """One Gateway, normalised.

    ``connected`` is read from more than one spelling on purpose. Which field an appliance
    reports varies by version, and a missing key must read as **unknown**, never as
    "disconnected" — telling an operator their Gateway is down because we did not
    recognise a field name is worse than saying nothing.
    """
    connected = None
    for key in ("connected", "is_connected", "online"):
        if key in raw:
            connected = bool(raw.get(key))
            break
    nodes = raw.get("nodes") or raw.get("cluster_nodes") or []
    return {
        "id": raw.get("id"),
        "name": str(raw.get("name") or ""),
        "connected": connected,
        "nodes": len(nodes) if isinstance(nodes, list) else 0,
    }


async def find_gateway(tenant, name: str) -> dict | None:
    """One Gateway by name, or None. Names are matched exactly — an appliance may hold two
    that differ only in case, and guessing between them is worse than not finding one."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    for row in await list_gateways(tenant):
        if row["name"] == wanted:
            return row
    return None


# ── writes ───────────────────────────────────────────────────────────────────

async def _call(tenant, method: str, path: str, *, json: dict | None = None,
                allow_404: bool = False):
    """One authenticated Config API call. Returns ``(status, body)``.

    The refusals name the PRA-side fix. A 403 on a write is nearly always the API
    account's permissions rather than the credentials — the token request already proved
    those — and saying "forbidden" alone sends an operator to re-paste a secret that works.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S,
                                     headers={"Accept": "application/json"}) as client:
            token = await get_token(client, tenant)
            resp = await client.request(method, f"{tenant.api_base}{path}", json=json,
                                        headers={"Authorization": f"Bearer {token}"})
    except PRATenantError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("PRA %s %s for tenant %s failed", method, path, tenant.name,
                       exc_info=True)
        raise PRATenantError(
            f"could not reach PRA at {tenant.api_base} ({type(exc).__name__}) — check the "
            f"hostname, DNS and any firewall.") from None

    if resp.status_code == 404 and allow_404:
        return 404, None
    if resp.status_code == 403:
        raise PRATenantError(
            f"PRA refused {method} {path} for tenant {tenant.name!r} (403). The API "
            f"account needs Configuration API access with permission to manage Gateways "
            f"(Management > API Configuration).")
    if resp.status_code == 422:
        raise PRATenantError(
            f"PRA rejected {method} {path} for tenant {tenant.name!r} (422): "
            f"{_detail(resp)}")
    if resp.status_code >= 400:
        raise PRATenantError(
            f"PRA {method} {path} for tenant {tenant.name!r} failed ({resp.status_code}).")
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    return resp.status_code, body


def _detail(resp) -> str:
    """The field errors from a 422, compact. PRA answers ``{"errors": {field: [msg]}}``
    or ``{"message": ...}``; anything else is reported by status alone."""
    try:
        body = resp.json()
    except ValueError:
        return "no detail"
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                             for k, v in errors.items())
        if body.get("message"):
            return str(body["message"])
    return "no detail"


async def create_gateway(tenant, name: str, *, comments: str = "") -> dict:
    """Create a clustered Linux Gateway and return the raw resource.

    **Clustered Linux is not a style choice — it is the only shape that has a deploy
    key.** The spec says ``docker_deploy_key`` "will only be set for Linux Gateways that
    have clustered set to true", and the POV's Gateway is a container on the broker VM, so
    any other shape creates a Gateway nothing here can install. It is also the shape the
    cloud gateway hosts already use, which is why a rebuilt broker VM rejoins as a node.

    Shell Jump and Protocol Tunnel are switched on because the wire-up's jump items need
    them; PRA's defaults leave Shell Jump off, and the first SSH jump would then fail at
    session start rather than here.
    """
    payload = {
        "name": name,
        "platform": "linux-x86",
        "clustered": True,
        "enabled": True,
        "shell_jump_enabled": True,
        "protocol_tunnel_enabled": True,
        "comments": (comments or "")[:1024],
    }
    _status, body = await _call(tenant, "POST", _GATEWAY_PATH, json=payload)
    if not isinstance(body, dict) or not body.get("id"):
        raise PRATenantError(
            f"PRA created a Gateway for tenant {tenant.name!r} and returned no id for it. "
            f"Look for {name!r} in the appliance and remove it before trying again.")
    return body


async def get_gateway(tenant, gateway_id) -> dict | None:
    """The raw Gateway resource, or None when PRA no longer has it."""
    status, body = await _call(tenant, "GET", f"{_GATEWAY_PATH}/{gateway_id}",
                               allow_404=True)
    if status == 404:
        return None
    return body if isinstance(body, dict) else None


async def delete_gateway(tenant, gateway_id) -> bool:
    """Delete a Gateway. True when it was deleted, False when it was already gone.

    PRA deletes the Gateway's nodes and every Asset it owns along with it, so the only
    caller is a teardown that has already removed the POV's jump items and is deleting a
    Gateway it created itself.
    """
    status, _body = await _call(tenant, "DELETE", f"{_GATEWAY_PATH}/{gateway_id}",
                                allow_404=True)
    return status != 404


async def count_gateway_nodes(tenant, gateway_id) -> int | None:
    """How many nodes the Gateway has, from ``/jumpoint/{id}/node``.

    The list endpoint does not carry nodes at all ("Node resources are not returned in
    the response"), so a count read off a list row is always zero. ``None`` means the
    appliance did not answer, which a caller must not report as zero nodes.
    """
    try:
        status, body = await _call(tenant, "GET", f"{_GATEWAY_PATH}/{gateway_id}/node",
                                   allow_404=True)
    except PRATenantError:
        return None
    if status == 404:
        return None
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        # The spec types this response as a single JumpointNode; tolerate either shape.
        return 1 if body.get("id") else 0
    return None
