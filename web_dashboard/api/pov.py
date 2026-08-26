"""POV environments — read-only.

  GET /api/pov/platforms              — the registry, with capabilities and configured state
  GET /api/pov/templates              — templates a POV could be created from
  GET /api/pov/environments           — environments visible on the platform
  GET /api/pov/environments/{env_id}  — one, with its VMs, private IPs and published services

Nothing here creates, changes or deletes anything on a lab platform. That is deliberate for
this slice: it proves auth, the 423/Retry-After retry and the ``keep_idle`` guarantee
against a real account before any code path can leave a resource behind.

The whole router is gated on ``pov_environments_enabled``, which
``feature_flags._POV_ONLY`` masks off on a demo instance — so on the demo dashboard these
routes 404 with a message naming the profile, not a stack trace.

Every endpoint takes ``?platform=`` and resolves it through ``lab_platforms``. Skytap is the
only adapter today; the parameter exists so the second one is a registry entry rather than a
second router.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import User
from ..services import lab_platforms
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pov", tags=["pov"])

_DEFAULT_PLATFORM = "skytap"


def _adapter(platform: str):
    """Resolve a platform to its adapter, or fail with a message that names the problem.

    Two different failures, kept apart on purpose: an unknown platform is a bad request
    (the caller asked for something that does not exist), while an unconfigured one is a
    409 telling the operator where to fix it. Collapsing them into one 500 is how "Skytap
    isn't set up" ends up looking like a dashboard bug.
    """
    try:
        name = lab_platforms.normalize(platform)
    except lab_platforms.LabPlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mod = lab_platforms.adapter(name)
    if not mod.configured():
        raise HTTPException(
            status_code=409,
            detail=f"{lab_platforms.capabilities(name)['label']} is not configured — "
                   f"add its credentials in Settings → Integrations.",
        )
    return name, mod


def _platform_error(exc: Exception, what: str) -> HTTPException:
    """Turn an adapter error into a 502 that carries the platform's own words.

    The platform is upstream, so 502 rather than 500: this is not the dashboard failing,
    and the distinction is what stops an SE debugging the wrong system. The upstream text
    is included because a rate-limit and a bad token are both "it didn't work" without it.
    """
    logger.warning("POV: %s failed", what, exc_info=True)
    return HTTPException(status_code=502, detail=f"{what} failed: {exc}")


@router.get("/platforms")
async def list_platforms(current_user: User = Depends(get_current_user)):
    """Every lab platform this build knows about, with what it can do.

    ``capabilities`` is served to the UI rather than kept server-side so a platform that
    cannot, say, produce a share link renders "PRA only" instead of a button that 502s.
    """
    configured = set(lab_platforms.configured_platforms())
    return {
        "platforms": [
            {"name": name,
             "configured": name in configured,
             **lab_platforms.capabilities(name)}
            for name in lab_platforms.VALID_PLATFORMS
        ],
        "default": _DEFAULT_PLATFORM,
    }


@router.get("/templates")
async def list_templates(platform: str = Query(_DEFAULT_PLATFORM),
                         current_user: User = Depends(get_current_user)):
    """Templates a POV environment could be created from."""
    name, mod = _adapter(platform)
    try:
        return {"platform": name, "templates": await mod.list_templates()}
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"listing {name} templates") from exc


@router.get("/environments")
async def list_environments(platform: str = Query(_DEFAULT_PLATFORM),
                            current_user: User = Depends(get_current_user)):
    """Environments visible on the platform.

    Includes ones the dashboard did not create. That is the point of a read-only view: an
    SE's existing hand-built POVs are exactly what they want to see next to the managed
    ones.
    """
    name, mod = _adapter(platform)
    try:
        return {"platform": name, "environments": await mod.list_environments()}
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"listing {name} environments") from exc


@router.get("/environments/{env_id}")
async def get_environment(env_id: str,
                          platform: str = Query(_DEFAULT_PLATFORM),
                          current_user: User = Depends(get_current_user)):
    """One environment, with its VMs, private IPs and published services."""
    name, mod = _adapter(platform)
    try:
        return {"platform": name, "environment": await mod.get_environment(env_id)}
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"reading {name} environment {env_id}") from exc
