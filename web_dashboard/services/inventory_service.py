"""Cross-provider deployment inventory — a normalized, read-only view of every
resource the dashboard has deployed, assembled from its own DB records (no live
cloud calls).

Cloud VMs + on-prem Proxmox/Nutanix VMs come from completed, non-destroyed deploy
Jobs; cloud databases, K8s clusters, and virtual-desktop seats come from their
inventory tables. Each row is normalized to one dict shape. RBAC filtering is the
API layer's job (see :func:`visible_to`), not the collector's.
"""
import logging
from typing import Optional, Set

from sqlalchemy.orm import Session

from ..database import CloudDatabase, Job, K8sCluster, VirtualDesktop

logger = logging.getLogger(__name__)

# deploy job_type → (cloud/provider label, resource page to link to)
_VM_JOBS = {
    "ec2_deploy":     ("aws",     "/aws#instances"),
    "azure_deploy":   ("azure",   "/azure#vms"),
    "gce_deploy":     ("gcp",     "/gcp"),
    "oci_deploy":     ("oci",     "/oci"),
    "proxmox_deploy": ("proxmox", "/proxmox"),
    "nutanix_deploy": ("nutanix", "/nutanix"),
}


def _iso(dt):
    return dt.isoformat() if dt else None


def _vm_item(job) -> dict:
    """Normalize a completed, non-destroyed VM deploy Job into an inventory item.
    Name/region are pulled from whichever metadata key the provider used, so this
    stays robust across the per-cloud deploy shapes."""
    meta = job.metadata_dict
    cloud, href = _VM_JOBS[job.job_type]
    name = (meta.get("instance_name") or meta.get("vm_name") or meta.get("name")
            or job.cloud_resource_id or "(unnamed)")
    region = (meta.get("region") or meta.get("location") or meta.get("zone")
              or meta.get("node") or meta.get("cluster") or "")
    return {
        "id": f"job:{job.id}",
        "cloud": cloud,
        "kind": "vm",
        "name": name,
        "region": region,
        "state": "active",
        "workgroup": (job.workgroup or None),
        "deployed_by": job.created_by,
        "created_at": _iso(job.created_at),
        # Auto-delete timer; None = never. A VM has no inventory table, so its expiry
        # lives on this deploy Job — which is already what `job:<id>` identifies.
        # `source` is "provisioned" by construction: collect() only reaches here for a
        # deploy the dashboard itself ran.
        "expires_at": _iso(job.expires_at),
        "source": "provisioned",
        "job_id": job.id,
        # Ansible's connection address, same resolution as the Config-Management
        # target picker (api/config_mgmt.get_cloud_targets). Empty for providers whose
        # deploy job records no address — Proxmox/Nutanix store node/vmid instead, and
        # those VMs are configured through their hypervisor GROUP, not individually.
        "ip": meta.get("public_ip") or meta.get("private_ip") or "",
        "detail_href": href,
    }


# ── VM name claims (the pre-flight behind count-based deploys) ────────────────
#
# Statuses that mean "this name is spoken for". `completed` minus `destroyed` is the
# live-VM rule collect() uses; the in-flight three are here because a batch submitted
# while another batch is still running would otherwise check against nothing and
# collide at launch. This is also why the check can't just reuse collect(), which
# returns completed rows only (and scans three unrelated tables besides).
_NAME_HOLDING_STATUSES = ("completed", "pending", "queued", "running")


def _name_claimed_by(job) -> Optional[str]:
    """The VM name a deploy Job lays claim to, or None if it claims none.

    Same key-resolution order as :func:`_vm_item` — deliberately shared, so the
    pre-flight and the inventory page can never disagree about what a VM is called.
    Kept free of the Session so it is unit-testable against plain stand-ins."""
    meta = job.metadata_dict or {}
    if meta.get("destroyed"):
        return None
    name = (meta.get("instance_name") or meta.get("vm_name") or meta.get("name")
            or job.cloud_resource_id or "")
    return name.strip() or None


