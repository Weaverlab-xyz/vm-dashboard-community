"""Pure helpers for the per-cloud dashboard-stats endpoints.

Kept dependency-free (no cloud SDKs, no DB) so the counting/RBAC logic the
dashboard tiles rely on is unit-testable on its own. The API layer fetches the
(cached) instance list, then calls these to produce the tile counts.
"""


def summarize_instances(rows, accessible, running_field, running_value="running"):
    """Filter instance dicts to the caller's workgroups and count total + running.

    - ``accessible=None`` → admin: every row is visible.
    - otherwise a row is visible when its ``workgroup`` is in ``accessible``.
    - ``running`` counts visible rows whose ``running_field`` equals
      ``running_value`` case-insensitively (so AWS/Azure ``state="running"`` and
      GCP ``status="RUNNING"`` both work).
    """
    visible = [r for r in (rows or [])
               if accessible is None or r.get("workgroup") in accessible]
    running = sum(1 for r in visible
                  if str(r.get(running_field, "")).lower() == running_value.lower())
    return {"total": len(visible), "running": running}


def summarize_by_region(rows, accessible, running_field, region_field,
                        running_value="running"):
    """Same counts as ``summarize_instances``, broken down by region.

    ``region_field`` names the per-row key holding the region — ``"region"`` for
    AWS/GCP, ``"location"`` for Azure. Rows with a blank/absent region are
    grouped under ``"unknown"`` so they stay visible in the tile rather than
    silently vanishing from the breakdown.

    Returns ``{region: {"total": int, "running": int}}``.
    """
    by_region = {}
    for r in (rows or []):
        if accessible is not None and r.get("workgroup") not in accessible:
            continue
        region = str(r.get(region_field) or "").strip() or "unknown"
        bucket = by_region.setdefault(region, {"total": 0, "running": 0})
        bucket["total"] += 1
        if str(r.get(running_field, "")).lower() == running_value.lower():
            bucket["running"] += 1
    return by_region
