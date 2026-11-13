"""The Demo hub's API: every demo cell this caller may see, and the launchpad.

The estate instance's counterpart to /pov. Mounted in main.py behind
``_profile_page_gate("demo_pages")``, so on a POV instance it 404s naming the profile, as
the page does. Every route here is `demo:read`, explicit form (api/demo_gates).

Read-only, by design. Building and managing a cell stays on the cell's own routes
(/api/ot, /api/netcell, /api/agentcell), which accept `demo:<level>` beside the scope they
always took -- so the hub never becomes a second, differently-gated way to deploy.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import demo_cells, demo_launchpad
from .demo_gates import may, require_demo_read
from .gcp import _accessible_workgroups

router = APIRouter(prefix="/api/demo", tags=["demo"])

# Display order on the page, and the copy that says what each cell is for.
_CELLS = (
    ("ot", "OT demo cells",
     "A simulated plant -- FUXA HMI and PLC protocols -- reached only through PRA."),
    ("net", "Network demo cells",
     "A VyOS router with no inbound rule, managed through PRA and Password Safe."),
    ("agent", "Agent demo cells",
     "A non-human worker: SPIFFE identity, scoped token, and an off switch."),
)


@router.get("/summary")
def summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_demo_read),
):
    """The cells, by kind, and the launchpad.

    Workgroup scoping is GCP's ``_accessible_workgroups``, which every cloud module and
    the home page's tiles share, so a cell listed here is one the caller's own cloud page
    would show them.
    """
    accessible = _accessible_workgroups(current_user)

    def _may_read(scope):
        return may(current_user, scope, "read")

    results = {
        "ot": demo_cells.ot_cells(db, _may_read, accessible),
        "net": demo_cells.net_cells(db, _may_read, accessible),
        "agent": demo_cells.agent_cells(db, accessible, current_user.username),
    }
    cells = []
    for kind, title, blurb in _CELLS:
        res = results[kind]
        rows = res["cells"]
        cells.append({
            "kind": kind,
            "title": title,
            "blurb": blurb,
            "state": res["state"],
            "note": res["note"],
            "total": len(rows),
            "running": sum(1 for r in rows if r["status"] == "completed"),
            "cells": rows,
        })
    return {"cells": cells, "launchpad": demo_launchpad.describe()}
