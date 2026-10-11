#!/usr/bin/env python3
"""Render the Entitle HCL the dashboard generates at run time, for `terraform validate`.

`scripts/ci/terraform_validate.sh` checks every module under terraform/ against the real
provider schema. The Entitle virtual-application and bundle configurations are not
modules — `entitle_registration_service` writes them into a temp dir per job — so they
would otherwise meet the provider for the first time on a customer's apply. This writes
one directory per generator, filled with the catalog's real shapes and placeholder UUIDs
(the provider validates id format at plan time), so CI validates exactly what ships.

Standard library only: the generators import nothing else at module level.

Usage: python3 scripts/ci/render_entitle_hcl.py OUT_DIR   → OUT_DIR/{virtual_app,bundles}
"""
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from web_dashboard.services import entitle_persona_bundles as bundles  # noqa: E402
from web_dashboard.services import entitle_registration_service as reg  # noqa: E402
from web_dashboard.services import entitle_tiers  # noqa: E402


def _id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vm-dashboard-ci/{label}"))


_CFG = {"entitle_owner_id": _id("owner"), "entitle_workflow_id": _id("workflow"),
        "entitle_api_url": "https://api.us.entitle.io/v1"}
reg._cfg = lambda key: _CFG.get(key, "")


def main(out: str) -> None:
    resources = {}
    for b in bundles.catalog():
        resources.setdefault(b["cloud"].upper(), []).append({
            "key": b["group"], "name": b["name"], "virtualized_role_id": _id(b["group"]),
            "workflow_id": _id(f"workflow-{b['tier']}"),
            "allowed_durations": list(entitle_tiers.TIER_DURATIONS[b["tier"]]),
        })
    vapp = reg._generate_virtual_app_hcl(
        name="Cloud access (ci)", tags=("vm-dashboard", "virtual-app"),
        resources=[{"name": k, "description": f"{k} groups", "roles": v}
                   for k, v in resources.items()])
    bhcl = reg._generate_bundles_hcl(bundles=[{
        "key": b["key"], "name": b["name"], "description": b["purpose"],
        "category": bundles.CATEGORY, "tags": ["vm-dashboard", f"cloud:{b['cloud']}"],
        "workflow_id": _id(f"workflow-{b['tier']}"), "allowed_durations": b["durations"],
        "role_ids": [_id(f"{b['key']}-{i}") for i in range(3)],
    } for b in bundles.catalog()])
    for name, hcl in (("virtual_app", vapp), ("bundles", bhcl)):
        d = os.path.join(out, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "main.tf"), "w") as fh:
            fh.write(hcl)
        print(d)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
