"""BeyondTrust tenants: does each PRA and Password Safe credential still work?

The same token handshake as the tenant page's **Verify** (``bt_tenant_verify.verify``),
on a schedule, with the result also recorded on the tenant row so that page agrees with
this one. A demo instance that never created a tenant row is checked through its legacy
instance settings, which is what every product call there uses.

Entitle tenants are not checked here: ``bt_tenant_verify`` explains why there is no
side-effect-free read that proves an Entitle token. Entitle's health is the JIT sweep
(``collectors/entitle``).

A registered PRA tenant also gets a **Shared Asset Group** check: how many POVs still keep
their jump items in the tenant's appliance-wide Asset Group (``pov_wireup.shared_group_povs``),
so a vendor cannot be scoped to them. It is always OK -- those POVs work, and the move is
the SE's call -- and :func:`shared_group_note` totals it above the list. At zero across
every tenant nothing reads a tenant's ``jump_group_name`` any more, which is the day the
fallback in ``pov_wireup.tenant_override`` can go.
"""
from __future__ import annotations

import logging

from .. import core

logger = logging.getLogger(__name__)

_KIND_FLAGS = {"pra": "pra_enabled", "password_safe": "password_safe_enabled"}

# "### A POV wired before this will say so": what the move does and what it costs.
DOCS_HREF = "/docs/profiles/pov/customer-access#a-pov-wired-before-this-will-say-so"
# Named in the detail, up to this many; the rest are a count.
_NAMED = 5


def shared_group_check(db, row_id: str) -> dict:
    """POVs still in this PRA tenant's appliance-wide Asset Group. Never not OK.

    ``shared`` rides along in the stored check so :func:`shared_group_note` can total it.
    """
    from ...pov_wireup import shared_group_povs
    names = [env.name for env in shared_group_povs(db, row_id)]
    if not names:
        detail = "No POV keeps its jump items in this tenant's appliance-wide Asset Group."
    else:
        shown = ", ".join(names[:_NAMED]) + (f" and {len(names) - _NAMED} more"
                                              if len(names) > _NAMED else "")
        detail = (f"{len(names)} POV(s) still keep their jump items in this tenant's "
                  f"appliance-wide Asset Group, so no vendor can be scoped to them: "
                  f"{shown}. Each one's Access tab offers Move to its own Asset Group.")
    return dict(core.check("shared_group", "Shared Asset Group", core.OK, detail),
                shared=len(names))


def shared_group_note(items: list) -> dict | None:
    """The fleet line: how many POVs still rely on a tenant's appliance-wide Asset Group.

    ``items`` are the stored rows as the snapshot serialises them; only registered PRA
    tenants carry the check, so a build with none says nothing.
    """
    counts = [c.get("shared") or 0 for it in items for c in it.get("checks") or []
              if c.get("key") == "shared_group"]
    if not counts:
        return None
    total, tenants = sum(counts), sum(1 for n in counts if n)
    if total:
        text = (f"{total} POV(s) on {tenants} PRA tenant(s) still keep their jump items in "
                f"the tenant's appliance-wide Asset Group. They work, but no vendor can be "
                f"scoped to them; once none are left, nothing reads a tenant's Asset Group "
                f"setting.")
    else:
        text = ("Every wired POV has its own Asset Group: nothing reads a PRA tenant's "
                "appliance-wide Asset Group setting any more.")
    return {"text": text, "href": DOCS_HREF, "link": "Moving a POV to its own Asset Group"}


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
            checks = [core.check("credential", "Credential and API", status,
                                 detail if status != core.OK else
                                 (detail or "The API issued a token."))]
            # A POV names a registered tenant row, never the legacy instance settings.
            if kind == "pra" and row_id:
                checks.append(shared_group_check(db, row_id))
            items.append(core.Item(
                object_id=object_id, name=name, kind=label,
                href="/pov/templates" if row_id else "/settings",
                checks=checks))
    return items
