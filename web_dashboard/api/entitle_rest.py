"""
Entitle Remote Adapter for dashboard user identity — the one integration the
dashboard hosts itself.

Every other Phase 2 integration is a Cloud Function, because Entitle needs an
endpoint and the target is somewhere the dashboard has to reach. Here the dashboard
**is** the target system: the thing being granted is a permission on a dashboard
user. A function hop would add a network round trip, a second credential and a
second thing to deploy, and buy nothing.

Replaces the Entra-group indirection (Entitle adds the user to
``dashboard-<scope>-<level>``, the group appears in the OIDC ``groups`` claim, and
``_complete_oauth_login`` maps it) with a direct grant. That indirection has two
real costs this removes:

  * it only works for Entra. Local users and any other OIDC provider cannot be
    granted anything, which undercuts the point of the generic-OIDC support.
  * a grant does not take effect until the user's NEXT LOGIN, and a revoke does not
    either — so "just in time" was, in practice, "some time after your next login".

Routes are the same Remote Adapter contract the function adapters serve
(docs.beyondtrust.com/entitle/docs/open-api-definition), minus ``create_actor`` /
``delete_actor``: dashboard users are real accounts that already exist, so this
integration is NOT ephemeral. Entitle grants an existing actor a role.

    GET  /api/entitle/rest/get_assets            permission scopes
    GET  /api/entitle/rest/get_actors            dashboard users
    GET  /api/entitle/rest/get_all_permissions   who currently holds what
    POST /api/entitle/rest/give_access           {asset, actor_identifier, role_code}
    POST /api/entitle/rest/revoke_access         same
    POST /api/entitle/rest/check_config          {config}
"""
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import config_service
from ..services import entitle_user_grants as grants
from .auth import PERMISSION_LEVELS, PERMISSION_SCOPES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/entitle/rest", tags=["entitle-rest"])

def _require_secret(authorization: str = Header(default=""),
                    x_entitle_secret: str = Header(default="")) -> None:
    """Authenticate Entitle. Fails CLOSED.

    A dedicated shared secret rather than a Personal Access Token, deliberately:
    a PAT inherits its owning user's permissions, and an endpoint whose whole job
    is granting permissions must not authenticate with a credential that already
    has some. This is the same bearer contract the function adapters verify, so
    Entitle's ``headers`` config is identical for all of them.
    """
    expected = (config_service.get("entitle_rest_secret")
                or getattr(settings, "entitle_rest_secret", "") or "").strip()
    if not expected:
        # Never serve an unauthenticated grant endpoint just because it is
        # unconfigured. The 503 says "not set up", which is true and actionable.
        raise HTTPException(
            status_code=503,
            detail={"code": "entitle_rest_not_configured",
                    "message": "entitle_rest_secret is not set; this endpoint is closed"})
    presented = (x_entitle_secret or "").strip()
    if not presented:
        raw = (authorization or "").strip()
        presented = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        # Identical response for missing and wrong, so probing tells nothing.
        raise HTTPException(status_code=401, detail="unauthorized")


# ── Read routes ──────────────────────────────────────────────────────────────

@router.get("/get_assets", dependencies=[Depends(_require_secret)])
def get_assets():
    """Every permission scope, plus administrator."""
    assets = [grants.asset_for(scope, PERMISSION_LEVELS) for scope in PERMISSION_SCOPES]
    assets.append(grants.admin_asset())
    return {"next": "", "data": {"assets": assets}}


# POV accessors are excluded from every route in this file, and it is a security
# boundary rather than tidiness. An accessor is a prospect's ephemeral login into ONE
# POV; this integration grants permissions on the DASHBOARD, up to and including
# administrator. `_apply` resolves an actor by scanning users and matching a name, so an
# accessor reaching that lookup is a direct escalation path from "can tick a checklist"
# to "is an admin". The filter goes on the query, not on the response, so nothing here
# can leak one back into the actor set by taking a different route through the data.
_NOT_AN_ACCESSOR = User.accessor_env_id.is_(None)


@router.get("/get_actors", dependencies=[Depends(_require_secret)])
def get_actors(db: Session = Depends(get_db)):
    """Every dashboard user — local and OIDC alike, which is the point: the Entra
    -group path could only ever see Entra users.

    POV accessors are not among them; see _NOT_AN_ACCESSOR above."""
    actors = []
    for user in db.query(User).filter(User.is_active.is_(True), _NOT_AN_ACCESSOR).all():
        actors.append({
            "identifier": user.username,
            "name": user.username,
            "type": "dashboard_user",
            "email": str(getattr(user, "email", "") or ""),
        })
    return {"next": "", "data": {"actors": actors}}


