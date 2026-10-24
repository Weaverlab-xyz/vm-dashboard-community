"""Closing a POV out: use cases, the summary, sign-offs and sample-activity seeds.

Registered on ``api/pov.py``'s router, not a router of its own, so the router-level gates
(the ``pov:read`` floor and the per-POV instance gate) cover every route here exactly as they
did when this lived in that file. ``api/pov.py`` imports this module last, after everything
imported from it below is defined.
"""

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import (job_service, pov_env_service, pov_seed, pov_signoff, pov_summary,
                        pov_use_case_scope, pov_use_cases)
from .auth import get_current_user
from .pov import _POV_USE, _POV_WRITE_OWN, _env_or_404, logger, router


# ── use cases ────────────────────────────────────────────────────────────────
#
# The one place in the use-case stack that WRITES. That is worth naming, because a card
# navigates and never starts work — a card that POSTed a deploy would make the registry
# something that can spend money. A tick is not that: it spends nothing, builds nothing and
# touches no tenant. It also does not live in the registry at all; services/pov_cards stays
# a pure read, and the write lands here, behind the same auth every other POV action
# already carries.


class UseCaseRequest(BaseModel):
    # `done` by default, so the common case is an empty body.
    state: str = "done"
    # None means "leave the note alone", which is what an absent field has to mean now that
    # the customer writes notes on these rows too: an SE pressing Done sends no note, and
    # treating that as "" would erase what the customer had just written. "" still clears.
    note: str | None = None


@router.get("/managed/{env_id}/use-cases")
def list_use_cases(env_id: str, include_all: bool = Query(False, alias="all"),
                   db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """This POV's catalog, with each card's own state.

    Complete for every product MIX: a POV with no tenant for a product still gets that
    product's cards, rendered out of scope -- the mix decides a card's state, never its
    presence. What does remove a card is the POV's chosen use cases (the Setup tab's
    first step): a Password-Safe-only POV that chose Password Safe sees only those.

    ``all=1`` keeps the cards outside that choice, flagged ``in_pov: false``, for the
    Setup tab's picker. The customer's page never asks for it.
    """
    env = _env_or_404(db, env_id)
    return pov_use_cases.describe(db, env, include_excluded=include_all)


class UseCaseScopeRequest(BaseModel):
    """Which use cases this POV tests. ``products`` None clears the choice."""
    products: list[str] | None = None
    excluded: list[str] = []


@router.put("/managed/{env_id}/use-case-scope", dependencies=_POV_WRITE_OWN)
def set_use_case_scope(env_id: str, payload: UseCaseScopeRequest,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Choose the POV's use cases: Password Safe only, PRA only, Total PASM, and so on.

    Progress is never deleted here. A card taken out of the POV keeps its tick and notes
    and gets them back if it is put back in.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    try:
        products, excluded = pov_use_case_scope.validate(payload.products, payload.excluded)
    except pov_use_case_scope.ScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pov_use_case_scope.write(env, products, excluded)
    db.commit()
    logger.info("POV %s: use cases set to %s (%d unticked) by %s", env.id,
                ",".join(products) if products else "the whole catalog", len(excluded),
                getattr(current_user, "username", "?"))
    return pov_use_cases.describe(db, env, include_excluded=True)


@router.post("/managed/{env_id}/use-cases/{card_id}", dependencies=_POV_USE)
def set_use_case(env_id: str, card_id: str, payload: UseCaseRequest,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Tick a card off, or mark it skipped. Idempotent.

    A 400 on an unknown card id rather than a stored row: ``services/pov_cards`` and
    ``services/pov_runbooks`` are the allowlist between them, and these rows deliberately
    outlive registry edits, so an unvalidated id would outlive the typo that made it.
    """
    env = _env_or_404(db, env_id)
    try:
        progress = pov_use_cases.set_state(
            db, env, card_id,
            state=payload.state, note=payload.note,
            by=getattr(current_user, "username", "") or "",
            by_kind=pov_use_cases.KIND_SE)
    except pov_use_cases.UseCaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"card_id": card_id, "progress": progress,
            "summary": pov_use_cases.summary_for(db, env)}


@router.delete("/managed/{env_id}/use-cases/{card_id}", dependencies=_POV_USE)
def clear_use_case(env_id: str, card_id: str, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """Un-tick a card. Removing a row nobody wrote is success, not a 404 — the button is a
    toggle, and a double-click must not be an error."""
    env = _env_or_404(db, env_id)
    try:
        removed = pov_use_cases.clear(db, env, card_id)
    except pov_use_cases.UseCaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"card_id": card_id, "cleared": removed,
            "summary": pov_use_cases.summary_for(db, env)}


@router.get("/managed/{env_id}/summary")
def get_summary(env_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """One evaluation's account. Serves a destroyed POV as readily as a live one.

    No status filter, deliberately: this is the endpoint written for after a POV is over,
    and refusing the finished ones would leave it describing only the POVs whose story is
    not finished being told.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    return pov_summary.build(db, env)


@router.post("/managed/{env_id}/signoff", dependencies=_POV_WRITE_OWN)
def sign_off(env_id: str, db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
    """The SE's sign-off on what this POV covered. Works on a destroyed POV too.

    The customer's own is ``POST /api/pov/accessor/self/signoff``. Both write the same row
    shape, told apart by ``signed_by_kind``, and both are audited.
    """
    env = _env_or_404(db, env_id)
    actor = getattr(current_user, "username", "") or ""
    try:
        out = pov_signoff.record(db, env, by=actor, by_kind=pov_use_cases.KIND_SE)
    except pov_signoff.SignoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    job_service.log_audit(db, actor, "pov_signoff", target_vm=env.name,
                          details={"environment_id": env.id, "signoff_id": out["id"],
                                   "kind": pov_use_cases.KIND_SE})
    return {"signoff": out}


@router.delete("/managed/{env_id}/signoff/{signoff_id}", dependencies=_POV_WRITE_OWN)
def withdraw_sign_off(env_id: str, signoff_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Withdraw a sign-off, the SE's or the customer's. The row is kept, marked withdrawn:
    a sign-off that could be deleted would not be worth asking for."""
    env = _env_or_404(db, env_id)
    actor = getattr(current_user, "username", "") or ""
    try:
        out = pov_signoff.revoke(db, env, signoff_id, by=actor)
    except pov_signoff.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    job_service.log_audit(db, actor, "pov_signoff_withdrawn", target_vm=env.name,
                          details={"environment_id": env.id, "signoff_id": signoff_id})
    return {"signoff": out}


@router.get("/managed/{env_id}/seeds")
def list_seeds(env_id: str, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """The sample-activity sets this POV can run, and what each did last. No calls."""
    return pov_seed.describe(db, _env_or_404(db, env_id))


@router.post("/managed/{env_id}/seeds/{seed_id}", status_code=202,
             dependencies=_POV_WRITE_OWN)
def run_seed(env_id: str, seed_id: str, db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
    """Queue one sample-activity set. It WRITES to this POV's tenant, so it is audited.

    A run already in flight is returned rather than duplicated; see services/pov_seed for
    what each set writes and what it can never do.
    """
    env = _env_or_404(db, env_id)
    actor = getattr(current_user, "username", "") or ""
    try:
        job_id = pov_seed.enqueue(db, env, seed_id, by=actor)
    except pov_seed.SeedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    job_service.log_audit(db, actor, "pov_seed_requested", target_vm=env.name,
                          details={"environment_id": env.id, "seed": seed_id,
                                   "job_id": job_id})
    return {"job_id": job_id}
