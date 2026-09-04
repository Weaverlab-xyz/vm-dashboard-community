"""Entitle Remote Adapter for POV accessors — the **ephemeral** half.

``api/entitle_rest.py`` is the standing adapter, and its docstring says why it omits
``create_actor`` / ``delete_actor``: dashboard users are real accounts that already exist,
so Entitle grants an existing actor a role. This file is the opposite case in every
respect. A POV accessor does not exist until a prospect asks for one and must not exist
after the POV is gone, so **Entitle owns the account's whole lifecycle** — and in its
Ephemeral Accounts mode ``create_actor`` IS the grant and ``delete_actor`` is the revoke.
There is no ``give_access`` here, deliberately, rather than as an oversight.

    GET  /api/pov/accessor/rest/get_assets            live POVs, "pov:<env_id>"
    GET  /api/pov/accessor/rest/get_all_permissions
    POST /api/pov/accessor/rest/create_actor          mint; returns the credentials
    POST /api/pov/accessor/rest/delete_actor          delete
    POST /api/pov/accessor/rest/check_config

Three things are copied from ``functions/fnworkloads/db_grant.py`` rather than reinvented,
because each was learned the expensive way there:

* **The identity arrives in ``provisioning_data``** in Ephemeral mode, not in ``actor``.
  Reading only ``actor`` is what made every real ephemeral grant come back 400 "needs an
  actor" — the field is simply somewhere else.
* **``delete_actor`` refuses any username without the ephemeral prefix.** It is the only
  thing standing between this integration and an operator's account, and it is checked on
  the name from the request rather than on anything a row says.
* **``create_actor`` returns the credentials**, because handing them to the requester is
  the entire point of the call.

Its secret is its own (``pov_accessor_rest_secret``), not the standing adapter's: an
endpoint whose job is minting logins must not authenticate with a credential that already
grants permissions. Same fail-closed shape either way — 503 when unset, and one response
for missing and wrong so probing tells nothing.

**Registering this adapter with Entitle is a later slice.** Until then an SE points an
Entitle REST integration at these routes by hand, which is also how the Ephemeral-mode
question in ``entitle_registration_service.register_rest`` gets answered — see
docs/profiles/pov/design/use-cases.md.
"""
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import settings
from ..database import PovAccessor, PovEnvironment, get_db
from ..services import config_service, pov_accessor_service, pov_env_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pov/accessor/rest", tags=["pov-accessor-rest"])

# The asset identifier Entitle sees for a POV. Prefixed so a bare environment id can never
# be mistaken for one, and so a second asset type here later is a prefix rather than a
# guess about what a string means.
_ASSET_PREFIX = "pov:"


def _require_secret(authorization: str = Header(default=""),
                    x_entitle_secret: str = Header(default="")) -> None:
    """Authenticate Entitle. Fails CLOSED.

    Its own key rather than ``entitle_rest_secret``: that one authenticates an integration
    that grants dashboard permissions, and this one mints logins. Sharing it would mean a
    leak of either is a leak of both, on an instance that does customer work.
    """
    expected = (config_service.get("pov_accessor_rest_secret")
                or getattr(settings, "pov_accessor_rest_secret", "") or "").strip()
    if not expected:
        # Never serve an unauthenticated account-minting endpoint because nobody
        # configured it. 503 says "not set up", which is true and actionable.
        raise HTTPException(
            status_code=503,
            detail={"code": "pov_accessor_rest_not_configured",
                    "message": "pov_accessor_rest_secret is not set; this endpoint is closed"})
    presented = (x_entitle_secret or "").strip()
    if not presented:
        raw = (authorization or "").strip()
        presented = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


def _live_povs(db: Session) -> list:
    """The POVs an accessor could be minted for.

    Destroying and destroyed rows are excluded: an asset Entitle can request against but
    this dashboard would refuse to mint for is a grant that fails at the last step, in
    front of whoever asked.
    """
    return (db.query(PovEnvironment)
              .filter(PovEnvironment.status.notin_(("destroying", "destroyed")))
              .order_by(PovEnvironment.created_at.desc()).all())


