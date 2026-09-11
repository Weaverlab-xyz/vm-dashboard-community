"""Audit-log API — integrity, reading, and export.

Read-only and **admin-only** throughout. The audit trail is hash-chained (see
``services/audit_chain.py``), and until this file grew past its one endpoint the chain
was the only thing you could do with it: 75 ``log_audit`` call sites wrote to a table
with no list, no filter and no export, and a `verify` that only ran when somebody
remembered to ask. A trail nobody can read is not evidence.

**Why admin-only, stated rather than assumed.** ``api/jobs.py`` answers the same
question with ``can_audit_jobs`` and scopes non-admins to their own rows. That would be
defensible here too, but an audit log's rows are *about* people — a per-user view of who
did what is a different feature with a different blast radius, and shipping the narrow
answer first is the reversible choice. ``jobs`` also deliberately avoids becoming an
existence oracle; keeping this admin-only sidesteps that question entirely rather than
answering it by accident.
"""
import csv
import io
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import AuditLog, User, get_db
from ..services import job_service
from .auth import require_explicit_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/audit", tags=["audit"])

# An export streams; it must not be able to pull the whole table into one response
# either. The table has no retention and cannot have one — pruning any row breaks the
# chain by construction — so "all of it" is a number that only ever grows.
MAX_EXPORT_ROWS = 100_000

_COLUMNS = ("seq", "timestamp", "username", "action", "target_vm", "ip_address",
            "details", "prev_hash", "entry_hash")


def _parse_dt(value: Optional[str], field: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"{field} must be ISO-8601 (e.g. 2026-09-01T00:00:00)")


def _filtered(db: Session, username, action, target, since, until, q):
    """The one query builder every read here goes through, so the page, the export and
    the count cannot disagree about what a filter means."""
    rows = db.query(AuditLog).filter(AuditLog.seq.isnot(None))
    if username:
        rows = rows.filter(AuditLog.username == username)
    if action:
        # Prefix match: actions are namespaced (`agent.create`, `agent.revoke`), so
        # "agent." is the question an operator actually has.
        rows = rows.filter(AuditLog.action.like(f"{action}%"))
    if target:
        rows = rows.filter(AuditLog.target_vm.like(f"%{target}%"))
    if since is not None:
        rows = rows.filter(AuditLog.timestamp >= since)
    if until is not None:
        rows = rows.filter(AuditLog.timestamp <= until)
    if q:
        like = f"%{q}%"
        rows = rows.filter(AuditLog.details.like(like) | AuditLog.action.like(like)
                           | AuditLog.target_vm.like(like))
    return rows


def _row_dict(e: AuditLog) -> dict:
    return {
        "seq": e.seq,
        "id": e.id,
        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        "username": e.username,
        "action": e.action,
        "target_vm": e.target_vm,
        "ip_address": e.ip_address,
        "details": e.details_dict,
        "prev_hash": e.prev_hash,
        "entry_hash": e.entry_hash,
    }


@router.get("/verify")
def verify_audit_log(
    current_user: User = Depends(require_explicit_permission("audit", "read")),
    db: Session = Depends(get_db),
) -> dict:
    """Recompute the audit hash chain. Returns ``{ok, count, first_broken_seq}``:
    ``ok=false`` with the offending ``seq`` means a row was altered, removed, or
    reordered since it was written.

    The same check runs on a timer and raises ``audit.chain_broken`` when it fails —
    this endpoint is the on-demand version, not the only one."""
    return job_service.verify_audit_chain(db)


@router.get("")
def list_audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    username: Optional[str] = Query(None, description="Exact actor match"),
    action: Optional[str] = Query(None, description="Action prefix, e.g. 'agent.'"),
    target: Optional[str] = Query(None, description="Substring of the target"),
    since: Optional[str] = Query(None, description="ISO-8601 lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="ISO-8601 upper bound (inclusive)"),
    q: Optional[str] = Query(None, description="Substring across action/target/details"),
    current_user: User = Depends(require_explicit_permission("audit", "read")),
    db: Session = Depends(get_db),
) -> dict:
    """A page of audit entries, newest first. Admin only."""
    rows = _filtered(db, username, action, target,
                     _parse_dt(since, "since"), _parse_dt(until, "until"), q)
    total = rows.count()
    entries = (rows.order_by(AuditLog.seq.desc())
               .offset((page - 1) * page_size).limit(page_size).all())
    return {"entries": [_row_dict(e) for e in entries], "total": total,
            "page": page, "page_size": page_size}


@router.get("/actions")
def list_actions(
    current_user: User = Depends(require_explicit_permission("audit", "read")),
    db: Session = Depends(get_db),
) -> dict:
    """The distinct action names present, so the filter can be a list rather than a
    guess at the vocabulary."""
    rows = (db.query(AuditLog.action).filter(AuditLog.seq.isnot(None))
            .distinct().order_by(AuditLog.action.asc()).all())
    return {"actions": [r[0] for r in rows if r[0]]}


@router.get("/export")
def export_audit(
    fmt: str = Query("csv", pattern="^(csv|json)$"),
    username: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    target: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    current_user: User = Depends(require_explicit_permission("audit", "read")),
    db: Session = Depends(get_db),
):
    """Stream the matching entries as CSV or JSON, oldest first.

    Oldest-first rather than the page's newest-first: an export is for handing to
    something else, and the chain reads forwards. The hashes go in the file too — that
    is what makes an exported copy checkable by whoever receives it rather than a table
    they have to take on trust.

    Streamed rather than assembled: this is the product's first tabular export, and the
    table it reads has no retention policy.
    """
    rows = (_filtered(db, username, action, target,
                      _parse_dt(since, "since"), _parse_dt(until, "until"), q)
            .order_by(AuditLog.seq.asc())
            .limit(MAX_EXPORT_ROWS)
            .yield_per(1000))
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    if fmt == "json":
        def _json():
            yield "[\n"
            first = True
            for e in rows:
                yield ("" if first else ",\n") + json.dumps(_row_dict(e), default=str)
                first = False
            yield "\n]\n"
        return StreamingResponse(
            _json(), media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.json"'})

    def _csv():
        buf = io.StringIO()
        writer = csv.writer(buf)

        def _flush():
            out = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            return out

        # Yielded before the loop, so an export that matches nothing is still a valid
        # CSV with a header rather than an empty file that reads like a failure.
        writer.writerow(_COLUMNS)
        yield _flush()
        for e in rows:
            d = _row_dict(e)
            writer.writerow([
                d["seq"], d["timestamp"], d["username"], d["action"], d["target_vm"],
                d["ip_address"], json.dumps(d["details"], default=str),
                d["prev_hash"], d["entry_hash"],
            ])
            yield _flush()

    return StreamingResponse(
        _csv(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.csv"'})
