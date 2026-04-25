"""
Config Management API — Ansible playbook runner (local Docker path).

All endpoints require authentication.  Playbook runs are dispatched as
background jobs; the client gets a job_id immediately and can poll
/api/jobs/{id} for progress and final output.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import get_current_user
from ..models.user import User
from ..services import job_service
from ..services import ansible_storage
from ..services.ansible_storage import AnsibleStorageError
from ..services import ansible_local_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config-mgmt", tags=["config-mgmt"])


@router.get("/playbooks")
async def list_playbooks(current_user: User = Depends(get_current_user)):
    """List available playbooks from the configured storage backend (S3 / Azure Blob / GCS)."""
    try:
        return await ansible_storage.list_playbooks()
    except AnsibleStorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/inventory")
async def get_inventory(current_user: User = Depends(get_current_user)):
    """
    Return the dynamic Ansible inventory.

    Only on-premises hypervisors that are both enabled (feature flag) and have
    a host address configured appear.  The response includes:
      targets   — simplified list for the UI target picker
      inventory — full Ansible JSON inventory (groups + hostvars)
    """
    return {
        "targets":   ansible_local_service.get_configured_targets(),
        "inventory": ansible_local_service.build_inventory(),
    }


# ── Playbook run ──────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    playbook: str
    target: str
    extra_vars: dict = {}


async def _run_job(job_id: str, playbook: str, target: str, extra_vars: dict) -> None:
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 5, f"Fetching playbook '{playbook}'…")
        try:
            pb_b64 = await ansible_storage.fetch_playbook_b64(playbook)
        except AnsibleStorageError as e:
            job_service.set_failed(db, job_id, f"Playbook storage error: {e}")
            return

        job_service.update_progress(db, job_id, 20, f"Running playbook against {target}…")
        output, rc = await ansible_local_service.run_playbook(
            pb_b64, target, extra_vars or None
        )

        if rc == 0:
            job_service.set_completed(db, job_id, {"output": output, "returncode": rc})
        else:
            job_service.set_failed(
                db, job_id, f"ansible-playbook exited {rc}:\n{output}"
            )
    except Exception as e:
        logger.exception("ansible-local job %s failed: %s", job_id, e)
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.post("/run")
async def run_playbook(
    payload: RunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Run a playbook against a target as a background job.

    target must be one of the configured hypervisor group keys returned by
    /api/config-mgmt/inventory, or a bare IP / hostname for ad-hoc runs.
    """
    targets = ansible_local_service.get_configured_targets()
    valid_keys = {t["key"] for t in targets}

    # Bare IP/hostname targets (contain a dot or colon) are allowed ad-hoc.
    is_adhoc = "." in payload.target or ":" in payload.target
    if not is_adhoc and payload.target not in valid_keys:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Target '{payload.target}' is not a configured hypervisor. "
                f"Configured: {sorted(valid_keys) or '(none — enable integrations in Settings)'}."
            ),
        )

    job = job_service.create_job(
        db,
        job_type="ansible_local",
        description=f"Ansible: {payload.playbook} → {payload.target}",
        workgroup="ansible",
        owner_id=current_user.id,
    )
    background_tasks.add_task(
        _run_job, job.id, payload.playbook, payload.target, payload.extra_vars
    )
    return {"job_id": job.id, "status": "queued"}
