"""What a POV leaves behind: the account of what was demonstrated, and to whom.

Everything else in this feature is about a POV while it is alive. This is the one part
written for after it is not.

The record already existed and was already complete — ``run_env_destroy`` marks the row
``destroyed`` rather than deleting it because it is "the inventory record of something that
existed", and ``pov_use_cases.destroy_note`` deliberately keeps the checklist as that
record's contents. What was missing is that **nothing could reach it**:
``api/pov.list_managed`` filters destroyed rows out, so a finished POV appeared nowhere and
you needed its raw uuid to see it again. The evidence survived teardown and the way to it
did not, which is the same as losing it at exactly the moment somebody wants it — a
renewal conversation, weeks later.

Two functions, and the split is the two questions actually asked:

  :func:`archive`  "which evaluations have we run?" — a light row per POV, no per-VM
                   queries, because it renders a list nobody acts on
  :func:`build`    "what happened in that one?" — the whole account of a single POV

Read-only, both. Nothing here writes, and nothing here reaches a tenant or a lab platform:
a summary that made network calls would fail for exactly the POVs it exists to describe,
whose platform environment is gone.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import PovAccessor, PovEnvironment, PovUseCaseProgress
from . import pov_use_cases

logger = logging.getLogger(__name__)

# How many past POVs the archive returns unasked. An SE accumulates these at a few a month,
# so a year of work fits comfortably and nobody pages through it.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _products(env: PovEnvironment) -> list:
    """Which BeyondTrust products this evaluation was wired into, by name.

    Read off the row rather than from a live check, which is the only thing that could
    work for a destroyed POV — and the honest thing for a live one too. The question is
    what the evaluation COVERED, and that was decided when its tenants were chosen.
    """
    return [label for label, wired in (
        ("Privileged Remote Access", env.pra_tenant_id),
        ("Password Safe", env.ps_tenant_id),
        ("Entitle", env.entitle_tenant_id),
    ) if wired]


# ── the archive ──────────────────────────────────────────────────────────────

def archive(db: Session, *, limit: int = DEFAULT_LIMIT) -> dict:
    """Past POVs, newest first, with just enough to choose one.

    Deliberately NOT ``api/pov._serialize``. That builds five describes per row — gateway,
    resource broker, wire-up, share, accessors — each of which asks a question about a
    living environment, and every one of them is both meaningless and a wasted query for a
    POV that no longer exists. This is a list somebody reads and clicks; it needs a name, a
    date, and enough of the shape to recognise which evaluation it was.

    The coverage counts come from one aggregate over the progress rows rather than from
    ``pov_use_cases.summary_for``: that resolves the whole catalog per POV to work out what
    was in scope, which is the right answer on the detail page and a lot of work to repeat
    down a list.
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    rows = (db.query(PovEnvironment)
              .filter(PovEnvironment.status == "destroyed")
              .order_by(PovEnvironment.created_at.desc())
              .limit(limit).all())
    if not rows:
        return {"environments": [], "truncated": False}

    ids = [r.id for r in rows]
    counts: dict = {}
    for env_id, state, kind, note in (
            db.query(PovUseCaseProgress.environment_id, PovUseCaseProgress.state,
                     PovUseCaseProgress.checked_by_kind, PovUseCaseProgress.note)
              .filter(PovUseCaseProgress.environment_id.in_(ids)).all()):
        slot = counts.setdefault(env_id, {"done": 0, "skipped": 0, "notes": 0,
                                          "by_customer": 0})
        if state == "done":
            slot["done"] += 1
        elif state == "skipped":
            slot["skipped"] += 1
        if note:
            slot["notes"] += 1
        if kind == pov_use_cases.KIND_ACCESSOR:
            slot["by_customer"] += 1

    accessors: dict = {}
    for env_id, in db.query(PovAccessor.environment_id).filter(
            PovAccessor.environment_id.in_(ids)).all():
        accessors[env_id] = accessors.get(env_id, 0) + 1

    return {
        "environments": [{
            "id": r.id,
            "name": r.name,
            "platform": r.platform,
            "created_at": _iso(r.created_at),
            "created_by": r.created_by or "",
            "workgroup": r.workgroup or "",
            "products": _products(r),
            "accessors_issued": accessors.get(r.id, 0),
            **{"done": 0, "skipped": 0, "notes": 0, "by_customer": 0},
            **counts.get(r.id, {}),
        } for r in rows],
        # Said rather than implied: a list silently cut at fifty is one an SE trusts and
        # should not.
        "truncated": len(rows) == limit,
    }


