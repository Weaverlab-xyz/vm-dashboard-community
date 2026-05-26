"""
Bootstrap the Entitle virtual application — Phase 2 wrapper.

Reads the dashboard-* Entra groups Phase 1 wrote into
``oauth_group_mappings`` (one row per group, each carrying the Entra
object id) and emits a ``groups.auto.tfvars.json`` file that the
``terraform/entitle_user_jit/`` module consumes. Optionally runs
``terraform init`` + ``terraform plan`` or ``terraform apply`` for the
operator.

Tier assignment matches the design (§5.4 of the user-JIT doc):
  - auto_approve     : baseline + every *-read
  - single_approver  : every *-write + workgroup-*
  - two_approver     : every *-delete + admin

Re-running is idempotent: the tfvars file is overwritten and the
underlying Terraform module identifies entities by name, so a second
apply is a no-op when nothing changed.

Usage:
  python -m web_dashboard.scripts.bootstrap_entitle_app \\
      [--output-tfvars terraform/entitle_user_jit/groups.auto.tfvars.json] \\
      [--apply | --plan] \\
      [--entitle-integration-id <id>] \\
      [--single-approver-group <id>] \\
      [--two-approver-group <id>] \\
      [--workdir terraform/entitle_user_jit]

Without --apply / --plan the script just writes the tfvars file and
exits — useful for CI flows that own the Terraform invocation.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from ..database import OAuthGroupMapping, SessionLocal

logger = logging.getLogger(__name__)


# ── Tier inference ───────────────────────────────────────────────────────────
#
# The bootstrap script is the canonical place where group-name shape →
# tier mapping lives. Edits here flow through the tfvars file into the
# Terraform module's workflow_id_by_tier lookup.

_AUTO_APPROVE_SUFFIXES = ("-read",)
_SINGLE_APPROVER_SUFFIXES = ("-write",)
_TWO_APPROVER_SUFFIXES = ("-delete",)


def _tier_for_group(display_name: str) -> str:
    """Return the workflow tier this dashboard-* group should route through.

    Match order:
      1. exact 'dashboard-admin' or 'dashboard-<tenant>-admin' → two_approver
      2. exact 'dashboard-baseline' → auto_approve
      3. workgroup-* → single_approver
      4. *-read | *-write | *-delete suffix → respective tier
      5. fallback → single_approver (the safe-but-not-the-loosest default)
    """
    name = display_name.lower()
    # admin is the highest-blast-radius grant — always two_approver.
    if name.endswith("-admin") or name == "dashboard-admin":
        return "two_approver"
    if name == "dashboard-baseline" or name.endswith("-baseline"):
        return "auto_approve"
    # workgroup membership is a single-approver action (joining a workgroup
    # gates downstream access; the action itself is reversible).
    if "-workgroup-" in name:
        return "single_approver"
    if any(name.endswith(s) for s in _AUTO_APPROVE_SUFFIXES):
        return "auto_approve"
    if any(name.endswith(s) for s in _SINGLE_APPROVER_SUFFIXES):
        return "single_approver"
    if any(name.endswith(s) for s in _TWO_APPROVER_SUFFIXES):
        return "two_approver"
    logger.warning(
        "tier inference fell through for group %r — defaulting to single_approver",
        display_name,
    )
    return "single_approver"


def _tfvar_key(display_name: str) -> str:
    """A stable, Terraform-friendly key for the groups map.

    Terraform's ``for_each`` over a map of objects keys the resources by
    the map key. We use a sanitised version of the group name so the
    state file's resource addresses stay readable
    (``entitle_resource.dashboard_group["dashboard-aws-write"]``).
    """
    return re.sub(r"[^a-z0-9-]+", "-", display_name.lower()).strip("-")


# ── tfvars generation ────────────────────────────────────────────────────────

def build_groups_tfvars(db) -> dict:
    """Read oauth_group_mappings and shape it into the Terraform module's
    ``groups`` variable. Returns the dict to be JSON-serialised."""
    rows = (
        db.query(OAuthGroupMapping)
        .filter(OAuthGroupMapping.display_name.like("dashboard-%"))
        .order_by(OAuthGroupMapping.display_name.asc())
        .all()
    )
    groups: dict[str, dict] = {}
    for row in rows:
        if not row.entra_group_id:
            logger.warning("skipping %r — no entra_group_id set", row.display_name)
            continue
        groups[_tfvar_key(row.display_name)] = {
            "display_name": row.display_name,
            "directory_group_id": row.entra_group_id,
            # Surface a useful description in the Entitle catalog. If the
            # row carries one we use it verbatim; otherwise synthesise from
            # the name so the catalog doesn't look empty.
            "description": _group_description(row),
            "tier": _tier_for_group(row.display_name),
        }
    return {"groups": groups}


def _group_description(row) -> str:
    """Best-effort description for an Entitle catalog entry."""
    # OAuthGroupMapping in prod doesn't have a description column, so we
    # synthesise from the name. If a row carries a non-empty
    # default_permissions blob, append it in parentheses so the operator
    # sees what the group grants without leaving the Entitle UI.
    base = f"VM Dashboard JIT: {row.display_name}"
    perms = (row.default_permissions or "").strip()
    if perms and perms not in {"{}", "null"}:
        return f"{base} ({perms})"
    return base


def write_tfvars(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    print(f"Wrote {len(payload.get('groups', {}))} groups -> {path}")


# ── Terraform invocation ─────────────────────────────────────────────────────

def run_terraform(workdir: Path, action: str, env_extra: dict) -> int:
    """Run ``terraform <action>`` in ``workdir`` with the supplied env."""
    if action not in {"init", "plan", "apply"}:
        raise ValueError(f"unsupported action {action!r}")
    env = dict(os.environ)
    env.update(env_extra)
    cmd = ["terraform", "-chdir", str(workdir), action]
    if action == "apply":
        cmd.append("-auto-approve")
    print(f"+ {' '.join(cmd)}")
    return subprocess.call(cmd, env=env)


# ── Entry point ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--workdir",
        default="terraform/entitle_user_jit",
        help="Path to the Terraform module directory.",
    )
    parser.add_argument(
        "--output-tfvars",
        default=None,
        help="tfvars JSON file to write. Defaults to <workdir>/groups.auto.tfvars.json.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="After writing tfvars, run `terraform init` + `terraform plan`.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="After writing tfvars, run `terraform init` + `terraform apply -auto-approve`.",
    )
    parser.add_argument(
        "--entitle-integration-id",
        default=os.environ.get("ENTITLE_INTEGRATION_ID", ""),
        help="Entra <-> Entitle integration id. Falls back to ENTITLE_INTEGRATION_ID env.",
    )
    parser.add_argument(
        "--single-approver-group",
        default=os.environ.get("SINGLE_APPROVER_GROUP", ""),
        help="Approver id for the single-approver tier. Falls back to SINGLE_APPROVER_GROUP env.",
    )
    parser.add_argument(
        "--two-approver-group",
        default=os.environ.get("TWO_APPROVER_GROUP", ""),
        help="Approver id for the two-approver tier. Falls back to TWO_APPROVER_GROUP env.",
    )
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    if not workdir.exists():
        print(f"ERROR: workdir {workdir} not found", file=sys.stderr)
        return 2

    tfvars_path = Path(args.output_tfvars) if args.output_tfvars else workdir / "groups.auto.tfvars.json"

    db = SessionLocal()
    try:
        payload = build_groups_tfvars(db)
    finally:
        db.close()

    if not payload.get("groups"):
        print(
            "WARNING: no dashboard-* groups found in oauth_group_mappings — "
            "run bootstrap_entitle_groups.py first (Phase 1).",
            file=sys.stderr,
        )

    write_tfvars(tfvars_path, payload)

    if not (args.plan or args.apply):
        return 0

    if not (args.entitle_integration_id and args.single_approver_group and args.two_approver_group):
        print(
            "ERROR: --entitle-integration-id / --single-approver-group / --two-approver-group "
            "all required for plan / apply (or set the matching env vars).",
            file=sys.stderr,
        )
        return 2

    # Pass the operator-supplied vars through TF_VAR_* env so the
    # terraform command line stays short and the API key never lands on
    # any shell-history line.
    env_extra = {
        "TF_VAR_entitle_integration_id": args.entitle_integration_id,
        "TF_VAR_single_approver_group": args.single_approver_group,
        "TF_VAR_two_approver_group": args.two_approver_group,
    }

    if rc := run_terraform(workdir, "init", env_extra):
        return rc
    action = "apply" if args.apply else "plan"
    return run_terraform(workdir, action, env_extra)


if __name__ == "__main__":
    sys.exit(main())