def live_or_pending_vm_names(db: Session, job_type: str) -> Set[str]:
    """Casefolded names one provider's deploy jobs currently claim.

    Casefolded because the comparison has to err toward a false positive: EC2 Name
    tags are case-sensitive, Azure resource names are not, and GCE names are
    lowercase-only, so the insensitive comparison is the only one safe for all three.

    Honest about its limits — the jobs table is not authoritative for cloud state, so
    this catches collisions with VMs *this dashboard* created or is creating. A VM
    made outside the dashboard with the same name still collides at launch. That is
    the same fidelity collect() and api/oci._existing_freetier_usage already work at.
    """
    rows = (db.query(Job)
            .filter(Job.job_type == job_type,
                    Job.status.in_(_NAME_HOLDING_STATUSES))
            .all())
    names = set()
    for job in rows:
        claimed = _name_claimed_by(job)
        if claimed:
            names.add(claimed.casefold())
    return names


def _db_item(row) -> dict:
    return {
        "id": f"clouddb:{row.id}",
        "cloud": row.cloud,
        "kind": "database",
        # Separate from `name` so a bulk-run selection can check it against the
        # engines the ansible-cloud image actually ships client libraries for.
        "engine": row.engine,
        # registered = the dashboard didn't provision it; delete deregisters rather
        # than destroys, and its credential is a Password Safe managed account.
        "source": row.source or "provisioned",
        "name": f"{row.engine} {row.instance_id or row.private_host or row.id[:8]}".strip(),
        "region": row.region or "",
        "state": row.status,
        "workgroup": None,
        "deployed_by": row.created_by,
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "job_id": None,
        "detail_href": "/databases",
    }


def _k8s_item(row) -> dict:
    return {
        "id": f"k8s:{row.id}",
        "cloud": row.cloud,
        "kind": "k8s",
        # Same meaning as _db_item's: a `registered` cluster is one the dashboard was
        # told about rather than provisioned, so it is never auto-deleted.
        "source": row.source or "registered",
        "name": row.name,
        "region": row.region or "",
        "state": row.status,
        "workgroup": None,
        "deployed_by": row.created_by,
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "job_id": row.deploy_job_id,
        "detail_href": "/k8s",
    }


def _desktop_item(row) -> dict:
    name = row.pool_name + (f" · {row.assigned_user}" if row.assigned_user else "")
    return {
        "id": f"vdesktop:{row.id}",
        "cloud": row.cloud,
        "kind": "desktop",
        "name": name,
        "region": "",
        "state": row.status,
        "workgroup": None,
        "deployed_by": row.created_by,
        "created_at": _iso(row.created_at),
        # A seat never carries an auto-delete timer: its teardown is
        # vdesktop_pool_teardown(seat_ids), so expiring one seat would silently shrink a
        # live pool. There is deliberately no expires_at column on virtual_desktops, so
        # nothing can be stamped here that the sweeper wouldn't honour.
        "expires_at": None,
        "source": "provisioned",
        "job_id": None,
        "detail_href": "/desktops",
    }


def collect(db: Session) -> list:
    """Assemble the full (unfiltered) inventory from DB records. The returned
    dicts are detached from the session (all primitives), so the caller may close
    the session immediately."""
    items = []

    vm_jobs = (
        db.query(Job)
        .filter(Job.job_type.in_(tuple(_VM_JOBS)), Job.status == "completed")
        .order_by(Job.created_at.desc())
        .all()
    )
    for job in vm_jobs:
        if job.metadata_dict.get("destroyed"):
            continue
        items.append(_vm_item(job))

    for row in (db.query(CloudDatabase)
                .filter(CloudDatabase.status.notin_(("deleted", "decommissioned"))).all()):
        items.append(_db_item(row))

    for row in db.query(K8sCluster).filter(K8sCluster.status != "deleted").all():
        items.append(_k8s_item(row))

    for row in (db.query(VirtualDesktop)
                .filter(VirtualDesktop.status.notin_(("deprovisioning", "deleted"))).all()):
        items.append(_desktop_item(row))

    # Annotate each row with whether it can be a Config-Management target, and why
    # not when it can't. Derived from the same _target_spec the bulk-run endpoint
    # validates with, so the page's checkboxes and the server's guard can never
    # disagree — and the operator sees the reason on hover instead of after a 400.
    # Annotate each row with whether it can carry an auto-delete timer, and why not when
    # it can't — the same contract as cfg_runnable/cfg_reason above, for the same reason:
    # the Expires column renders the control only when eligible and shows this reason on
    # hover, so the page and the sweeper's own guard can never disagree.
    #
    # Note what is deliberately NOT computed here: an "expiring soon" boolean. collect()
    # is cached for 60s, so a time-derived flag would go stale inside the cache (the
    # discipline cost_service.apply_budget_alerts documents). Clients get the raw
    # expires_at plus the warn threshold and derive it live.
    from . import expiry_policy
    for item in items:
        spec = _target_spec(item)
        unrunnable = isinstance(spec, str)
        item["cfg_runnable"] = not unrunnable
        item["cfg_reason"] = spec if unrunnable else ""

        capable, why = expiry_policy.ttl_capable(item)
        exempt = capable and expiry_policy.is_exempt(item)
        item["ttl_capable"] = capable and not exempt
        item["expiry_exempt"] = exempt
        item["ttl_reason"] = (
            "its workgroup is exempt from auto-delete" if exempt else why
        )

    return items


