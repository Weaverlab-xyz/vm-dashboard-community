"""Signing off a POV: somebody confirming that the checklist is what was evaluated.

The Summary tab could already say what a POV covered, and copy it as Markdown. What it
could not say is that anybody AGREED with it -- which is the thing a technical win is, and
what an SE otherwise collects as an email that lives in their inbox and nowhere else.

A sign-off is a :class:`~..database.PovSignoff` row: who, which kind of person (the SE,
or the customer through their accessor login), when, and a snapshot of the coverage they
were looking at. Three rules:

  * **It records a snapshot, not a pointer.** Ticks made after somebody signed are
    reported as such (``current`` false) rather than retro-fitted under their name.
  * **It is never deleted.** Withdrawing one sets ``revoked_at``; the report still shows
    it. Evidence that can vanish is not evidence.
  * **It holds no free text.** No comment, no customer name -- the reason
    ``PovEnvironment`` has no customer field applies to everything hanging off it. What the
    customer thought is already in the per-card notes, which they wrote themselves.

Read-only apart from :func:`record` and :func:`revoke`, and neither reaches a tenant or a
lab platform: a sign-off has to work on a destroyed POV, which is when it is most often
asked for.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovSignoff
from ..utils.clock import utcnow
from . import pov_use_cases


class SignoffError(ValueError):
    """A refusal written for the person who pressed the button."""


class NotFound(SignoffError):
    pass


def snapshot(summary: dict) -> dict:
    """The part of ``pov_summary.build`` a sign-off attests to."""
    return {
        "coverage": {k: summary["coverage"].get(k, 0) for k in ("done", "skipped", "total")},
        "by_group": [{k: g.get(k) for k in ("group", "label", "in_scope", "done", "skipped")}
                     for g in summary.get("by_group") or []],
        "covered": sorted(e["id"] for e in summary.get("covered") or []),
        "skipped": sorted(e["id"] for e in summary.get("skipped") or []),
    }


def fingerprint(snap: dict) -> str:
    """Changes when what was covered or skipped changes -- not when a note is edited."""
    basis = {"covered": snap.get("covered") or [], "skipped": snap.get("skipped") or []}
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def _iso(value) -> str:
    return value.isoformat() + "Z" if value else ""


def _out(row: PovSignoff, current_hash: str) -> dict:
    try:
        snap = json.loads(row.coverage_json or "{}")
    except ValueError:
        snap = {}
    return {
        "id": row.id,
        "signed_by": row.signed_by,
        "signed_by_kind": row.signed_by_kind,
        "signed_at": _iso(row.signed_at),
        "revoked": row.revoked_at is not None,
        "revoked_at": _iso(row.revoked_at),
        "revoked_by": row.revoked_by or "",
        # False once the checklist has moved on since this was signed.
        "current": row.summary_hash == current_hash,
        "coverage": snap.get("coverage") or {},
    }


def describe_all(db: Session, env: PovEnvironment, current_hash: str) -> list:
    """Every sign-off on this POV, oldest first, withdrawn ones included."""
    rows = (db.query(PovSignoff).filter(PovSignoff.environment_id == env.id)
              .order_by(PovSignoff.signed_at).all())
    return [_out(r, current_hash) for r in rows]


def record(db: Session, env: PovEnvironment, *, by: str, by_kind: str) -> dict:
    """Sign off what this POV covers right now. Idempotent per person and version.

    Signing the same coverage twice returns the first sign-off rather than writing a
    second: a double click is one sign-off, and a list of identical rows would read as
    several people agreeing.
    """
    from . import pov_summary

    if by_kind not in (pov_use_cases.KIND_SE, pov_use_cases.KIND_ACCESSOR):
        raise SignoffError(f"unknown signer kind {by_kind!r}")
    if not by:
        raise SignoffError("a sign-off needs a signed-in user")
    summary = pov_summary.build(db, env)
    snap = snapshot(summary)
    if not snap["covered"]:
        raise SignoffError("nothing has been marked as covered yet, so there is nothing "
                           "to sign off")
    digest = fingerprint(snap)
    existing = (db.query(PovSignoff)
                  .filter(PovSignoff.environment_id == env.id,
                          PovSignoff.signed_by == by,
                          PovSignoff.summary_hash == digest,
                          PovSignoff.revoked_at.is_(None)).first())
    if existing is not None:
        return _out(existing, digest)
    row = PovSignoff(environment_id=env.id, signed_by=by[:100], signed_by_kind=by_kind,
                     coverage_json=json.dumps(snap, sort_keys=True), summary_hash=digest)
    db.add(row)
    db.commit()
    return _out(row, digest)


def revoke(db: Session, env: PovEnvironment, signoff_id: str, *, by: str) -> dict:
    """Withdraw a sign-off. The row stays, marked, so the report can still show it."""
    from . import pov_summary

    row = (db.query(PovSignoff)
             .filter(PovSignoff.id == signoff_id,
                     PovSignoff.environment_id == env.id).first())
    if row is None:
        raise NotFound("no such sign-off on this POV")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        row.revoked_by = (by or "")[:100] or None
        db.commit()
    return _out(row, fingerprint(snapshot(pov_summary.build(db, env))))


def for_signer(db: Session, env: PovEnvironment, username: str) -> dict:
    """This person's own standing sign-off on the current coverage, or {}.

    What the accessor page reads to say "you confirmed this on …" instead of offering
    the button again.
    """
    from . import pov_summary

    digest = fingerprint(snapshot(pov_summary.build(db, env)))
    row = (db.query(PovSignoff)
             .filter(PovSignoff.environment_id == env.id,
                     PovSignoff.signed_by == username,
                     PovSignoff.revoked_at.is_(None))
             .order_by(PovSignoff.signed_at.desc()).first())
    return _out(row, digest) if row else {}
