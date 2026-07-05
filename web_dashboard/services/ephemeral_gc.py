"""Garbage collector for ephemeral cloud secrets (managed-account checkout on the
ECS / Cloud Run runners).

Each run force-deletes its own ephemeral secrets in a ``finally`` — this sweeper is
the safety net for the crash-between-create-and-delete case. It lists the tagged/
labelled ephemerals in AWS SM + GCP SM and force-deletes any older than the
configured TTL. Called on startup and opportunistically before a run creates new
ones. Best-effort throughout: a provider that isn't configured is skipped, and a
delete failure is logged, not raised.
"""
import logging
import time

logger = logging.getLogger(__name__)


def _ttl_min() -> int:
    from . import config_service as cs
    from ..config import settings
    try:
        return int(cs.get("ansible_ephemeral_secret_ttl_min")
                   or getattr(settings, "ansible_ephemeral_secret_ttl_min", 30) or 30)
    except (TypeError, ValueError):
        return 30


def sweep() -> dict:
    """Reap expired ephemeral ansible secrets in AWS SM + GCP SM. Returns a
    per-provider count of what was deleted."""
    from . import ephemeral_secrets, secrets_backend_service as sbs
    ttl = _ttl_min()
    now = time.time()
    result = {"aws": 0, "gcp": 0}
    providers = (
        ("aws", sbs.list_aws_sm_ephemeral, sbs.delete_aws_sm),
        ("gcp", sbs.list_gcp_sm_ephemeral, sbs.delete_gcp_sm),
    )
    for name, lister, deleter in providers:
        try:
            items = lister()
        except Exception as exc:  # provider not configured / no access → skip
            logger.debug("ephemeral GC: %s list skipped: %s", name, exc)
            continue
        for sid in ephemeral_secrets.expired(items, ttl, now):
            try:
                deleter(sid)
                result[name] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("ephemeral GC: failed to delete %s %s: %s", name, sid, exc)
    if result["aws"] or result["gcp"]:
        logger.info("ephemeral GC reaped %s", result)
    return result