def accessible_workgroups(user):
    """Canonical workgroup names a user can see, or ``None`` for admins — the value
    :func:`visible_to` expects. Shared by the inventory listing and the bulk-run
    endpoint so a change to inventory RBAC can't apply to reading but not to acting."""
    if user.is_effective_admin:
        return None
    return [w.lower() for w in user.workgroups_list]


def visible_to(item: dict, accessible, username: str) -> bool:
    """RBAC predicate. ``accessible=None`` → admin (sees everything). Otherwise a
    workgroup-scoped item (a VM) is visible when its workgroup is in the user's
    set; an item without a workgroup (database / k8s / desktop) is visible only to
    the user who created it."""
    if accessible is None:
        return True
    wg = item.get("workgroup")
    if wg:
        return wg in accessible
    return item.get("deployed_by") == username


# ── Bulk Config-Management selection ──────────────────────────────────────────

# Kinds that have a Config-Management path at all. "desktop" is a virtual-desktop
# seat — there is no Ansible target behind it, so it can never be selected.
CONFIG_MANAGEABLE_KINDS = ("vm", "k8s", "database")

# Ceiling on one bulk run. Each target becomes its own job, so a mis-click on
# "select all" against a large estate would otherwise fan out unbounded work.
MAX_BULK_TARGETS = 50


class BulkSelectionError(Exception):
    """A bulk selection that can't be run, with an operator-facing explanation."""


def _target_spec(item: dict):
    """How to aim a Config-Management run at this row: the RunRequest fields to set,
    or a string explaining why the row isn't runnable.

    The VM rule is deliberately provider-agnostic — a row is targetable if its deploy
    recorded an address, whoever deployed it — rather than an allowlist of clouds that
    would drift as providers are added."""
    from . import ansible_cloud_run_service as acr

    kind = item.get("kind")
    cloud = (item.get("cloud") or "").lower()

    if kind == "vm":
        ip = item.get("ip") or ""
        if not ip:
            return ("no recorded IP address — its deploy job stored none. Proxmox and "
                    "Nutanix VMs are configured through their hypervisor group target "
                    "on the Config Management page, not selected individually.")
        # `cloud` drives SSH-key retrieval and only means something for the three
        # clouds that store one; anything else runs as a plain ad-hoc IP target.
        return {"target": ip, "cloud": cloud if cloud in ("aws", "azure", "gcp") else ""}

    if kind == "k8s":
        if cloud not in acr.K8S_TARGET_CLOUDS:
            return (f"cloud {cloud!r} has no Ansible runner for Kubernetes targets "
                    f"(supported: {'/'.join(acr.K8S_TARGET_CLOUDS)}).")
        return {"target_kind": "k8s", "target_id": item["id"].split(":", 1)[1]}

    if kind == "database":
        if cloud not in acr.DB_TARGET_CLOUDS:
            return (f"cloud {cloud!r} has no Ansible runner for database targets "
                    f"(supported: {'/'.join(acr.DB_TARGET_CLOUDS)}).")
        engine = item.get("engine")
        if engine not in acr.ANSIBLE_DB_ENGINES:
            return (f"engine {engine!r} is not supported for Ansible runs "
                    f"(supported: {', '.join(acr.ANSIBLE_DB_ENGINES)}).")
        return {"target_kind": "database", "target_id": item["id"].split(":", 1)[1]}

    return f"{kind!r} resources have no Config-Management path."


