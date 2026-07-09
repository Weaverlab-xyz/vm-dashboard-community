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
    status: str
    progress_pct: int
    progress_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_by: Optional[str]
    error_message: Optional[str]
    duration_seconds: Optional[int]

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
