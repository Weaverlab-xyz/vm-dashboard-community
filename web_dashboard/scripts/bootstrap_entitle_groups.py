"""
Provision the Entra ID security groups + matching ``oauth_group_mappings``
rows that back the Entitle user-based JIT authorization flow.

See ``docs/design/entitle-user-jit.md`` Phase 1 for the contract.

One-shot, operator-run, idempotent. The script:

  1. Computes the planned group inventory (admin + baseline +
     scope/level + per-workgroup) from the dashboard's own
     ``PERMISSION_SCOPES`` / ``PERMISSION_LEVELS`` / workgroups
     sources so the dashboard's RBAC and Entra stay in sync.
  2. For each planned group, queries Microsoft Graph by
     ``displayName``. If the group exists, reuses its object id;
     if not, creates it as a security group.
  3. For each (object id, default_permissions, workgroup) tuple,
     upserts an ``oauth_group_mappings`` row so the dashboard's
     resolver sees it on the next login.

Both halves are idempotent — re-running the script after an
operator manually edits a group's mapping in the ``/groups`` UI
will not overwrite the edit (it matches by ``entra_group_id``,
not ``display_name``).

Usage:
  python -m web_dashboard.scripts.bootstrap_entitle_groups \\
      --client-id <app-reg-id> --client-secret <secret> \\
      [--tenant-id <override>] \\
      [--scope=permissions|workgroups|baseline|admin|all] \\
      [--tenant-prefix=<slug>] \\
      [--dry-run] [--yes]

Required Graph permissions on the app registration:
  ``Group.ReadWrite.All`` (application permission, admin consented).

The OAuth login app registration is typically **not** the same app
— that one only needs ``openid``/``profile``/``email`` and
``GroupMember.Read.All``. Create a separate app registration for
this bootstrap and treat its secret as a per-deployment Key Vault
entry.

See ``docs/runbooks/entitle-user-jit-phase-1-bootstrap-entra.md``
for the operator walk-through.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from typing import Iterable

import httpx
from azure.identity import ClientSecretCredential
from sqlalchemy.orm import Session

from ..config import settings
from ..database import OAuthGroupMapping, SessionLocal

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Scope ordering kept in sync with api/auth.py PERMISSION_SCOPES /
# PERMISSION_LEVELS. The script imports them at runtime rather than
# hard-coding so a new scope added to api/auth.py is automatically
# covered the next time bootstrap runs.
from ..api.auth import PERMISSION_LEVELS, PERMISSION_SCOPES  # noqa: E402


@dataclass(frozen=True)
class PlannedGroup:
    """One group we intend to (idempotently) provision in Entra + DB."""

    display_name: str
    description: str
    workgroup: str  # required by the oauth_group_mappings table
    default_permissions: dict | None  # None = "all permissions" (matches table semantics)


def _norm(name: str) -> str:
    """Force a group name to the lowercase canonical the dashboard expects.

    Entra is case-insensitive for displayName lookups but stores the
    casing as-given. Lowercase keeps the run-once / run-twice diff
    empty and avoids 'Dashboard-Aws-Read' / 'dashboard-aws-read' drift.
    """
    return name.lower()


def _build_inventory(
    scopes: set[str],
    workgroups: Iterable[str],
    tenant_prefix: str | None,
    placeholder_workgroup: str,
) -> list[PlannedGroup]:
    """Return the full list of groups + mappings to provision.

    ``scopes`` chooses which subsets are included: any of
    ``"admin"``, ``"baseline"``, ``"permissions"``, ``"workgroups"``.

    ``placeholder_workgroup`` fills the (non-nullable) workgroup
    column on the mapping rows for entries that aren't workgroup-
    specific (admin / baseline / permission tuples). The dashboard
    treats it as the user's default workgroup if no
    ``dashboard-workgroup-*`` membership claims a stronger one;
    operators usually point it at their first/primary workgroup.
    """
    pfx = f"dashboard-{tenant_prefix}-" if tenant_prefix else "dashboard-"
    out: list[PlannedGroup] = []

    if "admin" in scopes:
        out.append(
            PlannedGroup(
                display_name=_norm(f"{pfx}admin"),
                description=(
                    "VM Dashboard — admin role. Members are granted "
                    "is_admin=true at login. High-value grant; "
                    "recommend non-auto-approve Entitle policy."
                ),
                workgroup=placeholder_workgroup,
                default_permissions={"is_admin": True},
            )
        )

    if "baseline" in scopes:
        out.append(
            PlannedGroup(
                display_name=_norm(f"{pfx}baseline"),
                description=(
                    "VM Dashboard — safe-read baseline (vms:read + "
                    "jobs:read). Recommended auto-approve in Entitle."
                ),
                workgroup=placeholder_workgroup,
                default_permissions={"vms": ["read"], "jobs": ["read"]},
            )
        )

    if "permissions" in scopes:
        for scope in PERMISSION_SCOPES:
            for level in PERMISSION_LEVELS:
                out.append(
                    PlannedGroup(
                        display_name=_norm(f"{pfx}{scope}-{level}"),
                        description=(
                            f"VM Dashboard — grants {scope}:{level} "
                            "for the duration of the Entra group membership."
                        ),
                        workgroup=placeholder_workgroup,
                        default_permissions={scope: [level]},
                    )
                )

    if "workgroups" in scopes:
        for wg in workgroups:
            out.append(
                PlannedGroup(
                    display_name=_norm(f"{pfx}workgroup-{wg}"),
                    description=(
                        f"VM Dashboard — places the member into workgroup "
                        f"'{wg}' for the duration of the membership. "
                        "default_permissions is NULL — combine with a "
                        "permission-tuple group for write access."
                    ),
                    workgroup=wg,
                    default_permissions=None,
                )
            )

    return out


def _get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    cred = ClientSecretCredential(
        tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
    )
    return cred.get_token(GRAPH_SCOPE).token


def _find_group_id(client: httpx.Client, display_name: str) -> str | None:
    """Return the Entra object id for an existing group, or None.

    Filters by displayName equality. Graph is case-insensitive on this
    filter so a previously-created TitleCase variant is still found.
    """
    safe = display_name.replace("'", "''")
    r = client.get(
        f"{GRAPH}/groups",
        params={"$filter": f"displayName eq '{safe}'", "$select": "id,displayName"},
    )
    r.raise_for_status()
    items = r.json().get("value", [])
    return items[0]["id"] if items else None


def _create_group(client: httpx.Client, plan: PlannedGroup) -> str:
    """Create the security group; return its new object id."""
    body = {
        "displayName": plan.display_name,
        "description": plan.description,
        "mailEnabled": False,
        "mailNickname": plan.display_name.replace(" ", "-"),
        "securityEnabled": True,
    }
    r = client.post(f"{GRAPH}/groups", json=body)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Graph refused group create for {plan.display_name!r}: "
            f"{r.status_code} {r.text}"
        )
    return r.json()["id"]


def _upsert_mapping(
    db: Session, *, entra_group_id: str, plan: PlannedGroup, dry_run: bool
) -> str:
    """Insert an ``oauth_group_mappings`` row if absent.

    Returns one of ``"created"`` / ``"exists"`` so the caller can
    summarise. Never overwrites an operator-edited mapping (we match
    by entra_group_id, the immutable identifier).
    """
    existing = (
        db.query(OAuthGroupMapping)
        .filter(OAuthGroupMapping.entra_group_id == entra_group_id)
        .first()
    )
    if existing:
        return "exists"

    if dry_run:
        return "created"  # caller logs; nothing persisted

    db.add(
        OAuthGroupMapping(
            id=str(uuid.uuid4()),
            entra_group_id=entra_group_id,
            display_name=plan.display_name,
            workgroup=plan.workgroup,
            default_permissions=(
                json.dumps(plan.default_permissions) if plan.default_permissions else None
            ),
        )
    )
    return "created"


def _summarise(rows: list[tuple[str, str, str]]) -> None:
    """Print a short table: action, group, mapping. ``rows`` is
    ``(action, display_name, mapping_action)``."""
    if not rows:
        print("(no changes planned)")
        return
    width = max(len(r[1]) for r in rows)
    print(f"{'action':<10} {'group':<{width}}  mapping")
    print(f"{'-'*10} {'-'*width}  {'-'*8}")
    for action, name, mapping in rows:
        print(f"{action:<10} {name:<{width}}  {mapping}")


def run(
    *,
    client_id: str,
    client_secret: str,
    tenant_id: str,
    scopes: set[str],
    tenant_prefix: str | None,
    placeholder_workgroup: str,
    workgroups: list[str],
    dry_run: bool,
) -> int:
    """Top-level entry: returns a process exit code."""
    inventory = _build_inventory(
        scopes=scopes,
        workgroups=workgroups,
        tenant_prefix=tenant_prefix,
        placeholder_workgroup=placeholder_workgroup,
    )

    if not inventory:
        print("Nothing to do — selected --scope produced an empty inventory.")
        return 0

    print(f"Planned inventory: {len(inventory)} groups (scope={','.join(sorted(scopes))})")
    if dry_run:
        print("(dry-run: no Graph writes, no DB commits)")

    token = _get_token(tenant_id, client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    summary: list[tuple[str, str, str]] = []
    with httpx.Client(headers=headers, timeout=30.0) as client, SessionLocal() as db:
        for plan in inventory:
            existing_id = _find_group_id(client, plan.display_name)
            if existing_id:
                action = "exists"
                group_id = existing_id
            else:
                if dry_run:
                    action = "create"
                    # Use a placeholder id so the mapping summary makes
                    # sense; nothing is written.
                    group_id = f"<would-create:{plan.display_name}>"
                else:
                    group_id = _create_group(client, plan)
                    action = "created"

            mapping_action = _upsert_mapping(
                db, entra_group_id=group_id, plan=plan, dry_run=dry_run
            )
            summary.append((action, plan.display_name, mapping_action))

        if not dry_run:
            db.commit()

    print()
    _summarise(summary)
    print()
    created = sum(1 for a, _, _ in summary if a == "created")
    existed = sum(1 for a, _, _ in summary if a == "exists")
    mapped = sum(1 for _, _, m in summary if m == "created")
    print(f"Groups: {created} created, {existed} already existed.")
    print(f"Mappings: {mapped} new ({len(summary) - mapped} already mapped).")
    if dry_run:
        print("Re-run without --dry-run to apply.")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bootstrap_entitle_groups",
        description=(
            "Provision Entra security groups + oauth_group_mappings "
            "rows for the Entitle user-JIT flow. Idempotent."
        ),
    )
    p.add_argument("--client-id", required=True, help="Bootstrap app registration client id.")
    p.add_argument(
        "--client-secret",
        required=True,
        help="Bootstrap app registration client secret. Use Key Vault, not a literal.",
    )
    p.add_argument(
        "--tenant-id",
        default=None,
        help="Entra tenant id (defaults to settings.azure_oauth_tenant_id).",
    )
    p.add_argument(
        "--scope",
        default="all",
        choices=["admin", "baseline", "permissions", "workgroups", "all"],
        help="Which subset to provision. 'all' = admin + baseline + permissions + workgroups.",
    )
    p.add_argument(
        "--tenant-prefix",
        default=None,
        help=(
            "Optional slug inserted between 'dashboard-' and the suffix "
            "for per-tenant deployments (Phase 5). Example: 'acme' "
            "produces 'dashboard-acme-aws-read'."
        ),
    )
    p.add_argument(
        "--placeholder-workgroup",
        default=None,
        help=(
            "Workgroup name written to oauth_group_mappings for "
            "non-workgroup-specific entries (admin/baseline/permission "
            "tuples). Defaults to the first key in settings.workgroups, "
            "or 'default' if no workgroups are configured."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Plan only; no Graph or DB writes.")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    tenant_id = args.tenant_id or settings.azure_oauth_tenant_id
    if not tenant_id:
        print(
            "ERROR: tenant id not provided and settings.azure_oauth_tenant_id is empty.",
            file=sys.stderr,
        )
        return 2

    if args.scope == "all":
        scopes = {"admin", "baseline", "permissions", "workgroups"}
    else:
        scopes = {args.scope}

    configured_wgs = list(settings.workgroups.keys()) if settings.workgroups else []
    placeholder = args.placeholder_workgroup or (configured_wgs[0] if configured_wgs else "default")

    if not args.yes and not args.dry_run:
        print(f"About to provision Entra groups in tenant {tenant_id} (scope={args.scope}).")
        print(f"  Placeholder workgroup for non-WG mappings: {placeholder}")
        if "workgroups" in scopes:
            print(f"  Workgroup groups: {', '.join(configured_wgs) or '(none configured)'}")
        if args.tenant_prefix:
            print(f"  Tenant prefix:    dashboard-{args.tenant_prefix}-*")
        ans = input("Continue? [y/N] ").strip().lower()
        if ans not in {"y", "yes"}:
            print("Aborted.")
            return 1

    return run(
        client_id=args.client_id,
        client_secret=args.client_secret,
        tenant_id=tenant_id,
        scopes=scopes,
        tenant_prefix=args.tenant_prefix,
        placeholder_workgroup=placeholder,
        workgroups=configured_wgs,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
