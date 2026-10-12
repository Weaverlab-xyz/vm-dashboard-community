"""PRA Gateways: is each Gateway connected, and is each host the dashboard runs up?

Two views of the same thing, kept apart because they fail differently:

* **PRA's view** -- every Gateway the appliance knows, and whether a node of it is
  connected (``pra_tenant_api.list_gateways``). A Gateway PRA shows as disconnected is
  one no jump through it will work, whoever deployed it.
* **The hosts the dashboard deployed** (``Gateway`` rows) -- the status the gateway
  reconcile pass writes from what it observed: degraded (host up, no gateway task) and
  missing (a requested host that is gone) included.

``connected`` that PRA did not report reads as unknown, never as disconnected, for the
reason ``pra_tenant_api._gateway`` gives.
"""
from __future__ import annotations

import logging

from .. import core

logger = logging.getLogger(__name__)

_HOST_STATUS = {"running": core.OK, "degraded": core.WARN, "error": core.FAIL,
                "missing": core.FAIL, "provisioning": core.UNKNOWN,
                "deleting": core.UNKNOWN}


def host_items(rows) -> list:
    """Items for the dashboard-deployed Gateway hosts. Pure."""
    out = []
    for row in rows:
        if row.status == "deleted":
            continue
        status = _HOST_STATUS.get(row.status, core.UNKNOWN)
        where = " ".join(p for p in (row.cloud, row.region or row.zone or "") if p)
        detail = f"{row.status.capitalize()}" + (f" in {where}" if where else "")
        if row.error and status != core.OK:
            detail += f": {core.text(row.error, 300)}"
        out.append(core.Item(
            object_id=f"host:{row.id}", name=row.name,
            kind="Gateway host (shared)" if row.managed else "Gateway host",
            href="/containers#gateways",
            checks=[core.check("host", "Host", status, detail)]))
    return out


def gateway_items(tenant_name: str, tenant_id: str, gateways: list) -> list:
    """Items for the Gateways one PRA tenant reports. Pure."""
    out = []
    for gw in gateways:
        connected = gw.get("connected")
        status = core.OK if connected else (core.UNKNOWN if connected is None else core.FAIL)
        detail = ("A node is connected." if connected else
                  "PRA did not report whether a node is connected." if connected is None
                  else "No node is connected, so no jump through this Gateway will work.")
        out.append(core.Item(
            object_id=f"pra:{tenant_id or 'configured'}:{gw.get('id')}",
            name=core.text(gw.get("name") or gw.get("id"), 120),
            kind=f"PRA Gateway ({tenant_name})", href="/containers#gateways",
            checks=[core.check("connected", "Connected", status, detail)]))
    return out


async def collect(db) -> list:
    from ... import pra_tenant_api
    from ...bt_tenant_service import _from_settings, to_tenant
    from ....database import BeyondTrustTenant, Gateway
    items = host_items(db.query(Gateway).all())

    rows = (db.query(BeyondTrustTenant)
            .filter(BeyondTrustTenant.kind == "pra", BeyondTrustTenant.is_active.is_(True))
            .all())
    tenants = []
    for row in rows:
        try:
            tenants.append((row.id, to_tenant(row)))
        except Exception:  # noqa: BLE001
            logger.warning("health: PRA tenant %s credential did not resolve", row.id,
                           exc_info=True)
    if not rows:
        legacy = _from_settings("pra")
        if legacy is not None:
            tenants.append(("", legacy))

    for tenant_id, tenant in tenants:
        try:
            gateways = await pra_tenant_api.list_gateways(tenant)
        except pra_tenant_api.PRATenantError as exc:
            # Written for an operator by pra_tenant_api; the tenant check carries the
            # credential side, this records that the list itself could not be read.
            items.append(core.Item(
                object_id=f"pra:{tenant_id or 'configured'}:list", name=tenant.name,
                kind="PRA tenant", href="/containers#gateways",
                checks=[core.check("list", "Gateway list", core.FAIL, str(exc))]))
            continue
        items.extend(gateway_items(tenant.name, tenant_id, gateways))
    return items
