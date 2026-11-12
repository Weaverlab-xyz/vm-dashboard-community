"""In-product feedback API -- submit a report, and see what became of it.

Routes
------
* ``GET  /api/feedback/context``  what the form needs to render (mode, repo, visibility)
* ``POST /api/feedback/submit``   file or compose one submission
* ``GET  /api/feedback/mine``     the caller's own submissions
* ``GET  /api/feedback/all``      every submission (administrators)

The POV guest's equivalent is **not here**. It lives on ``self_router`` in
``api/pov_accessor`` as ``POST /api/pov/accessor/self/feedback``, inside the prefix the
accessor allowlist already permits -- see that module, and the note on
``_ACCESSOR_ALLOWED_PREFIXES`` in ``api/auth``, for why widening the allowlist was the
wrong move.

Two things about the shape of this module:

**Every route is a plain ``def``.** ``tests/test_sync_db_routes_off_the_event_loop``
keeps ``NOT_YET_CONVERTED`` empty and calls that the finish line: an ``async def`` route
running synchronous SQLAlchemy puts the query on the event loop, where one slow call
freezes the whole worker. Being sync also puts the blocking ``httpx`` call in the
threadpool, which is where it belongs.

**The router mounts ungated.** No ``_feature_gate``: an administrator has to read
``/context`` to configure the feature before switching it on, and a gated router
answering 404 reads to the UI as "no data". Each route refuses on its own terms when
the feature is off.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import FeedbackSubmission, User, get_db
from ..services import (build_provenance, feature_flags, feedback_guard,
                        feedback_service)
from ..utils.clock import utcnow
from .auth import get_current_user, require_admin, require_person

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])

MAX_LIST = 200


class FeedbackCreate(BaseModel):
    """What the browser sends. Note what is NOT here: the version, the commit and the
    profile are read server-side, because a client that could set its own version could
    pin a bug on a build that never had it."""
    category: str = Field(default="other", max_length=32)
    title: str = Field(default="", max_length=feedback_service.MAX_TITLE)
    body: str = Field(default="", max_length=feedback_service.MAX_BODY)
    page_path: str = Field(default="", max_length=feedback_service.MAX_PAGE_PATH)


def _enabled() -> bool:
    return bool(feature_flags.enabled("feedback_enabled"))


def _require_on() -> str:
    """The current mode, refusing with an explanation when there is nothing to do.

    A 400 with a sentence, not a 404: "off" and "misconfigured" are operator problems,
    and a bare 404 on a button the user can see reads as a broken dashboard.
    """
    if not _enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Feedback is not enabled on this instance.")
    mode = feedback_service.mode()
    if mode == "off":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Feedback has no destination configured. An administrator sets the "
                   "repository under Settings.")
    return mode


def _clean_page_path(raw: str) -> str:
    """The one client-supplied context field.

    It labels a report and governs nothing, so trusting the browser with it is fine --
    but it is length-capped and required to look like a path, so it cannot be used to
    smuggle a sentence into the issue body's context table.
    """
    p = (raw or "").strip()
    if not p.startswith("/"):
        return ""
    return p[:feedback_service.MAX_PAGE_PATH]


def _row_out(row: FeedbackSubmission, *, full_body: bool = False) -> dict:
    out = {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "category": row.category,
        "title": row.title,
        "page_path": row.page_path,
        "app_version": row.app_version,
        "status": row.status,
        "mode": row.mode,
        "github_issue_number": row.github_issue_number,
        "github_issue_url": row.github_issue_url,
        "issue_state": row.issue_state,
        "error_message": row.error_message,
    }
    if full_body:
        out["body"] = row.body
        out["submitted_by"] = row.submitted_by
        out["submitter_kind"] = row.submitter_kind
        out["pov_env_id"] = row.pov_env_id
    return out


def record_and_deliver(db: Session, *, submitted_by: str, submitter_kind: str,
                       payload: FeedbackCreate, pov_env_id: str = "") -> dict:
    """Validate, throttle, record, then file or compose. Shared with the accessor route.

    The order is the whole design:

    1. **Throttle first**, so a flood neither grows the table nor reaches GitHub.
    2. **Refuse a credential** before anything is stored -- the text is still on the
       submitter's screen, so they can fix it.
    3. **Insert the row** with ``status="pending"`` and commit. This is what makes the
       throttle's count honest and makes a filed-but-unrecorded issue impossible.
    4. **Then** talk to GitHub, and update the row with the outcome either way.

    A failure at step 4 leaves the row as ``error`` with a message rather than losing
    the submission: the words are kept, and the response says plainly that it was saved
    here but not filed.
    """
    mode = _require_on()

    title = (payload.title or "").strip()
    body = (payload.body or "").strip()
    if not title or not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A title and a description are both required.")
    category = (payload.category or "other").strip().lower()
    if category not in feedback_service.CATEGORIES:
        category = "other"

    try:
        feedback_guard.check(db, username=submitted_by)
    except feedback_guard.FeedbackThrottled as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=("That is a lot of feedback in one go - thank you. Please try again "
                    "a little later."),
            headers={"Retry-After": str(exc.retry_after)}) from exc

    found = feedback_service.find_credentials(title, body)
    if found:
        rules = sorted({f["rule"] for f in found})
        lines = sorted({str(f["line"]) for f in found})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("This looks like it contains a credential (" + ", ".join(rules)
                    + " on line " + ", ".join(lines) + "). Please remove it and send "
                    "again - nothing has been filed or stored."))

    prov = {}
    try:
        prov = build_provenance.app_version() or {}
    except Exception:  # noqa: BLE001 - documented as never raising; belt and braces
        prov = {}
    try:
        profile = feature_flags.install_profile()
    except Exception:  # noqa: BLE001
        profile = ""

    row = FeedbackSubmission(
        submitted_by=submitted_by,
        submitter_kind=submitter_kind,
        pov_env_id=pov_env_id or None,
        category=category,
        title=title,
        body=body,
        page_path=_clean_page_path(payload.page_path),
        app_version=str(prov.get("version") or ""),
        commit=str(prov.get("commit") or ""),
        instance_profile=profile,
        mode=mode,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    issue_title = feedback_service.compose_title(row)
    issue_body = feedback_service.compose_body(row)

    if mode == "compose" and submitter_kind == "accessor":
        # A prospect is not a contributor to this repository and may well have no GitHub
        # account at all, so handing them a prefilled issue form to file themselves is a
        # dead end dressed up as success. Their words are kept here, where the SE running
        # the evaluation reads them on the admin view -- which is what the guest form
        # means when it says this reached the team.
        row.status = "composed"
        db.commit()
        logger.info("feedback %s recorded from accessor %s (pov %s), compose mode",
                    row.id, submitted_by, pov_env_id or "-")
        return {"mode": "compose", "id": row.id, "recorded_only": True,
                "status": row.status}

    if mode == "compose":
        try:
            url, truncated = feedback_service.compose_url(
                feedback_service.repo(), issue_title, issue_body)
        except feedback_service.FeedbackError as exc:
            row.status = "error"
            row.error_message = str(exc)
            db.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=str(exc)) from exc
        row.status = "composed"
        db.commit()
        return {"mode": "compose", "id": row.id, "compose_url": url,
                "truncated": truncated, "status": row.status}

    try:
        issue = feedback_service.file_issue(issue_title, issue_body)
    except feedback_service.FeedbackError as exc:
        row.status = "error"
        row.error_message = str(exc)
        db.commit()
        # 502, not 400: the submitter did nothing wrong and their words were kept.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=str(exc)) from exc

    row.github_issue_number = issue["number"]
    row.github_issue_url = issue["url"]
    row.issue_state = "open"
    row.checked_at = utcnow()
    row.status = "filed"
    db.commit()
    logger.info("feedback %s filed as issue #%s by %s",
                row.id, issue["number"], submitted_by)
    return {"mode": "direct", "id": row.id, "issue_number": issue["number"],
            "issue_url": issue["url"], "status": row.status}


@router.get("/context")
def feedback_context(db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)) -> dict:
    """What the form needs before the user types a word.

    ``repo_private`` is read live from GitHub and may be ``None`` -- rendered as
    "unknown", never guessed, because telling somebody a public tracker is private is
    the one answer that would actively mislead them about who can read their words.
    """
    if not _enabled():
        return {"enabled": False, "mode": "off"}
    mode = feedback_service.mode()
    out = {
        "enabled": True,
        "mode": mode,
        "repo": feedback_service.repo(),
        "categories": list(feedback_service.CATEGORIES),
        "compose_body_limit": feedback_service.COMPOSE_BODY_LIMIT,
        "max_title": feedback_service.MAX_TITLE,
        "max_body": feedback_service.MAX_BODY,
    }
    if mode != "off":
        out["repo_private"] = feedback_service.repo_is_private()
        out["remaining"] = feedback_guard.remaining(
            db, username=current_user.username)
    return out


@router.post("/submit")
def submit_feedback(payload: FeedbackCreate, db: Session = Depends(get_db),
                    current_user: User = Depends(require_person)) -> dict:
    """File (or compose) one piece of feedback.

    ``require_person`` rather than ``get_current_user``: a workload or OAuth
    service-account token filing GitHub issues is abuse surface with no legitimate use.
    """
    return record_and_deliver(db, submitted_by=current_user.username,
                              submitter_kind="user", payload=payload)


@router.get("/mine")
def my_feedback(limit: int = Query(default=50, ge=1, le=MAX_LIST),
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)) -> dict:
    """The caller's own submissions, newest first.

    Filtered by row rather than gated by scope: everyone may send feedback, so everyone
    may see their own. The all-rows view is a separate, admin-only route.
    """
    rows = (db.query(FeedbackSubmission)
            .filter(FeedbackSubmission.submitted_by == current_user.username)
            .order_by(FeedbackSubmission.created_at.desc())
            .limit(limit).all())
    return {"data": [_row_out(r, full_body=True) for r in rows]}


@router.get("/all")
def all_feedback(limit: int = Query(default=100, ge=1, le=MAX_LIST),
                 status_filter: Optional[str] = Query(default=None, alias="status"),
                 db: Session = Depends(get_db),
                 current_user: User = Depends(require_admin)) -> dict:
    """Every submission on the instance, newest first. Administrators only.

    Admin-only for the same reason the notification delivery log is: these rows hold the
    full text somebody typed, including a POV guest's, and that is not everybody's to
    read.
    """
    q = db.query(FeedbackSubmission)
    if status_filter:
        q = q.filter(FeedbackSubmission.status == status_filter)
    rows = (q.order_by(FeedbackSubmission.created_at.desc()).limit(limit).all())
    return {"data": [_row_out(r, full_body=True) for r in rows]}
