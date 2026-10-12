"""BeyondTrust tenants: does each PRA and Password Safe credential still work?

The same token handshake as the tenant page's **Verify** (``bt_tenant_verify.verify``),
on a schedule, with the result also recorded on the tenant row so that page agrees with
this one. A demo instance that never created a tenant row is checked through its legacy
instance settings, which is what every product call there uses.

Entitle tenants are not checked here: ``bt_tenant_verify`` explains why there is no
side-effect-free read that proves an Entitle token. Entitle's health is the JIT sweep
(``collectors/entitle``).
"""
from __future__ import annotations

import logging

from .. import core

logger = logging.getLogger(__name__)

_KIND_FLAGS = {"pra": "pra_enabled", "password_safe": "password_safe_enabled"}


def _tenants(db, kind: str) -> list:
    """``[(object_id, Tenant, row_id)]`` for one kind: every active row, else the
    legacy instance settings when configured."""
    from ...bt_tenant_service import _from_settings, to_tenant
    from ....database import BeyondTrustTenant
    rows = (db.query(BeyondTrustTenant)
            .filter(BeyondTrustTenant.kind == kind, BeyondTrustTenant.is_active.is_(True))
            .order_by(BeyondTrustTenant.name).all())
    out = []
    for row in rows:
        try:
            out.append((f"{kind}:{row.id}", to_tenant(row), row.id))
        except Exception:  # noqa: BLE001 - a credential that cannot be resolved is a result
            logger.warning("health: tenant %s credential did not resolve", row.id,
                           exc_info=True)
            out.append((f"{kind}:{row.id}", None, row.id))
    if not rows:
        legacy = _from_settings(kind)
        if legacy is not None:
            out.append((f"{kind}:configured", legacy, ""))
    return out


async def collect(db) -> list:
    from ... import bt_tenant_service, bt_tenant_verify, feature_flags
    items = []
    for kind, flag in _KIND_FLAGS.items():
        if not feature_flags.enabled(flag):
            continue
        label = bt_tenant_service.LABELS[kind]
        for object_id, tenant, row_id in _tenants(db, kind):
            name = f"{label}: {tenant.name if tenant else row_id}"
            if tenant is None:
                status, detail = core.FAIL, ("Its credential could not be read from where "
                                             "it is stored (vault reference or key).")
            else:
                try:
                    ok, detail = await bt_tenant_verify.verify(tenant)
                    status = core.OK if ok else core.FAIL
                except bt_tenant_service.BTTenantError as exc:
                    # A refusal about the request (no URL), written for an operator.
                    status, detail = core.UNKNOWN, str(exc)
                except Exception as exc:  # noqa: BLE001 - see bt_tenant_verify's docstring
                    logger.warning("health: verify of %s raised", object_id, exc_info=True)
                    status, detail = core.FAIL, (f"The check failed unexpectedly "
                                                 f"({type(exc).__name__}).")
                if row_id and status != core.UNKNOWN:
                    bt_tenant_service.record_result(
                        db, row_id, error="" if status == core.OK else detail)
            items.append(core.Item(
                object_id=object_id, name=name, kind=label,
                href="/pov/templates" if row_id else "/settings",
                checks=[core.check("credential", "Credential and API", status,
                                   detail if status != core.OK else
                                   (detail or "The API issued a token."))]))
    return items
