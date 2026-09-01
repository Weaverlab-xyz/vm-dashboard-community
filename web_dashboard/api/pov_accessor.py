"""POV accessors, from the two sides that hold a session.

  GET    /api/pov/managed/{id}/accessors        the POV's live accessors  (SE)
  POST   /api/pov/managed/{id}/accessors        mint one; credentials ONCE (SE)
  DELETE /api/pov/managed/{id}/accessors/{aid}  revoke one                (SE)
  GET    /api/pov/accessor/self                 the accessor's own POV    (accessor)

Two routers in one module because they are two halves of one feature, and one file is
where the relationship between them stays visible. Their AUTH is what differs, and sharply:

* the SE routes sit on the POV router's gate and take an environment id in the path, like
  every other ``/api/pov/managed`` route;
* ``/self`` takes **no environment id at all.** It resolves the POV from
  ``current_user.accessor_env_id``, so there is no parameter to tamper with and therefore
  no ownership check for a future edit to forget. It is the only route in
  ``api/auth._ACCESSOR_ALLOWED_PREFIXES``, and that list is an allowlist precisely so this
  stays the only one without anybody having to remember.

Entitle's own calls arrive at ``api/pov_accessor_rest.py`` instead — no session, a shared
secret, and a different set of things to be careful about.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import PovEnvironment, User, get_db
from ..services import pov_accessor_service, pov_env_service
from .auth import get_current_user

logger = logging.getLogger(__name__)

# The SE half rides the POV router's prefix, so it inherits that gate at mount time.
router = APIRouter(prefix="/api/pov", tags=["pov-accessors"])
# The accessor's own half. A separate router only so the allowlisted path is spelled in
# one obvious place; it is mounted with the same gate.
self_router = APIRouter(prefix="/api/pov/accessor", tags=["pov-accessors"])


class AccessorRequest(BaseModel):
    """Who this login is for. Both fields optional and neither is trusted for identity.

    An email is a label an SE types so they can tell two accessors apart in a list — not a
    login (the username is generated) and not a delivery address (nothing is emailed). It
    is stored because "which of these three is Dana's?" is otherwise unanswerable a week
    later.
    """
    email: str = ""
    full_name: str = ""
    days: int | None = None


def _env_or_404(db: Session, env_id: str) -> PovEnvironment:
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    return env


@router.get("/managed/{env_id}/accessors")
async def list_accessors(env_id: str, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """This POV's live accessors. Never a password — see the service's describe_one."""
    env = _env_or_404(db, env_id)
    return pov_accessor_service.describe(db, env)


@router.post("/managed/{env_id}/accessors", status_code=201)
async def create_accessor(env_id: str, payload: AccessorRequest,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Mint an accessor. **The password is in this response and nowhere else.**

    There is no reveal endpoint, unlike the share link's. That link's password has to be
    re-readable because an SE reads it to a customer days later; an accessor that has lost
    its password is replaced instead, which is one click, leaves an audit line, and ends
    with a credential only one person ever saw.

    This is also the path that makes the slice usable before the Entitle integration is
    registered — see docs/design/pov-use-cases.md on why that half is deliberately later.
    """
    env = _env_or_404(db, env_id)
    try:
        row, password = pov_accessor_service.mint(
            db, env, email=payload.email, full_name=payload.full_name,
            source=pov_accessor_service.SOURCE_MANUAL,
            by=getattr(current_user, "username", "") or "", days=payload.days)
    except pov_accessor_service.AccessorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"accessor": pov_accessor_service.describe_one(row), "password": password,
            **pov_accessor_service.describe(db, env)}


@router.delete("/managed/{env_id}/accessors/{accessor_id}")
async def revoke_accessor(env_id: str, accessor_id: str, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Revoke one. Deletes the login; the binding stays as the record that it existed."""
    env = _env_or_404(db, env_id)
    row = pov_accessor_service.get(db, accessor_id)
    # Checked against the env in the path, not just by id: an id from another POV must not
    # be revocable through this one's URL, and a 404 rather than a 403 keeps the two cases
    # indistinguishable to a caller probing for ids.
    if row is None or row.environment_id != env.id:
        raise HTTPException(status_code=404, detail="No such accessor on this POV")
    pov_accessor_service.revoke(db, row, reason="revoked by an operator",
                                by=getattr(current_user, "username", "") or "")
    return {"revoked": accessor_id, **pov_accessor_service.describe(db, env)}


# ── the accessor's own view ──────────────────────────────────────────────────

@self_router.get("/self")
async def accessor_self(db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """The POV this login is attached to, and its use-case checklist.

    Read-only in this slice: ticking a card, leaving a note and the page that renders both
    land next. The identity has to exist and be provable first, and this is what proves it.

    Serves any authenticated caller, not only accessors — an SE hitting it simply has no
    ``accessor_env_id`` and gets the 409 below, which is a truthful answer rather than a
    second permission rule to keep in step with the allowlist.
    """
    try:
        return pov_accessor_service.self_view(db, current_user)
    except pov_accessor_service.AccessorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
