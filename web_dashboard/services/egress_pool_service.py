"""The dashboard's own OUTBOUND address pool, read from the platform hosting it.

Why this exists: the managed-node firewalls (Portainer, Rancher) admit the dashboard by
source address, and ``managed_node_service.detect_egress_ip`` learns that address by
asking an echo service. That is exactly right for a laptop, a VM or anything behind a
NAT gateway — one address, and the echo sees it. It is exactly WRONG on an Azure
Container Apps environment with no NAT gateway, which SNATs outbound traffic from a
shared pool of several hundred addresses and picks one per destination. The echo
service's flow and the node's flow then leave from DIFFERENT addresses, so the /32 the
dashboard pins is never the one the node sees: the readiness poll passes on a lucky
attempt, the bootstrap is dropped, and re-detecting returns the same wrong address.

The platform publishes that pool — ``properties.outboundIpAddresses`` on the Container
App — so the dashboard reads it with its own managed identity and the allow-list admits
the whole pool. No operator paste, and no list to go stale: every firewall refresh
re-reads it.

Needs ONE grant on the hosting side: the Container App's managed identity must be able
to read the Container App (``Reader`` on the app, or on its resource group — the
worker and the UI are two apps, and both call the node). When that is missing the pool
keeps its last good value and :func:`status` carries a message naming the exact
``az`` command to run. Everywhere other than Container Apps this is a no-op.
"""
import base64
import ipaddress
import json
import logging
import os
import re

import httpx

from . import config_service

logger = logging.getLogger(__name__)

# Persisted, feature-NEUTRAL: the pool is a property of the dashboard's host, so
# Portainer and Rancher share one copy. Written by whichever process refreshed last;
# both apps in one Container Apps environment egress from the same pool.
POOL_KEY = "dashboard_egress_pool"
SOURCE_KEY = "dashboard_egress_pool_source"
ERROR_KEY = "dashboard_egress_pool_error"

# Optional overrides, read from the ENVIRONMENT because they describe the deployment,
# not a setting anyone should edit from the UI.
RESOURCE_ID_ENV = "DASHBOARD_HOST_RESOURCE_ID"
# ``off`` disables discovery and CLEARS the pool on the next refresh -- for an
# environment given a NAT gateway, whose one stable address the echo detects fine.
DISABLE_ENV = "DASHBOARD_EGRESS_POOL"
IDENTITY_CLIENT_ID_ENV = "DASHBOARD_HOST_IDENTITY_CLIENT_ID"

PLATFORM_ACA = "azure-container-apps"

_ARM = "https://management.azure.com"
_ARM_RESOURCE = "https://management.azure.com/"
_ACA_API_VERSION = "2024-03-01"
_ARG_API_VERSION = "2021-03-01"
_IDENTITY_API_VERSION = "2019-08-01"
_TIMEOUT_S = 10.0

# A sanity bound, not a cloud limit (those are applied per cloud at ingress time). A
# Consumption environment publishes ~400; anything past this is not an egress pool
# anyone should be writing into a firewall.
MAX_POOL = 1000

# Container App names are lowercase alphanumerics and hyphens. Validated before the
# name goes into a Resource Graph query string.
_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class EgressPoolError(Exception):
    """Discovery could not complete. The message is shown to the operator verbatim."""


def hosting_platform(env=None) -> str:
    """The platform the dashboard runs on, when it is one with a discoverable pool.

    Container Apps injects ``CONTAINER_APP_NAME`` into every replica."""
    env = os.environ if env is None else env
    if (env.get(DISABLE_ENV) or "").strip().lower() in ("off", "0", "false", "no"):
        return ""
    if (env.get("CONTAINER_APP_NAME") or "").strip():
        return PLATFORM_ACA
    return ""


def pool_cidrs() -> list:
    """The last discovered pool as /32s (``[]`` when none was ever discovered)."""
    csv = config_service.get(POOL_KEY) or ""
    return [c.strip() for c in csv.split(",") if c.strip()]


def status() -> dict:
    """Read-only summary for the firewall readouts. No network call."""
    cidrs = pool_cidrs()
    return {
        "platform": hosting_platform(),
        "cidrs": cidrs,
        "count": len(cidrs),
        "source": config_service.get(SOURCE_KEY) or "",
        "error": config_service.get(ERROR_KEY) or "",
    }


