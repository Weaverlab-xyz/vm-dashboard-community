"""Shared BeyondTrust PRA pickers endpoint.

The per-DB tunnel form and the k8s/rancher tunnel modals each already surface the
PRA Jump Group / Jumpoint / Vault-account-group pickers, but those endpoints are
either DB-specific or behind a feature gate. The VM deploy forms (AWS/Azure/GCP/
OCI) are always available, so they need an always-on source for the same pickers —
this router provides it, so every deploy/provision form fills its Jump group and
Jumpoint dropdowns from one place instead of hardcoding text inputs.

  GET /api/pra/pickers  — Jump Groups, Jumpoints and Vault account groups
"""
from fastapi import APIRouter, Depends

from ..database import User
from ..services import pra_api_service
from .auth import get_current_user

router = APIRouter(prefix="/api/pra", tags=["pra"])


@router.get("/pickers")
async def pra_pickers(current_user: User = Depends(get_current_user)):
    """PRA pickers for the deploy/provision form dropdowns — Jump Groups,
    Jumpoints and Vault account groups (best-effort, cloud-agnostic; PRA objects
    aren't region/cloud-scoped). ``configured`` is false when PRA OAuth isn't set,
    so the UI can show a note instead of empty dropdowns and fall back to the
    configured defaults at broker time."""
    pickers = await pra_api_service.list_pickers()
    return {"configured": pra_api_service.configured(), **pickers}