# ── one POV's account ────────────────────────────────────────────────────────

def build(db: Session, env: PovEnvironment) -> dict:
    """Everything worth saying about one evaluation, alive or finished.

    Card states still resolve against the POV's product mix, which for a destroyed POV
    reports what it WAS wired into — the honest answer for a record, and the same answer
    its own page gave while it ran.

    Only cards somebody touched are returned. A summary listing every untouched card would
    be the catalog again, and the catalog is not what happened.
    """
    catalog = pov_use_cases.describe(db, env)

    covered, skipped, notes = [], [], []
    for group in catalog["groups"]:
        for card in group["use_cases"]:
            progress = card.get("progress") or {}
            if not (progress.get("state") or progress.get("note")):
                continue
            entry = {
                "id": card["id"],
                "title": card["title"],
                "persona": group["persona"],
                "persona_label": group["label"],
                "minutes": card["minutes"],
                "state": progress.get("state", ""),
                "note": progress.get("note", ""),
                "by": progress.get("by", ""),
                "by_kind": progress.get("by_kind", ""),
                "at": progress.get("at", ""),
            }
            if progress.get("state") == "done":
                covered.append(entry)
            elif progress.get("state") == "skipped":
                skipped.append(entry)
            if progress.get("note"):
                notes.append(entry)

    # Every accessor ever issued, revoked ones included — the live list on the Access tab
    # is for managing access, and this one is for saying whether the customer was ever in
    # it themselves.
    issued = (db.query(PovAccessor)
                .filter(PovAccessor.environment_id == env.id)
                .order_by(PovAccessor.created_at).all())

    return {
        "environment": {
            "id": env.id,
            "name": env.name,
            "platform": env.platform,
            "status": env.status,
            "created_at": _iso(env.created_at),
            "created_by": env.created_by or "",
            "workgroup": env.workgroup or "",
            "template": env.template_name or env.template_id or "",
            "products": _products(env),
            "finished": env.status == "destroyed",
        },
        "coverage": catalog["summary"],
        "covered": covered,
        "skipped": skipped,
        "notes": notes,
        "by_persona": _by_persona(catalog),
        "customer": {
            "accessors_issued": len(issued),
            # The claim worth making, and the only one the data supports: somebody outside
            # the account ticked something themselves. "They were given a login" is not the
            # same evidence.
            "took_part": any(e["by_kind"] == pov_use_cases.KIND_ACCESSOR
                             for e in covered + skipped + notes),
            "notes_from_customer": sum(
                1 for e in notes if e["by_kind"] == pov_use_cases.KIND_ACCESSOR),
        },
    }


def _by_persona(catalog: dict) -> list:
    """Coverage per role, so the summary maps to who was in the room.

    Roles with nothing in scope are dropped — a Password-Safe-only evaluation has several,
    and reporting "0 of 0" against each is noise in a document somebody reads once.
    """
    out = []
    for group in catalog["groups"]:
        in_scope = [c for c in group["use_cases"] if c["state"] != "out_of_scope"]
        if not in_scope:
            continue
        out.append({
            "persona": group["persona"],
            "label": group["label"],
            "in_scope": len(in_scope),
            "done": sum(1 for c in in_scope
                        if (c.get("progress") or {}).get("state") == "done"),
            "skipped": sum(1 for c in in_scope
                           if (c.get("progress") or {}).get("state") == "skipped"),
        })
    return out
