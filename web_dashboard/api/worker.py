"""Background job-worker status — read-only.

The worker's concurrency caps are editable in Settings (``WorkerFeatureConfig``), but the
numbers it *actually* runs with can differ from the ones saved: they are clamped to the
ceilings in ``services/worker_policy`` and then again to what this process's DB connection
pool can serve. A cap that is silently clamped is worse than one refused — "I raised the
light cap to 10 and nothing changed" is otherwise only diagnosable from a container log
most operators never open.

So the worker publishes what it resolved, and this endpoint reads it back. One route, no
mutations: the caps are written through ``PATCH /api/setup/feature/worker`` like every
other panel, and the runtime status is deliberately not a field on that model — the same
rule that keeps ``resource_expiry_last_sweep`` off the auto-delete panel, since saving the
panel would otherwise overwrite the worker's own readout.
"""
import logging

from fastapi import APIRouter, Depends

from ..database import User
from ..services import worker_policy
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/worker", tags=["worker"])


@router.get("/status")
async def worker_status(current_user: User = Depends(require_admin)) -> dict:
    """What the job worker resolved its limits to, as last published by the worker itself.

    Returns ``{"never_run": true}`` when no worker has published yet — a fresh install, or
    one still running an image that predates this — and ``{"corrupted": true, ...}`` if the
    stored blob will not parse, because an operator reading "corrupted" learns more than
    one staring at an empty card.

    Admin-only: the caps and the pool capacity describe the deployment's shape, and the
    hostname identifies a container.
    """
    status = worker_policy.get_runtime_status()
    # The ceilings are not in the published blob — they are code, not config — but the
    # panel needs them to render "max" hints next to each field, and they are the answer
    # to "why did my 999 become 4".
    status["ceilings"] = {
        "heavy": worker_policy.HEAVY_CAP_CEILING,
        "medium": worker_policy.MEDIUM_CAP_CEILING,
        "light": worker_policy.LIGHT_CAP_CEILING,
        "total": worker_policy.TOTAL_CAP_CEILING,
        "executor_threads": worker_policy.EXECUTOR_THREADS_CEILING,
        "drain_timeout_s": worker_policy.DRAIN_TIMEOUT_CEILING_S,
    }
    return status
