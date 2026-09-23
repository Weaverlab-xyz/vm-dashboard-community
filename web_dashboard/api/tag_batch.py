"""Applying an operator's tag edit to one or many VMs — the part all four clouds share.

One implementation rather than four, for the same reason ``api/unmanaged.py`` gives: what
this decides is *which keys a user may change*, and a rule copied four times is a rule that
will differ four ways. The per-cloud parts (how to address a VM, how to write a tag) are
arguments; the guard, the audit and the response shape are not.

**Why this is not ``queue_power_batch``.** That fans out to background *jobs* because a
power op takes minutes and can fail halfway. A tag write is a single API call that
finishes in under a second, so a Job row per VM would be pure overhead and would send the
operator to ``/jobs`` to read an outcome this can simply return. The reassign endpoints
(``api/aws.py::reassign_instance_workgroup`` and its three siblings) already write a tag
synchronously and are the precedent followed here.

**One target or fifty is the same path.** The per-VM editor posts a list of one. That is
deliberate: a separate single-VM route is a second place for the guard to be forgotten,
and it is the guard that stops somebody deleting the ``managed-by`` that ``/costs`` and
teardown select on.

**Partial success is normal and is reported per VM.** Fifty instances is fifty independent
API calls; a stopped VM, a deleted VM or a throttled region fails on its own. The envelope
mirrors ``queue_power_batch``'s deliberately — ``updated`` / ``failed`` as *named* lists,
never counts — so the pages can render outcomes the way they already do.
"""
import logging
from typing import Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..services import job_service, tag_policy
from .power_batch import BULK_MAX_TARGETS

logger = logging.getLogger(__name__)

# The same cap as a bulk power op, and the same number on purpose: an operator who can
# select fifty VMs for one action should not find a different limit on the next. See
# power_batch.BULK_MAX_TARGETS for why fifty.
TAG_MAX_TARGETS = BULK_MAX_TARGETS

# The audit action. Dotted, because api/audit.py's action filter is a PREFIX match, so
# `tags.` groups every tag mutation — including whatever Phase 2b adds for hypervisors.
AUDIT_ACTION = "tags.update"


def _dedupe(targets: list) -> list:
    """Drop repeated targets, order preserved.

    The same VM named twice is one edit. Without this the second pass would read the
    state the first one just wrote, and its audit row would record a change that did not
    happen — two entries for one edit, the second one a no-op that looks like a second
    operator action.
    """
    seen, out = set(), []
    for t in targets:
        try:
            key = t.model_dump_json()
        except Exception:  # noqa: BLE001 — a plain dict or an object without pydantic
            key = repr(t)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


async def apply_tag_edit(
    db: Session, *, cloud: str, targets: list, add: dict, remove: list,
    apply_one: Callable, label_of: Callable, created_by: str = "",
) -> dict:
    """Apply one add/remove edit across ``targets``. Returns the per-VM outcome.

    ``apply_one(target)`` is the cloud's own writer, awaited once per VM, returning
    ``(before, after)`` as every ``*_service.update_tags`` does. ``label_of(target)``
    names the VM for the operator and for the audit row.

    Raises ``HTTPException`` for a refusal that applies to the whole request — a
    protected key, an illegal key for this provider, nothing selected, too many selected
    — so none of those can half-apply. A failure that belongs to one VM never raises.
    """
    add = {str(k): ("" if v is None else str(v)) for k, v in (add or {}).items()}
    remove = [str(k) for k in (remove or [])]

    if not add and not remove:
        raise HTTPException(status_code=400,
                            detail="Nothing to change — name a tag to add or remove.")

    # The guard, before anything is read or written. `assert_editable` covers both sides
    # of the edit: removing `managed-by` is exactly as damaging as overwriting it, and an
    # earlier draft that checked only `add` would have allowed the worse of the two.
    try:
        tag_policy.assert_editable(list(add) + remove)
        # No `existing` here: the per-resource cap depends on tags this has not read yet,
        # so only a grossly oversized request is caught. The provider enforces the real
        # cap and its refusal arrives as that VM's `failed` entry — which is honest, and
        # better than reading every VM twice to pre-empt a limit almost nobody hits.
        tag_policy.validate_edit(cloud, add, remove)
    except tag_policy.TagPolicyError as exc:
        # 409, not 400: the request is well-formed and the operator is allowed to edit
        # tags — this particular key is spoken for. 400 would read as "you typed it
        # wrong", which sends them to fix the wrong thing.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    targets = _dedupe(list(targets or []))
    if not targets:
        raise HTTPException(status_code=400, detail="No VMs selected.")
    if len(targets) > TAG_MAX_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=(f"{len(targets)} VMs selected; the limit for one tag edit is "
                    f"{TAG_MAX_TARGETS}."))

    updated, failed, unchanged = [], [], []
    for target in targets:
        label = label_of(target)
        try:
            before, after = await apply_one(target)
        except HTTPException as exc:
            failed.append({"name": label, "error": str(exc.detail)})
            continue
        except Exception as exc:  # noqa: BLE001 — one cloud's error must not end the run
            # The exception is logged whole and only its text is carried outward; a cloud
            # SDK error can quote a request id and an account id, which is not something
            # to render on a shared page.
            logger.warning("tag edit failed for %s on %s", label, cloud, exc_info=True)
            failed.append({"name": label, "error": str(exc)})
            continue

        if before == after:
            # The write was a no-op: every added key already held that value and every
            # removed key was already absent. Reported separately rather than as a
            # success, and deliberately NOT audited — an audit row for a change that did
            # not happen is the kind of entry that makes a log untrustworthy.
            unchanged.append({"name": label})
            continue

        updated.append({
            "name": label,
            "tags": tag_policy.normalise(after, cloud),
            "added": {k: v for k, v in add.items() if before.get(k) != v},
            "removed": [k for k in remove if k in before],
        })
        # One row per VM per change, so /audit's target filter can answer "what has
        # happened to this VM" and the chain records each resource separately. Placed
        # after the cloud call: a row claiming an edit the provider refused is worse than
        # no row. log_audit commits, which is safe here because nothing else is pending.
        job_service.log_audit(
            db, created_by, AUDIT_ACTION, target_vm=label,
            details={"cloud": cloud,
                     "added": updated[-1]["added"],
                     "removed": updated[-1]["removed"],
                     "tags_after": sorted(after)})

    return {"cloud": cloud, "count": len(updated), "updated": updated,
            "failed": failed, "unchanged": unchanged}
