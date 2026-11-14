"""The Workspace API: one persona's landing page, for the signed-in user.

Mounted in main.py behind ``_profile_page_gate("workspace_pages")``, so on a POV instance
it 404s naming the profile, as the page does. Any signed-in user may call it: a workspace
is curation (services/workspaces), and every piece of data in it is filtered by the
caller's own permissions by the reader that already owns that data --

* the getting-started ticks read only the CALLER's own jobs and schedules;
* "Your resources" is ``api/inventory.visible_items`` -- the inventory page's own reader,
  workgroup rule included -- and is withheld entirely without ``inventory:read``, the
  scope that page is behind;
* recent jobs are the caller's own, as /jobs shows them to a user without ``jobs:read``.

Which persona, in the dashboard lens's order: ``?persona=`` when it names one (to open
another role's workspace; it persists nothing), then the ``persona`` cookie the lens and
this page's switcher write (an explicit pick), then the user's own assignment, their
group's and the instance default (``personas.resolve_for_user`` -- the server knows the
user here, so it does not need the ``persona_assigned`` cookie the HTML pages read). With
none, the response is the chooser.
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..database import Job, JobSchedule, User, get_db
from ..services import personas, workspaces
from .auth import get_current_user, has_permission

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


def _resolve(user: User, asked: str, picked: str) -> tuple:
    """``(key, source)``. Both inputs are validated against the registry, never reflected."""
    if personas.applies():
        for raw, source in ((asked, "url"), (picked, "cookie")):
            raw = (raw or "").strip().lower()
            if raw in personas.VALID_PERSONAS:
                return raw, source
    return personas.resolve_for_user(user)


def _may_read_inventory(user: User) -> bool:
    return bool(getattr(user, "is_effective_admin", False)) or has_permission(
        user, "inventory", "read")


@router.get("/summary")
async def summary(
    request: Request,
    persona: str = Query("", description="Open this persona's workspace instead of your own."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    key, source = _resolve(current_user, persona, request.cookies.get("persona", ""))
    ws = workspaces.get(key)
    if ws is None:
        return {"persona": "", "source": source, "chooser": workspaces.chooser(),
                "options": personas.options()}

    username = current_user.username
    tick_types = workspaces.step_job_types(ws)
    completed = set()
    if tick_types:
        completed = {t for (t,) in (
            db.query(Job.job_type)
            .filter(Job.created_by == username, Job.status == "completed",
                    Job.job_type.in_(tick_types))
            .distinct().all())}
    scheduled = {t for (t,) in (
        db.query(JobSchedule.job_type)
        .filter(JobSchedule.created_by == username)
        .distinct().all())}

    shown_types = ws.job_types or tick_types
    recent = []
    if shown_types:
        rows = (db.query(Job)
                .filter(Job.created_by == username, Job.job_type.in_(shown_types))
                .order_by(Job.created_at.desc())
                .limit(workspaces.RECENT_JOBS_CAP).all())
        recent = [{"id": r.id, "job_type": r.job_type, "status": r.status,
                   "workgroup": r.workgroup or "",
                   "created_at": r.created_at.isoformat() if r.created_at else "",
                   "href": f"/jobs/{r.id}"} for r in rows]

    resources, resources_state = [], "ok"
    if not ws.resource_kinds and not ws.resource_clouds:
        resources_state = "none"
    elif not _may_read_inventory(current_user):
        resources_state = "forbidden"
    else:
        from .inventory import visible_items
        items, _ = await visible_items(current_user)
        resources = workspaces.select_resources(ws, items)

    out = workspaces.describe(key, completed_types=completed, scheduled_types=scheduled,
                              resources=resources, recent_jobs=recent)
    out["source"] = source
    out["resources_state"] = resources_state
    out["options"] = personas.options()
    return out
