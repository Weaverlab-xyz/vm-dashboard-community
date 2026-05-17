"""
Bulk-assign workgroup to existing on-prem VMs.

Used by the bulk-action toolbar on each on-prem provider page when an admin
selects a set of VMs the dashboard didn't deploy itself and tags them with a
real workgroup. Cloud providers manage workgroup via cloud-side resource tags
and are explicitly rejected here.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import workgroup_override_service
from ..services.workgroup_override_service import ALLOWED_PROVIDERS, OverrideError
from .auth import require_admin

router = APIRouter(prefix="/api/workgroup-overrides", tags=["workgroup-overrides"])


class BulkAssignRequest(BaseModel):
    provider: str
    vm_ids: List[str]
    workgroup: str


class BulkClearRequest(BaseModel):
    provider: str
    vm_ids: List[str]


@router.post("/bulk")
def bulk_assign(
    payload: BulkAssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if payload.provider not in ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"provider '{payload.provider}' is not allowed. Workgroup overrides only apply "
                f"to on-prem providers: {sorted(ALLOWED_PROVIDERS)}."
            ),
        )
    try:
        count = workgroup_override_service.set_many(
            db,
            provider=payload.provider,
            vm_ids=payload.vm_ids,
            workgroup=payload.workgroup,
            user_username=current_user.username,
        )
    except OverrideError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"updated": count}


@router.delete("/bulk")
def bulk_clear(
    payload: BulkClearRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if payload.provider not in ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"provider '{payload.provider}' is not allowed. Workgroup overrides only apply "
                f"to on-prem providers: {sorted(ALLOWED_PROVIDERS)}."
            ),
        )
    try:
        count = workgroup_override_service.clear_many(
            db, provider=payload.provider, vm_ids=payload.vm_ids
        )
    except OverrideError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"removed": count}
