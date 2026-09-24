"""Job-related Pydantic schemas"""
from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel


class JobResponse(BaseModel):
    id: str
    job_type: str
    workgroup: Optional[str]
    vm_path: Optional[str]
    description: Optional[str] = None  # human label stored in metadata (e.g. Ansible runs)
    batch_id: Optional[str] = None     # groups the jobs of one bulk Config-Management run
    status: str
    progress_pct: int
    progress_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_by: Optional[str]
    error_message: Optional[str]
    duration_seconds: Optional[int]
    # ── Change window / scheduler ─────────────────────────────────────────────
    # `schedule_state` is derived SERVER-SIDE (job_service.schedule_state) rather than
    # from the fields below, because the jobs list and the job detail page would
    # otherwise each reimplement "is this scheduled, or merely pending?" and the two
    # would disagree the first time either was edited. One string, one definition.
    #
    # Empty on every job that carries no schedule, which is almost all of them.
    schedule_state: str = ""          # "" | scheduled | awaiting_approval | missed | overran
    scheduled_for: Optional[datetime] = None
    window_ends_at: Optional[datetime] = None
    change_window_name: Optional[str] = None   # resolved for display; None = ad-hoc time
    approval_required: bool = False
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None

    class Config:
        from_attributes = True


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    page: int
    page_size: int


class JobProgressUpdate(BaseModel):
    job_id: str
    status: str
    progress_pct: int
    progress_message: Optional[str]
    log_line: Optional[str]
    timestamp: str
