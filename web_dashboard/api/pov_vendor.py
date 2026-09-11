"""PRA Vendor Onboarding for one POV — the SE's half of ``services/pov_vendor_access``.

    GET    /api/pov/managed/{env_id}/vendor              this POV's vendor state + users
    POST   /api/pov/managed/{env_id}/vendor              create the policy + vendor group
    DELETE /api/pov/managed/{env_id}/vendor              remove both
    POST   /api/pov/managed/{env_id}/vendor/users        mint one vendor login
    DELETE /api/pov/managed/{env_id}/vendor/users/{id}   revoke one

Shaped after ``api/pov_accessor.py`` and carrying two of its decisions on purpose:

**Every refusal is 409, never 404.** These routes are mounted behind
``pov_environments_enabled``, and a gated router answers 404 when the feature is off — so
a 404 for "your appliance does not serve the vendor API" would read as "the POV feature is
not on here" and send an SE to the wrong screen entirely. The one real 404 is a POV or a
vendor-user row that does not exist.

**A user id from another POV gets 404, not 403.** Same reason ``revoke_accessor`` does it:
the two cases stay indistinguishable to a caller probing for ids.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import PovEnvironment, User, get_db
from ..services import pov_env_service, pov_vendor_access
from ..services.pra_tenant_api import PRATenantError
from .auth import get_current_user, require_permission, require_pov_env_access

logger = logging.getLogger(__name__)

# Sharing api/pov.py's PREFIX does not share its dependencies -- a router is gated by what
# it declares, not by what another router mounted at the same path declares. api/pov_accessor
# .py carried the same sentence and the same gap until the permission scopes went in, and
# this module was written against the branch point, so it arrived ungated: any authenticated
# user could read, create and delete a PRA vendor group on ANY POV, and mint a third-party
# login into somebody else's customer environment.
#
# So, the same two guards api/pov_accessor.py settled on, for the same reasons:
#
#   pov:write -- minting and revoking a vendor's login hands out a CREDENTIAL into the
#                customer's environment. That is not a read, and a stakeholder holding
#                {"pov": ["read","use"]} must not reach it.
#   require_pov_env_access -- the instance gate, so an SE narrowed to one POV cannot open a
#                vendor group on another. Every route here names an {env_id}, and it answers
#                404 rather than 403 so the id stays unprobeable.
router = APIRouter(
    prefix="/api/pov",
    tags=["pov-vendors"],
    dependencies=[Depends(require_permission("pov", "write")),
                  Depends(require_pov_env_access)],
)


class VendorGroupRequest(BaseModel):
    """How long, and from where.

    ``days`` is always clamped to the POV's own expiry, and then to PRA's 1-365, so a
    number here is a ceiling rather than a promise.

    ``network_restrictions`` is PRA's *Network address allow list* — IP prefixes, one per
    entry. There is deliberately no email-domain field: that list and the self-registration
    portal are ``/login`` settings with no Configuration API, so offering them here would
    be a form that silently discards what somebody typed.
    """
    days: int | None = None
    network_restrictions: list[str] = []


class VendorUserRequest(BaseModel):
    """Who this login is for. Neither field is trusted for identity.

    The username is generated and carries the ``povvnd_`` prefix; the email is a label so
    an SE can tell two vendors apart, and a delivery address only in the sense that PRA
    shows it on the account — **nothing is emailed**, because the Configuration API has no
    invite call. The password comes back in the response and is handed over by hand.
    """
    email: str = ""
    full_name: str = ""


def _env_or_404(db: Session, env_id: str) -> PovEnvironment:
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    return env


def _refusal(exc: Exception) -> HTTPException:
    """One conversion, so every route answers the same way. 409, never 404 — see module."""
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/managed/{env_id}/vendor")
async def vendor_state(env_id: str, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """This POV's vendor group and its live logins. No network calls, never a password."""
    env = _env_or_404(db, env_id)
    return pov_vendor_access.describe(db, env)


@router.post("/managed/{env_id}/vendor")
async def register_vendor(env_id: str, payload: VendorGroupRequest,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Create this POV's Group Policy and Vendor Group in its own PRA appliance.

    Synchronous rather than a job, like the Entitle registration next to it: it is three
    API calls somebody is watching, and a job row would put a page refresh between the
    button and its result.
    """
    env = _env_or_404(db, env_id)
    try:
        return await pov_vendor_access.register(
            db, env, by=getattr(current_user, "username", "") or "",
            days=payload.days, network_restrictions=payload.network_restrictions)
    except (pov_vendor_access.VendorAccessError, PRATenantError) as exc:
        raise _refusal(exc) from None


@router.delete("/managed/{env_id}/vendor")
async def remove_vendor(env_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Remove the Vendor Group and its Group Policy. PRA deletes the group's users with it."""
    env = _env_or_404(db, env_id)
    try:
        await pov_vendor_access.deregister(
            db, env, by=getattr(current_user, "username", "") or "")
    except (pov_vendor_access.VendorAccessError, PRATenantError) as exc:
        raise _refusal(exc) from None
    return pov_vendor_access.describe(db, env)


@router.post("/managed/{env_id}/vendor/users", status_code=201)
async def create_vendor_user(env_id: str, payload: VendorUserRequest,
                             db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    """Mint one vendor login. **The password is in this response and nowhere else.**

    There is no reveal endpoint and nothing stores it — PRA holds only its hash. A vendor
    who has lost theirs is replaced, which is one click and leaves an audit line. The
    account is created with "must change password at next login" set, so the value handed
    over stops working as soon as it has been used once.
    """
    env = _env_or_404(db, env_id)
    try:
        row, password = await pov_vendor_access.mint_user(
            db, env, email=payload.email, full_name=payload.full_name,
            by=getattr(current_user, "username", "") or "")
    except (pov_vendor_access.VendorAccessError, PRATenantError) as exc:
        raise _refusal(exc) from None
    return {"user": pov_vendor_access.describe_user(row), "password": password,
            **pov_vendor_access.describe(db, env)}


@router.delete("/managed/{env_id}/vendor/users/{user_id}")
async def revoke_vendor_user(env_id: str, user_id: str, db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    """Delete one vendor login from the appliance and stamp the row."""
    env = _env_or_404(db, env_id)
    row = pov_vendor_access.get_user(db, user_id)
    # Checked against the env in the path, not just by id — see the module docstring.
    if row is None or row.environment_id != env.id:
        raise HTTPException(status_code=404, detail="No such vendor login on this POV")
    try:
        await pov_vendor_access.revoke_user(
            db, env, row, reason="revoked by an operator",
            by=getattr(current_user, "username", "") or "")
    except (pov_vendor_access.VendorAccessError, PRATenantError) as exc:
        raise _refusal(exc) from None
    return {"revoked": user_id, **pov_vendor_access.describe(db, env)}