def _set_if_changed(key: str, value: str) -> None:
    if (config_service.get(key) or "") != value:
        config_service.set(key, value)


def _token_oid(token: str) -> str:
    """The ``oid`` claim of an Entra access token, for the remediation message only.

    NOT verification — nothing is trusted from this. It names the identity the operator
    has to grant, which is otherwise a portal lookup."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("oid") or "")
    except Exception:  # noqa: BLE001
        return ""


def _grant_hint(app: str, oid: str, resource_id: str) -> str:
    scope = resource_id or (f"$(az containerapp show -n {app} -g <resource-group> "
                            f"--query id -o tsv)")
    who = oid or f"$(az containerapp show -n {app} -g <resource-group> --query identity.principalId -o tsv)"
    return (f"Grant it Reader on the Container App (or on its resource group, which covers "
            f"the worker and the UI app with one grant): az role assignment create "
            f"--assignee-object-id {who} --assignee-principal-type ServicePrincipal "
            f"--role Reader --scope {scope}")


def parse_pool(values) -> list:
    """Normalise ``outboundIpAddresses`` to sorted, de-duplicated IPv4 /32s. Pure.

    IPv4 only: that is what the echo detection and every node ingress path handle, and
    a v6 entry in a v4 source list is rejected outright by some clouds."""
    out = set()
    for v in values or []:
        try:
            ip = ipaddress.ip_address(str(v).strip())
        except ValueError:
            continue
        if ip.version == 4:
            out.add(f"{ip}/32")
    return sorted(out, key=lambda c: ipaddress.ip_network(c))


async def _aca_token(client: httpx.AsyncClient, env) -> str:
    endpoint = (env.get("IDENTITY_ENDPOINT") or "").strip()
    header = (env.get("IDENTITY_HEADER") or "").strip()
    app = env.get("CONTAINER_APP_NAME", "")
    if not (endpoint and header):
        raise EgressPoolError(
            f"The Container App {app!r} has no managed identity, so the dashboard cannot "
            f"read its own outbound address pool. Enable one: az containerapp identity "
            f"assign -n {app} -g <resource-group> --system-assigned — then restart the "
            f"revision.")
    params = {"api-version": _IDENTITY_API_VERSION, "resource": _ARM_RESOURCE}
    client_id = (env.get(IDENTITY_CLIENT_ID_ENV) or "").strip()
    if client_id:
        params["client_id"] = client_id
    r = await client.get(endpoint, params=params, headers={"X-IDENTITY-HEADER": header})
    if r.status_code != 200:
        raise EgressPoolError(
            f"The managed identity endpoint refused a token (HTTP {r.status_code}): "
            f"{r.text[:200]}")
    token = (r.json() or {}).get("access_token") or ""
    if not token:
        raise EgressPoolError("The managed identity endpoint returned no access token.")
    return token


async def _aca_resource(client, token: str, env) -> tuple:
    """``(resource_id, outboundIpAddresses)`` for the Container App this replica is."""
    app = (env.get("CONTAINER_APP_NAME") or "").strip()
    auth = {"Authorization": f"Bearer {token}"}
    rid = (env.get(RESOURCE_ID_ENV) or "").strip()
    oid = _token_oid(token)
    if rid:
        r = await client.get(f"{_ARM}{rid}", params={"api-version": _ACA_API_VERSION},
                             headers=auth)
        if r.status_code in (401, 403, 404):
            # ARM answers 404 as well as 403 to a principal that cannot read a resource.
            raise EgressPoolError(
                f"The dashboard's managed identity cannot read {rid} (HTTP "
                f"{r.status_code}). {_grant_hint(app, oid, rid)}")
        if r.status_code != 200:
            raise EgressPoolError(f"Reading {rid} failed (HTTP {r.status_code}): "
                                  f"{r.text[:200]}")
        return rid, ((r.json() or {}).get("properties") or {}).get("outboundIpAddresses")

    if not _APP_NAME_RE.match(app):
        raise EgressPoolError(f"Unexpected Container App name {app!r}; set "
                              f"{RESOURCE_ID_ENV} to its resource id.")
    # The replica knows its app NAME but not its subscription or resource group, and
    # Resource Graph is the one API that can find a resource by name across both. It
    # only returns what the identity may read, so "no rows" is the missing-grant case.
    query = ("resources | where type =~ 'microsoft.app/containerapps' "
             f"and name =~ '{app}' | project id, ips = properties.outboundIpAddresses")
    r = await client.post(f"{_ARM}/providers/Microsoft.ResourceGraph/resources",
                          params={"api-version": _ARG_API_VERSION},
                          headers=auth, json={"query": query})
    if r.status_code != 200:
        raise EgressPoolError(f"Resource Graph query failed (HTTP {r.status_code}): "
                              f"{r.text[:200]}. {_grant_hint(app, oid, '')}")
    rows = (r.json() or {}).get("data") or []
    if isinstance(rows, dict):  # objectArray is the default, but a table shape exists
        cols = [c.get("name") for c in rows.get("columns") or []]
        rows = [dict(zip(cols, row)) for row in rows.get("rows") or []]
    if not rows:
        raise EgressPoolError(
            f"The dashboard's managed identity cannot see its own Container App "
            f"{app!r}. {_grant_hint(app, oid, '')}")
    if len(rows) > 1:
        raise EgressPoolError(
            f"{len(rows)} Container Apps named {app!r} are visible to the dashboard's "
            f"identity; set {RESOURCE_ID_ENV} to this one's resource id.")
    return rows[0].get("id") or "", rows[0].get("ips")


async def discover(env=None, client: httpx.AsyncClient = None) -> tuple:
    """``(source, cidrs)`` read live from the hosting platform. Raises EgressPoolError.

    ``("", [])`` off a platform with a discoverable pool. ``client`` is injectable for
    tests."""
    env = os.environ if env is None else env
    if hosting_platform(env) != PLATFORM_ACA:
        return "", []
    own = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT_S, trust_env=False)
    try:
        token = await _aca_token(client, env)
        rid, ips = await _aca_resource(client, token, env)
    except httpx.HTTPError as exc:
        raise EgressPoolError(f"Could not reach Azure to read the outbound address pool: "
                              f"{type(exc).__name__}: {exc}") from exc
    finally:
        if own:
            await client.aclose()
    cidrs = parse_pool(ips)
    if not cidrs:
        raise EgressPoolError(f"{rid or 'The Container App'} publishes no IPv4 outbound "
                              f"addresses.")
    if len(cidrs) > MAX_POOL:
        raise EgressPoolError(
            f"{rid} publishes {len(cidrs)} outbound addresses, more than the {MAX_POOL} "
            f"this will write into a firewall. Attach a NAT Gateway to the environment's "
            f"subnet for one stable address instead.")
    return f"{PLATFORM_ACA}:{rid}", cidrs


async def refresh(env=None, client: httpx.AsyncClient = None) -> dict:
    """Re-read the pool and persist it. NEVER raises; returns :func:`status`.

    Only callers that are about to APPLY a firewall should call this — it writes
    config, and on Container Apps it costs two ARM calls.

    On failure the LAST GOOD pool is kept: a transient ARM error must not narrow a
    working allow-list back to one /32 and lock the dashboard out of its own node. The
    failure is recorded instead, so the readout can say what to fix."""
    try:
        source, cidrs = await discover(env=env, client=client)
    except EgressPoolError as exc:
        logger.warning("Dashboard egress pool: %s", exc)
        _set_if_changed(ERROR_KEY, str(exc))
        return status()
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning("Dashboard egress pool discovery failed: %s", exc, exc_info=True)
        _set_if_changed(ERROR_KEY, f"Discovery failed: {type(exc).__name__}: {exc}")
        return status()
    # Off a pool platform ``source`` is "" and ``cidrs`` is [], which CLEARS a pool left
    # behind by a previous host — a dashboard moved off Container Apps must stop
    # admitting several hundred addresses it no longer egresses from.
    before = pool_cidrs()
    _set_if_changed(POOL_KEY, ",".join(cidrs))
    _set_if_changed(SOURCE_KEY, source)
    _set_if_changed(ERROR_KEY, "")
    if before != cidrs:
        logger.info("Dashboard egress pool: %d outbound address(es) from %s (was %d)",
                    len(cidrs), source or "no pool platform", len(before))
    return status()
