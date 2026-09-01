"""Per-POV use cases: what this environment can demonstrate, and what has been shown.

The third axis, and the one that only exists because a POV is not an instance.

``feature_flags.install_profile`` gates — it decides whether a feature exists here at all.
``personas`` curates — it decides which role's story leads. Neither can answer the question
an SE actually has in front of a customer, which is **"can I run this on THIS POV?"** A POV
carries its own PRA, Password Safe and Entitle tenants in three independent columns
precisely because a Password-Safe-only evaluation, a PRA + Password Safe one and an
all-three one are all normal shapes. On a POV instance every one of those has
``pra_enabled`` on, so the flag cannot tell them apart.

So this module resolves cards against a **POV row**, and it keeps two properties:

  * **It never subtracts.** Every persona and every card is present for every product mix.
    A card whose product this POV does not include is rendered and explained as
    ``out_of_scope`` — the same promise the persona layer makes, one layer down. A mix
    decides a card's STATE and nothing else.
  * **``personas`` still knows nothing about the database.** That module's dependency rule
    is load-bearing (``api/docs_pages`` imports it deliberately), so it takes a dict of
    booleans and this module is the one that owns ``PovEnvironment``. The import direction
    is one-way: ``pov_use_cases`` imports ``personas``, never the reverse.

The progress half is the other reason this exists. A POV runs for weeks across many
sessions and several people, so which demos have actually been run is state worth keeping —
and the accessor slice will let the prospect tick them off themselves, which is why every
write records who did it and in what capacity.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovUseCaseProgress
from . import personas, pov_wireup

logger = logging.getLogger(__name__)


class UseCaseError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# What a progress row may say. `skipped` is a real answer rather than an absence: "we ran
# it and it did not land" and "we never got to it" are different things to take into a
# renewal conversation, and only one of them is recoverable by running the demo.
#
# The empty string is the third, and it arrived with the customer's notes: somebody typing
# "couldn't get this working" on a card they have not marked is exactly the feedback this
# feature exists to capture, and making them claim they covered it first would be a lie the
# UI put in their mouth. So a row may hold a comment and no verdict. `_summarize` counts
# `done` and `skipped` by name, so such a row is neither, which is the truth.
STATE_NONE = ""
VALID_STATES = ("done", "skipped", STATE_NONE)

# Who ticked it. The SE today; the prospect once the accessor page lands. Recorded on every
# write because a checklist with no author is not evidence of anything.
KIND_SE = "se"
KIND_ACCESSOR = "accessor"
VALID_KINDS = (KIND_SE, KIND_ACCESSOR)

# A note is a sentence about a demo, not a document. Truncated rather than refused: an SE
# mid-POV should never lose a tick because they typed too much.
NOTE_MAX = 2000


def _now() -> datetime:
    return datetime.utcnow()


# ── this POV's product mix ───────────────────────────────────────────────────

def products_for(db: Session, env: PovEnvironment, wireup: dict | None = None) -> dict:
    """The booleans ``personas.pov_catalog`` resolves cards against.

    Six keys in two halves, and the split is the whole point. The three tenant keys answer
    "does this POV INCLUDE the product?" — a question about the evaluation, whose answer an
    operator changes by choosing a tenant. The three artifact keys answer "has the wire-up
    actually run for it?" — a question about this environment's state, whose answer is a
    button. Collapsing them would make "we did not buy Entitle" and "we have not wired
    Entitle yet" the same card, and only one of those is somebody's next click.

    ``wireup`` lets a caller that already computed ``pov_wireup.describe`` pass it in.
    ``api/pov._serialize`` does exactly that per row, and re-deriving it here would put a
    second per-VM query on the list endpoint for a number it already has.
    """
    state = pov_wireup.describe(db, env) if wireup is None else wireup
    return {
        "pra": bool(env.pra_tenant_id),
        "password_safe": bool(env.ps_tenant_id),
        "entitle": bool(env.entitle_tenant_id),
        # Counts, not the `*_ready` booleans beside them: `wireup_ready` means "the wire-up
        # could run", and a card wants to know whether it DID. A POV with a Gateway, a
        # tenant and nothing wired yet is `ready` by that measure and has no jump items.
        "wired": bool(state.get("wired_count")),
        "onboarded": bool(state.get("onboarded_count")),
        "entitle_wired": bool(state.get("entitle_count")),
    }


# ── progress ─────────────────────────────────────────────────────────────────

def _rows(db: Session, env: PovEnvironment) -> dict:
    return {r.card_id: r
            for r in db.query(PovUseCaseProgress)
                       .filter(PovUseCaseProgress.environment_id == env.id).all()}


def describe_row(row: PovUseCaseProgress | None) -> dict:
    """One card's progress. A card nobody has touched has a row of blanks, not ``None``.

    Uniform shape so the page never branches on a null — the empty state is
    ``state: ""``, which reads as "not started" everywhere without a second key saying so.
    """
    if row is None:
        return {"state": "", "note": "", "by": "", "by_kind": "", "at": ""}
    return {
        "state": row.state or "",
        "note": row.note or "",
        "by": row.checked_by or "",
        "by_kind": row.checked_by_kind or "",
        "at": row.checked_at.isoformat() if row.checked_at else "",
    }


def _groups(db: Session, env: PovEnvironment, products: dict) -> list:
    """The catalog for this POV with each card's progress merged in."""
    rows = _rows(db, env)
    groups = personas.pov_catalog(env.id, products)
    for group in groups:
        for card in group["use_cases"]:
            card["progress"] = describe_row(rows.get(card["id"]))
    return groups


