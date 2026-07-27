"""Shared pre-flight for count-based and multi-select cloud VM deploys.

The four cloud deploy endpoints all have to answer the same three questions before
they create a single job row:

  * what are the N names (``expand_names``),
  * is any of them already taken (``reject_name_collisions``),
  * does policy admit every one of them (``enforce_admission``).

Those answers were going to be copy-pasted four times, and the AWS and Azure bulk
runners are a standing demonstration of what copy-pasted deploy logic does over time
— one of them quietly stopped honouring the PRA overrides the single path applies.

Raises ``HTTPException`` directly, like ``admission_service`` and
``ansible_local_run_service`` already do: this is an enforcement seam, and the useful
thing to share is the exact status code and detail shape each failure produces.
``services/vm_naming`` stays free of FastAPI so it remains trivially unit-testable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Sequence

from fastapi import HTTPException

from . import vm_naming

logger = logging.getLogger(__name__)


def expand_names(base: str, count: int, provider: str) -> List[str]:
    """Expand a base name into ``count`` numbered names, or 400 with the reason.

    ``vm_naming.VMNameError`` messages are written for an operator (they name the
    provider's limit and the room left), so they are surfaced verbatim."""
    try:
        return vm_naming.expand(base, count, provider)
    except vm_naming.VMNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def reject_name_collisions(db, deploy_job_type: str, names: Sequence[str]) -> None:
    """409 if any name is already claimed, or repeated within this request.

    Why this is not optional: there is no uniqueness constraint on VM name anywhere,
    and Azure, GCP and OCI destroy all resolve their deploy job by FIRST MATCH on
    name. Two live VMs called web-01 make a later destroy ambiguous — it will pick
    one arbitrarily. Auto-numbering makes that easy to hit by accident, because
    deploying base "web" with a count of 3 twice produces the same three names.

    409 rather than 400: the request is well formed, the name is simply taken, and the
    operator's fix is different (pick another base, or destroy the existing VMs).
    """
    from . import inventory_service

    dupes = vm_naming.duplicates(names)
    clashes = vm_naming.collisions(
        names, inventory_service.live_or_pending_vm_names(db, deploy_job_type))
    if not dupes and not clashes:
        return

    parts = []
    if clashes:
        parts.append(
            f"{len(clashes)} of the {len(names)} name(s) this deploy would create are "
            f"already in use by VMs this dashboard deployed or is deploying: "
            f"{', '.join(clashes)}. Pick a different base name, or destroy those VMs first.")
    if dupes:
        parts.append(f"Repeated within this request: {', '.join(dupes)}.")
    raise HTTPException(status_code=409, detail={
        "code": "vm_name_collision",
        "message": " ".join(parts),
        "conflicts": clashes,
        "duplicates": dupes,
        "names": list(names),
    })


async def enforce_admission(action: str, *, requests: Sequence[dict],
                            actor=None, db=None) -> None:
    """Run the admission gate over every item in a batch, before any job is created.

    Per item, not per batch. On the AWS multi-select path each item carries a
    different image, and on the count path each carries a different name — both are
    fields policies can read, so a single call would have to evaluate one item's
    values and admit the rest on them.

    Uses the SAME action id as the single-deploy route (``aws:ec2:deploy`` and
    friends) rather than a ``:bulk`` variant. An operator who has already gated
    ``aws:ec2:deploy`` reasonably believes EC2 deploys are gated; a separate id would
    leave batches uncovered under a new name, which is the hole this closes.

    All-or-nothing, and it must run before the first ``create_job``. ``enforce``
    raises 403, and a denial partway through job creation would strand already-created
    ``queued`` children with no parent to drive them — those are unclaimable by the
    runner (it filters ``status='pending'``) *and* skipped by ``reconcile_stale_jobs``
    (it only looks at ``running``), so they would sit there forever.

    Note this deliberately does NOT match the per-row fail-soft semantics of the
    cloud-identity ``elevate`` wrapper in the bulk runners, where one denial fails one
    VM and the batch continues. That is a runtime gate around a cloud call with jobs
    already on the board; this is an API-boundary gate with nothing created yet.
    """
    from . import admission_service

    # enforce() shells out to the OPA binary synchronously, so N items would block the
    # event loop for N subprocesses. It is a complete no-op — no subprocess at all —
    # unless admission is enabled and this action is gated, so the default
    # configuration pays nothing either way. HTTPException propagates out of
    # to_thread unchanged.
    def _run() -> None:
        for request in requests:
            admission_service.enforce(action, request=request, actor=actor, db=db)

    await asyncio.to_thread(_run)


def batch_request_docs(request: dict, names: Sequence[str], *,
                       name_key: str = "name") -> List[dict]:
    """One ``input.request`` document per VM: the shared fields plus this VM's name.

    ``count`` and ``batch`` ride along so a policy can say "no batches over five" or
    treat batches differently. Rego ignores input keys it doesn't read, so adding them
    changes nothing for the shipped rules."""
    return [dict(request, **{name_key: name, "count": len(names),
                             "batch": len(names) > 1})
            for name in names]
