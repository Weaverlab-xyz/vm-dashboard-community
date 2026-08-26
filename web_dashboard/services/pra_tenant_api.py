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
