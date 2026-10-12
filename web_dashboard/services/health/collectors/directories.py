"""Directories: the Directory health page's results, shown alongside the other areas.

Nothing is collected here. ``directory_health_service`` keeps checking directories on its
own schedule and into its own table (it predates this framework and has an agent probe
that completes on the agent's schedule); this only mirrors its stored rows, so the
Health page can show every area in one place without asking anything twice.
"""
from __future__ import annotations

from .. import core


async def collect(db) -> list:
    from ... import directory_health_service as dh, directory_service
    rows = [r for r in directory_service.list_directories(db) if dh.monitored(r)]
    snap = dh.snapshot(db, rows)
    return [core.Item(object_id=d["directory_id"], name=d["name"],
                      kind=d["provider_label"], href="/directories/health",
                      checks=d["checks"] or [core.check(
                          "pending", "Health", core.UNKNOWN, "Not checked yet.")])
            for d in snap["directories"]]
