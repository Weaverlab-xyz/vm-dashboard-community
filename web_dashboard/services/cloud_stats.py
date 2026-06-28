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
