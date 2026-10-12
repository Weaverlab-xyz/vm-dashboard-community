"""Cloud APIs and spend: are the dashboard's own cloud credentials working, and are the
cost controls doing their job?

Everything here is already stored by the collectors and sweepers that do the work, so
this area makes no call of its own:

* **Cloud APIs** -- the dashboard's tile collector (``DashboardStatCache``) records, per
  provider, the last error and how many reads in a row have failed. A provider whose
  reads keep failing is a credential that expired or lost a permission, and every page
  for that cloud is showing stale data.
* **Cost reporting** -- the same, from the cost cache.
* **Spend caps** -- VMs stopped at their cap, or close to it.
* **Auto-delete** -- the last reaper pass: failures, and resources past their expiry
  that the timer is not allowed to delete.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .. import core

_PROVIDER_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud", "oci": "OCI",
                    "portainer": "Portainer", "rancher": "Rancher"}


def provider_items(rows, *, kind: str, area_label: str, now: datetime) -> list:
    """One item per provider (or cloud) from cache rows. Pure over row attributes:
    ``provider``/``cloud``, ``last_error``, ``consecutive_failures``, ``fetched_at``."""
    by: dict = {}
    for row in rows:
        key = getattr(row, "provider", None) or getattr(row, "cloud", None) or ""
        if key:
            by.setdefault(key, []).append(row)
    out = []
    for key, group in sorted(by.items()):
        failing = [r for r in group if (r.consecutive_failures or 0) > 0 and r.last_error]
        fetched = max((r.fetched_at for r in group if r.fetched_at), default=None)
        if failing:
            worst = max(failing, key=lambda r: r.consecutive_failures or 0)
            status = core.FAIL if len(failing) == len(group) else core.WARN
            detail = (f"{len(failing)} of {len(group)} read(s) failing, "
                      f"{worst.consecutive_failures} time(s) in a row: "
                      f"{core.text(worst.last_error, 250)}")
        elif fetched:
            status, detail = core.OK, f"Answered {core.ago(fetched, now)}."
        else:
            status, detail = core.UNKNOWN, "Not read yet."
        label = _PROVIDER_LABELS.get(key, key)
        out.append(core.Item(object_id=f"{kind}:{key}", name=f"{label} {area_label}",
                             kind=area_label, href="/settings",
                             checks=[core.check(kind, area_label, status, detail)]))
    return out


def spend_item(capped: int, warned: int, total: int) -> Optional[core.Item]:
    """Spend caps across every capped VM, or None when no VM has a cap. Pure."""
    if not total:
        return None
    if capped:
        status, detail = core.WARN, f"{capped} VM(s) stopped at their spend cap."
    elif warned:
        status, detail = core.WARN, f"{warned} VM(s) close to their spend cap."
    else:
        status, detail = core.OK, f"{total} VM(s) with a cap, none near it."
    return core.Item(object_id="spend-caps", name="Spend caps", kind="Cost control",
                     href="/inventory", checks=[core.check("spend", "Spend caps", status,
                                                           detail)])


def expiry_item(result: dict, *, enabled: bool) -> Optional[core.Item]:
    """The auto-delete timer's last pass, or None when the feature is off. Pure."""
    if not enabled:
        return None
    checks = []
    if result.get("never_run"):
        checks.append(core.check("ran", "Last pass", core.UNKNOWN, "Has not run yet."))
    elif result.get("corrupted"):
        checks.append(core.check("ran", "Last pass", core.UNKNOWN,
                                 "The stored result could not be read."))
    else:
        failed = result.get("failed") or []
        failed_n = len(failed) if isinstance(failed, list) else int(failed or 0)
        due = int(result.get("due") or 0)
        ended = core.parse_when(result.get("ended_at"))
        if failed_n:
            checks.append(core.check("ran", "Last pass", core.FAIL,
                                     f"{failed_n} deletion(s) failed in the pass "
                                     f"{core.ago(ended)}. See the auto-delete panel."))
        elif due and not result.get("deleting"):
            checks.append(core.check("ran", "Last pass", core.WARN,
                                     f"{due} resource(s) are past their expiry, but the "
                                     f"timer is not armed to delete them."))
        else:
            checks.append(core.check("ran", "Last pass", core.OK,
                                     f"Ran {core.ago(ended)}; "
                                     f"{int(result.get('reaped') or 0)} removed."))
    return core.Item(object_id="auto-delete", name="Auto-delete timer", kind="Cost control",
                     href="/settings", checks=checks)


async def collect(db) -> list:
    from ... import expiry_policy, expiry_reaper, suspend_sweeper
    from ....database import CloudCostCache, DashboardStatCache, Job
    now = datetime.utcnow()
    items = provider_items(db.query(DashboardStatCache).all(), kind="api",
                           area_label="API", now=now)
    items += provider_items(db.query(CloudCostCache).all(), kind="cost",
                            area_label="cost data", now=now)
    capped_vms = (db.query(Job)
                  .filter(Job.job_type.in_(suspend_sweeper._DEPLOY_TYPES),
                          Job.status == "completed", Job.spend_cap_usd.isnot(None)).all())
    capped_vms = [j for j in capped_vms if not j.metadata_dict.get("destroyed")]
    spend = spend_item(sum(1 for j in capped_vms if j.spend_capped_at),
                       sum(1 for j in capped_vms if j.spend_warned_at), len(capped_vms))
    if spend:
        items.append(spend)
    expiry = expiry_item(expiry_reaper.get_last_sweep_result(),
                         enabled=expiry_policy.enabled())
    if expiry:
        items.append(expiry)
    return items