def _identity(payload: dict) -> str:
    """Where the requester's identity can arrive, in precedence order.

    Ephemeral mode sends ``provisioning_data``; the standing contract (and every
    hand-rolled call) sends ``actor``. Reading only one of them is the bug this ordering
    exists to prevent — see the module docstring.
    """
    for source in (payload.get("provisioning_data"), payload.get("actor"), payload):
        if not isinstance(source, dict):
            continue
        for key in ("email", "identifier", "name", "user_email"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _requested_env(db: Session, payload: dict) -> PovEnvironment | None:
    identifier = str((payload.get("asset") or {}).get("identifier") or "").strip()
    if not identifier.startswith(_ASSET_PREFIX):
        return None
    return pov_env_service.get(db, identifier[len(_ASSET_PREFIX):])


# ── Read routes ──────────────────────────────────────────────────────────────

@router.get("/get_assets", dependencies=[Depends(_require_secret)])
def get_assets(db: Session = Depends(get_db)):
    """Every POV somebody could be granted access to.

    No ``get_actors``: in Ephemeral mode Entitle owns the account lifecycle and tracks its
    own actors, and sending that route fails its validation.
    """
    assets = [{
        "identifier": f"{_ASSET_PREFIX}{env.id}",
        "name": env.name,
        "type": "pov_environment",
        "role_options": [{
            "code": "accessor", "display_name": "POV accessor",
            "available": True, "permissions": ["pov:accessor"],
        }],
    } for env in _live_povs(db)]
    return {"next": "", "data": {"assets": assets}}


@router.get("/get_all_permissions", dependencies=[Depends(_require_secret)])
def get_all_permissions(db: Session = Depends(get_db)):
    """Who currently holds access to which POV — this integration's own grants only.

    **Keyed by asset id**: Entitle validates this against ``Get All Permission
    Response``, where ``actors_permissions`` is ``map[asset_id] -> [...]``, and a
    list fails validation with the accessor sync still green. The key is also the
    only place the binding appears — an accessor exists for ONE POV, and the flat
    list said "this person is an accessor" without ever saying of what.

    ``assets_permissions`` is the asset-to-asset half (which other assets grant
    access to this one); POVs do not contain one another, so it is empty.
    """
    actors_permissions = {f"{_ASSET_PREFIX}{env.id}": [] for env in _live_povs(db)}
    for row in db.query(PovAccessor).filter(PovAccessor.revoked_at.is_(None)).all():
        # A POV that is being torn down is not in the asset list any more, so its
        # accessors are not reported against an asset Entitle does not know.
        held = actors_permissions.get(f"{_ASSET_PREFIX}{row.environment_id}")
        if held is None:
            continue
        held.append({"actor_id": row.username, "role_code": "accessor",
                     "direct_member": True})
    return {"next": "", "data": {"actors_permissions": actors_permissions,
                                 "assets_permissions": {}}}


# ── Write routes ─────────────────────────────────────────────────────────────

@router.post("/create_actor", dependencies=[Depends(_require_secret)])
async def create_actor(request: Request, db: Session = Depends(get_db)):
    """Mint an accessor for one POV, and hand back its credentials.

    In Ephemeral mode this call **is** the grant — there is no ``give_access`` to follow —
    so an account minted here without access to the named POV would be a grant that
    reports success and gives the requester a login that can reach nothing. The asset is
    therefore required rather than optional: without one there is no POV to bind to, and
    binding to "all of them" is not a thing an accessor may be.
    """
    payload = await request.json()
    identity = _identity(payload)
    if not identity:
        raise HTTPException(
            status_code=400,
            detail="create_actor needs an email or identifier (in provisioning_data, "
                   "actor, or the body itself)")

    env = _requested_env(db, payload)
    if env is None:
        raise HTTPException(
            status_code=404,
            detail=f"create_actor needs an asset naming a live POV "
                   f"('{_ASSET_PREFIX}<environment id>')")

    try:
        row, password = pov_accessor_service.mint(
            db, env, email=identity, source=pov_accessor_service.SOURCE_ENTITLE,
            by="entitle",
            entitle_request_id=str(payload.get("request_id") or "")[:64],
            entitle_actor_id=identity[:128])
    except pov_accessor_service.AccessorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("pov-accessor-rest create_actor pov=%s identity=%s username=%s",
                env.id, identity, row.username)
    # The credentials ARE the point of create_actor: Entitle hands them to the requester.
    #
    # NESTED, not flat. Entitle validates this ``data`` against ``Provisioned Actor
    # Data``, which has exactly two properties — ``actor`` and ``login_info`` — and is
    # additionalProperties: False at the top level AND inside ``actor``, where ``email``
    # is required. The flat version every adapter here shipped was rejected in full
    # ("Additional properties are not allowed ('database', 'host', 'identifier', ...
    # were unexpected)"), and in Ephemeral mode that happens AFTER the account exists:
    # Entitle records the request as failed, the requester is told nothing, and
    # delete_actor is only driven from what Entitle believes it provisioned — so the
    # accessor outlives its grant. See functions/fnruntime/entitle.py, which is where
    # the same fix lives for the adapters that run as cloud functions.
    return {"data": {
        "actor": {
            "identifier": row.username,
            "name": row.username,
            "type": "pov_accessor",
            # The requester's address, not the minted username: it is the only link
            # back to the person who asked, and the schema requires the field.
            "email": identity,
        },
        "login_info": {
            "username": row.username,
            "password": password,
            "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        },
    }}


@router.post("/delete_actor", dependencies=[Depends(_require_secret)])
async def delete_actor(request: Request, db: Session = Depends(get_db)):
    """Remove an accessor. Idempotent, and it will not touch anything it did not mint.

    Deleting something already gone is success, not a 404: Entitle retries, and a delete
    that errors leaves a live login behind — the one outcome this integration exists to
    prevent.
    """
    payload = await request.json()
    username = str(payload.get("actor_identifier")
                   or (payload.get("actor") or {}).get("identifier") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="delete_actor needs actor_identifier")

    if not pov_accessor_service.is_accessor_username(username):
        # The only thing standing between this endpoint and an operator's account. Checked
        # on the name from the REQUEST, before any lookup, so a row that disagreed with
        # the name could not talk it into a delete either.
        logger.warning("pov-accessor-rest: refusing delete_actor for %r — not an "
                       "accessor username", username)
        raise HTTPException(
            status_code=403,
            detail="this integration may only delete POV accessor accounts")

    row = pov_accessor_service.by_username(db, username)
    if row is None:
        return {"data": {"actor_identifier": username, "deleted": False}}
    pov_accessor_service.revoke(db, row, reason="deleted by Entitle", by="entitle")
    return {"data": {"actor_identifier": username, "deleted": True}}


@router.post("/check_config", dependencies=[Depends(_require_secret)])
def check_config(db: Session = Depends(get_db)):
    """Reached only with a valid secret, so arriving here already proves the half of the
    configuration most likely to be wrong."""
    return {"data": {
        "valid": True,
        "asset_prefix": _ASSET_PREFIX,
        "povs": len(_live_povs(db)),
        "accessors": db.query(PovAccessor).filter(
            PovAccessor.revoked_at.is_(None)).count(),
    }}