def plan_bulk_run(items: list, selected_ids: list) -> dict:
    """Validate a bulk Config-Management selection and resolve each row to a target.

    ``items`` must already be RBAC-filtered for the caller (see :func:`visible_to`) —
    an id that isn't in it is rejected as unknown, which is what stops a client from
    naming a resource it can't see.

    Returns ``{"kind": …, "targets": [{"id", "name", "spec"}, …]}``; raises
    :class:`BulkSelectionError` with an operator-facing message otherwise.

    The load-bearing guard is kind homogeneity. Kinds are not interchangeable at any
    level: a VM run SSHes to a host, while k8s/database runs are localhost plays that
    reach out over a kubeconfig or DB login — different request fields, different
    runner, and a playbook written for one is meaningless against another. Mixing them
    could only ever produce a pile of failed jobs, so it's refused up front.
    """
    ids = list(dict.fromkeys(selected_ids or []))       # de-dupe, keep order
    if not ids:
        raise BulkSelectionError("No resources selected.")
    if len(ids) > MAX_BULK_TARGETS:
        raise BulkSelectionError(
            f"{len(ids)} resources selected; the limit for one bulk run is "
            f"{MAX_BULK_TARGETS}. Narrow the selection with the filters and run again.")

    by_id = {i["id"]: i for i in items}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise BulkSelectionError(
            f"{len(unknown)} selected resource(s) are no longer in your inventory: "
            f"{', '.join(sorted(unknown)[:5])}. Refresh and try again.")

    chosen = [by_id[i] for i in ids]

    kinds = sorted({c.get("kind") for c in chosen})
    if len(kinds) > 1:
        raise BulkSelectionError(
            f"A bulk run targets one kind of resource at a time; this selection mixes "
            f"{' and '.join(kinds)}. They use different connection paths — VMs are "
            f"configured over SSH, Kubernetes clusters and databases by a localhost "
            f"play — so one playbook cannot apply to both.")

    kind = kinds[0]
    if kind not in CONFIG_MANAGEABLE_KINDS:
        raise BulkSelectionError(
            f"{kind!r} resources cannot be a Config-Management target "
            f"(selectable kinds: {', '.join(CONFIG_MANAGEABLE_KINDS)}).")

    targets, problems = [], []
    for item in chosen:
        spec = _target_spec(item)
        if isinstance(spec, str):
            problems.append(f"{item.get('name') or item['id']}: {spec}")
        else:
            targets.append({"id": item["id"], "name": item.get("name") or item["id"],
                            "spec": spec})
    if problems:
        raise BulkSelectionError(
            "These selected resources can't be targeted:\n  - " + "\n  - ".join(problems))

    return {"kind": kind, "targets": targets}


# Run fields that authenticate an SSH CONNECTION. A k8s/database run is a localhost
# play reaching out over a kubeconfig or DB login — it has no SSH connection, and the
# run path silently ignores these (see RunRequest.target_kind). Silent is survivable
# for one run; across a batch it would let an operator believe a credential had been
# applied to every cluster, so a bulk run rejects them outright.
_CONNECTION_FIELDS = ("secret_ssh_key_source", "secret_become_source",
                      "managed_account", "managed_become")


def reject_connection_fields(kind: str, present: dict):
    """Error message when connection-identity fields are set for a non-VM bulk run,
    else ``None``. ``present`` maps field name → the submitted value (any truthy
    value counts as set)."""
    if kind == "vm":
        return None
    offenders = [f for f in _CONNECTION_FIELDS if present.get(f)]
    if not offenders:
        return None
    return (f"{', '.join(offenders)} cannot apply to {kind} targets — they run a "
            f"localhost play that reaches out over a kubeconfig or DB login, with no "
            f"SSH connection to authenticate. Use named secret vars instead.")