def _summarize(groups: list) -> dict:
    """``{done, skipped, total, cards}`` — and ``total`` counts only what this POV CAN run.

    A POV wired into one product has most of the catalog out of scope, and reporting
    "3 of 32" against it would read as a POV that is going badly rather than one that is
    scoped. ``cards`` keeps the honest denominator for anyone who wants it.
    """
    every = [c for g in groups for c in g["use_cases"]]
    in_scope = [c for c in every if c["state"] != "out_of_scope"]
    return {
        "done": sum(1 for c in in_scope if c["progress"]["state"] == "done"),
        "skipped": sum(1 for c in in_scope if c["progress"]["state"] == "skipped"),
        "total": len(in_scope),
        "cards": len(every),
    }


def describe(db: Session, env: PovEnvironment, wireup: dict | None = None) -> dict:
    """The payload the POV page's Use cases tab reads."""
    products = products_for(db, env, wireup)
    groups = _groups(db, env, products)
    return {
        "environment_id": env.id,
        "products": products,
        "groups": groups,
        "summary": _summarize(groups),
    }


def summary_for(db: Session, env: PovEnvironment, wireup: dict | None = None) -> dict:
    """Just the counts, for the row on the POV list.

    Costs one query plus a walk of the registry — which is a fixed size, not a function of
    the estate — so putting it on every row of the list is bounded work.
    """
    return _summarize(_groups(db, env, products_for(db, env, wireup)))


# ── writes ───────────────────────────────────────────────────────────────────

def set_state(db: Session, env: PovEnvironment, card_id: str, *,
              state: str = "done", note: str | None = None, by: str = "",
              by_kind: str = KIND_SE) -> dict:
    """Tick a card off (or mark it skipped). Idempotent, last writer recorded.

    **The registry is the allowlist.** An unknown card id is refused rather than stored:
    a progress table that accepts any string a client sends becomes a free-text store
    nobody can render, and these rows deliberately outlive registry edits — so the mistake
    would outlive it too.

    ``note`` has three values and they are three different intentions:

        None   leave whatever note is there alone   (the default)
        ""     clear it
        text   replace it

    That distinction is load-bearing now that two people write these rows. The SE's tick
    button sends a state and no note; the prospect's note is on the same row. With ``note``
    defaulting to ``""`` and written unconditionally, an SE ticking a card the customer had
    just commented on would silently erase the comment — the one piece of evidence in this
    feature that cannot be reconstructed.
    """
    persona_key, card = personas.find_pov_card(card_id)
    if card is None:
        raise UseCaseError(f"no POV use case with id {card_id!r}")
    if state not in VALID_STATES:
        raise UseCaseError(
            f"state must be one of {', '.join(VALID_STATES)}; got {state!r}")
    if by_kind not in VALID_KINDS:
        raise UseCaseError(
            f"by_kind must be one of {', '.join(VALID_KINDS)}; got {by_kind!r}")

    row = (db.query(PovUseCaseProgress)
             .filter(PovUseCaseProgress.environment_id == env.id,
                     PovUseCaseProgress.card_id == card.id).first())
    if row is None:
        row = PovUseCaseProgress(environment_id=env.id, card_id=card.id)
        db.add(row)

    # Written on every save, not only on insert: a card that moved between personas after
    # it was ticked should report where it lives now, and this is the cheapest place that
    # correction can happen.
    row.persona = persona_key
    row.state = state
    if note is not None:
        row.note = (note.strip()[:NOTE_MAX]) or None
    row.checked_by = (by or "")[:100] or None
    row.checked_by_kind = by_kind
    row.checked_at = _now()
    db.commit()

    logger.info("POV %s: use case %s marked %s by %s (%s)",
                env.id, card.id, state, by or "?", by_kind)
    return describe_row(row)


def clear(db: Session, env: PovEnvironment, card_id: str) -> bool:
    """Un-tick a card. ``True`` if a row was removed.

    Deleting rather than writing a third state: "not started" is the absence of a row
    everywhere else in this module, and a row saying so would be a second way to spell it.
    """
    persona_key, card = personas.find_pov_card(card_id)
    if card is None:
        raise UseCaseError(f"no POV use case with id {card_id!r}")
    deleted = (db.query(PovUseCaseProgress)
                 .filter(PovUseCaseProgress.environment_id == env.id,
                         PovUseCaseProgress.card_id == card.id).delete())
    db.commit()
    return bool(deleted)


def destroy_note(db: Session, env: PovEnvironment) -> str:
    """What the destroy job says about this POV's use-case record. Never raises.

    **Nothing is deleted here, and that is the decision rather than an omission.**
    ``run_env_destroy`` marks the POV row ``destroyed`` instead of removing it, because it
    is the inventory record of something that existed; these rows are that record's
    contents — which demos were run, which were skipped, and what was said about them.
    Reaping the environment is not a reason to delete the account of what it was for, and
    they hold no credential and reach no tenant, so there is nothing here that lingering
    endangers.

    What the destroy DOES owe an operator is the summary, in the job log, at the moment
    they are closing the POV out. That is the one place somebody looks at the end.
    """
    try:
        counts = summary_for(db, env)
    except Exception as exc:  # noqa: BLE001 — a destroy must not stop on a note
        logger.warning("POV %s: could not summarise use-case progress", env.id,
                       exc_info=True)
        return f"NOTE: the use-case record could not be read ({exc}); it is kept regardless."
    if not (counts["done"] or counts["skipped"]):
        return ""
    return (f"use-case record kept: {counts['done']} of {counts['total']} run, "
            f"{counts['skipped']} skipped")