@router.get("/get_all_permissions", dependencies=[Depends(_require_secret)])
def get_all_permissions(db: Session = Depends(get_db)):
    """What Entitle currently holds — its OWN grants only.

    Reporting the admin baseline or group-derived permissions here would invite
    Entitle to reconcile them away, revoking access it never granted.

    **Keyed by asset id, not a list.** Entitle validates this response against its
    ``Get All Permission Response`` schema, where ``actors_permissions`` is
    ``map[asset_id] -> [{actor_id, role_code, direct_member}]``; a list is rejected
    outright and the integration keeps whatever it last believed. Every served asset
    is a key even with nobody on it, so an empty one reads as "nobody holds this"
    rather than "not reported". ``assets_permissions`` is the asset-to-asset half —
    which other assets confer access to this one — and dashboard scopes do not nest,
    so it is empty; the role codes an asset offers belong in ``get_assets``.
    """
    actors_permissions = {grants.ADMIN_ASSET: []}
    for scope in PERMISSION_SCOPES:
        actors_permissions[f"{grants.ASSET_PREFIX}{scope}"] = []
    for user in db.query(User).filter(_NOT_AN_ACCESSOR).all():
        for asset_id, role in grants.held_by_asset(user):
            actors_permissions.setdefault(asset_id, []).append(
                {"actor_id": user.username, "role_code": role, "direct_member": True})
    return {"next": "", "data": {"actors_permissions": actors_permissions,
                                 "assets_permissions": {}}}


# ── Write routes ─────────────────────────────────────────────────────────────

def _apply(db: Session, payload: dict, *, grant: bool) -> dict:
    scope = grants.scope_from_asset(payload)
    if not scope:
        raise HTTPException(status_code=404, detail="unknown asset")
    if scope != grants.ADMIN_ASSET and scope not in PERMISSION_SCOPES:
        raise HTTPException(status_code=404, detail=f"unknown scope {scope!r}")

    role = str(payload.get("role_code") or "").strip().lower()
    if scope == grants.ADMIN_ASSET:
        if role and role != grants.ADMIN_ROLE:
            raise HTTPException(status_code=400, detail=f"unknown role {role!r}")
        role = grants.ADMIN_ROLE
    elif role not in PERMISSION_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown role_code {role!r} (expected one of {PERMISSION_LEVELS})")

    identifier = str(payload.get("actor_identifier") or "").strip()
    user = next((u for u in db.query(User).filter(_NOT_AN_ACCESSOR).all()
                 if grants.matches_actor(u, identifier)), None)
    if not user:
        raise HTTPException(status_code=404, detail=f"unknown actor {identifier!r}")

    changed = (grants.grant if grant else grants.revoke)(user, scope, role)
    if changed:
        db.commit()
    logger.info("entitle-rest %s user=%s scope=%s role=%s changed=%s",
                "give_access" if grant else "revoke_access",
                user.username, scope, role, changed)
    return {"data": {"actor_identifier": user.username, "role_code": role,
                     "asset": scope, "changed": changed}}


@router.post("/give_access", dependencies=[Depends(_require_secret)])
async def give_access(request: Request, db: Session = Depends(get_db)):
    return _apply(db, await request.json(), grant=True)


@router.post("/revoke_access", dependencies=[Depends(_require_secret)])
async def revoke_access(request: Request, db: Session = Depends(get_db)):
    """Idempotent: revoking something not held is success, not a 404.

    Entitle retries, and a revoke that errors leaves standing access behind — the
    one outcome this integration exists to prevent.
    """
    return _apply(db, await request.json(), grant=False)


@router.post("/check_config", dependencies=[Depends(_require_secret)])
def check_config(db: Session = Depends(get_db)):
    """Reached only with a valid secret, so arriving here already proves the half
    of the configuration most likely to be wrong."""
    return {"data": {
        "valid": True,
        "scopes": list(PERMISSION_SCOPES),
        "levels": list(PERMISSION_LEVELS),
        "users": db.query(User).filter(_NOT_AN_ACCESSOR).count(),
    }}
