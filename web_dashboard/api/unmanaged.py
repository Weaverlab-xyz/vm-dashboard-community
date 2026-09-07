"""The ``/unmanaged`` route the four cloud consoles share.

One implementation rather than four, because what this endpoint decides is *who may see a
VM the dashboard did not deploy* — and a permission rule copied four times is a permission
rule that will differ four ways. The per-cloud parts (how to list, what a deploy job calls
its identifier) are arguments; the flag check, the RBAC and the shape are not.

**A separate route, not a flag on the existing listing.** ``/instances`` and ``/vms`` answer
"what did this dashboard deploy", and a great deal downstream — the destroy button, the
suspend schedule, the expiry sweep, the cost attribution — is built on that answer being
exactly what it says. Merging two populations into one list would make every one of those
consumers responsible for telling them apart, and the first one to forget offers a Destroy
button on somebody's production database server. Here the two sets arrive through different
doors and only this door's rows carry ``managed: false``.

Destroy is not refused here so much as absent: there is no destroy route on this module and
nothing in a discovered row resolves into one. The guard that matters is in
``services/unmanaged_vms.assert_not_unmanaged``, called by the one destroy path that can
act without a deploy job.
"""
import logging
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import Job, User
from ..services import cache_service, feature_flags, unmanaged_vms

logger = logging.getLogger(__name__)

# Short, and deliberately shorter than the managed listings' minute: this list exists to
# be acted on, and a power action taken from a stale row is a power action aimed at
# something that may no longer be there. It is also the only listing here that grows with
# the estate rather than with what the dashboard deployed, so a long TTL would mostly
# serve up a big stale answer.
TTL_SECONDS = 60


def enabled() -> bool:
    """Whether unmanaged discovery is switched on. One reader, like ``_feature_gate``'s."""
    return bool(feature_flags.flags().get("cloud_unmanaged_discovery_enabled"))


def _require_enabled() -> None:
    if not enabled():
        # 404 rather than 403: with the flag off this capability does not exist, and a 403
        # would confirm to an unauthorised caller that there is something here to want.
        # The same choice `_feature_gate` makes, for the same reason.
        raise HTTPException(
            status_code=404,
            detail="Unmanaged VM discovery is not enabled on this instance.")


def managed_identifiers(db: Session, job_type: str, meta_key: str) -> set:
    """Every identifier this dashboard's deploy jobs know about, for one cloud.

    Destroyed rows are included on purpose. A VM whose destroy failed part-way is still
    this dashboard's problem, and listing it as somebody else's would send the operator to
    the wrong repair — and, worse, past the destroy guard that would otherwise have
    stopped them.
    """
    out = set()
    for job in db.query(Job).filter(Job.job_type == job_type).all():
        identifier = job.metadata_dict.get(meta_key)
        if identifier:
            out.add(identifier)
    return out


async def discover(cloud: str, *, job_type: str, fetch_live: Callable, cache_key: str):
    """``(rows, cached_at)`` — this cloud's unmanaged VMs, cached.

    Shared by the listing and by :func:`find`, so a VM can only be powered if discovery
    would have shown it. That is the authorization story in one sentence: the set you can
    act on is the set you can see, which is the invariant ``api/vms.py:233`` names.
    """
    meta_key = unmanaged_vms.ID_KEY[cloud]

    async def _fresh():
        from ..database import SessionLocal
        # Its own session: `get_or_refresh` can run this as a detached task that outlives
        # the request, and a fetcher closing over the request's session then leaks a
        # pooled connection. Same reason every other fetcher here owns one.
        session = SessionLocal()
        try:
            managed = managed_identifiers(session, job_type, meta_key)
        finally:
            session.close()
        return unmanaged_vms.partition(await fetch_live(), managed, cloud)

    return await cache_service.get_or_refresh(cache_key, TTL_SECONDS, _fresh)


async def find(cloud: str, identifier: str, *, job_type: str, fetch_live: Callable,
               cache_key: str) -> Optional[dict]:
    """The discovered row for one identifier, or ``None`` if discovery does not show it.

    What AWS and Azure use to resolve the region / resource group a power call needs and
    the deploy job would otherwise have supplied. Deliberately **not** taken from the
    request: a caller-supplied resource group would turn ``/power/stop`` into "deallocate
    any VM of this name anywhere the credentials reach", which is a considerably larger
    thing than this feature asked for.
    """
    if not enabled():
        return None
    rows, _ = await discover(cloud, job_type=job_type, fetch_live=fetch_live,
                             cache_key=cache_key)
    key = unmanaged_vms.ID_KEY[cloud]
    for row in rows:
        if row.get(key) == identifier:
            return row
    return None


def unmanaged_endpoint(
    cloud: str,
    *,
    job_type: str,
    fetch_live: Callable,
    accessible_workgroups: Callable,
    cache_key: str,
    user_dep: Callable,
):
    """Build the ``GET /unmanaged`` handler for one cloud.

    ``fetch_live`` is an async callable taking no arguments and returning this cloud's live
    rows, each carrying ``tags`` and the identifier named by
    ``unmanaged_vms.ID_KEY[cloud]``. ``accessible_workgroups`` is the cloud module's own —
    imported rather than reimplemented so this list and that cloud's managed list cannot
    disagree about who may see what. ``user_dep`` is that cloud's
    ``require_permission(<cloud>, "read")``, passed in because FastAPI reads the handler's
    signature and a closure variable would never appear in it.
    """
    async def _handler(
        workgroup: Optional[str] = Query(None, description="Narrow to one workgroup"),
        current_user: User = Depends(user_dep),
    ) -> dict:
        _require_enabled()
        accessible = accessible_workgroups(current_user)
        if workgroup is not None:
            canonical = workgroup.lower()
            if accessible is not None and canonical not in accessible:
                raise HTTPException(status_code=403,
                                    detail=f"No access to workgroup '{canonical}'")

        try:
            rows, cached_at = await discover(cloud, job_type=job_type,
                                             fetch_live=fetch_live, cache_key=cache_key)
        except Exception as exc:  # noqa: BLE001 — each cloud raises its own error type
            logger.warning("Unmanaged %s discovery failed: %s", cloud, exc)
            raise HTTPException(status_code=503, detail=str(exc))

        visible = unmanaged_vms.visible_to(rows, accessible)
        if workgroup is not None:
            visible = [r for r in visible if r.get("workgroup") == workgroup.lower()]
        return {"instances": visible, "count": len(visible), "cached_at": cached_at,
                "cloud": cloud}

    return _handler
