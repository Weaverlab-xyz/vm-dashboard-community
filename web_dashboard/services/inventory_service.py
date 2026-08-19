"""Cross-provider deployment inventory — a normalized, read-only view of every
resource the dashboard has deployed, assembled from its own DB records (no live
cloud calls).

Cloud VMs + on-prem Proxmox/Nutanix VMs come from completed, non-destroyed deploy
Jobs; cloud databases, K8s clusters, and virtual-desktop seats come from their
inventory tables; and every VM a remote agent has synced comes from the hypervisor
cache, whether the dashboard deployed it or not. Each row is normalized to one dict
shape. RBAC filtering is the API layer's job (see :func:`visible_to`), not the
collector's.

Note what the hypervisor source can and cannot see: ``hypervisor_vm_cache`` is written
only by agent-brokered syncs, so a directly-dialled Proxmox or vSphere connection
contributes nothing here. "Every hypervisor kind" means every AGENT-BOUND connection.
"""
import json
import logging
from typing import Optional, Set

from sqlalchemy.orm import Session

from ..database import (CloudDatabase, HypervisorConnection,
                        HypervisorVMCache, Job, K8sCluster, VirtualDesktop)
from . import expiry_policy, hypervisor_view_service

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
        # Which agent can reach this database, for a Config-Management run. Only ever set on
        # a cloud='local' registered row — an on-prem database the dashboard has a reference
        # to but no route to. NULL keeps the pre-existing behaviour (the dashboard's own
        # runner), which is the only option a provisioned cloud database has.
        "agent_id": getattr(row, "agent_id", None),
        # The endpoint an agent-executed localhost play reaches out to. Not shown in the UI;
        # `_target_spec` needs it because an agent job carries the address, not a row id it
        # could look up.
        "private_host": getattr(row, "private_host", "") or "",
        "port": getattr(row, "port", None),
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


# Hypervisor kind -> the page a synced VM links to. Workstation has no page of its own;
# its rows are the /vms page.
_HV_PAGES = {"proxmox": "/proxmox", "nutanix": "/nutanix", "vsphere": "/vsphere",
             "esxi": "/vsphere", "xcpng": "/xcpng", "hyperv": "/hyperv",
             "workstation": "/vms"}

# Kinds whose `scope` is a PLACE worth filtering on — a Proxmox node, a Nutanix cluster.
# Elsewhere `scope` is either empty (vsphere/xcpng/hyperv report none) or PER-VM: for
# workstation it is a VMX path on somebody's desktop, and one Region entry per row is not
# a filter.
_SCOPE_IS_A_REGION = ("proxmox", "nutanix")

# deploy job_type -> (hypervisor kind, the metadata key holding the created VM's own id).
# `set_completed` merges a deploy's result into the job's metadata and collect() only
# sees completed jobs, so these ids are present and the dedup below can be exact.
_HV_DEPLOY_JOBS = {"proxmox_deploy": ("proxmox", "vmid"),
                   "nutanix_deploy": ("nutanix", "uuid")}


def _hv_override_key(kind: str, vm_id: str, scope: str) -> str:
    """The key each provider's own ``_override_key`` builds.

    Must match api/{proxmox,nutanix,hyperv,vsphere,xcpng}.py EXACTLY, or an admin's
    assignment on those pages silently stops applying here. Proxmox is the only composite
    one — a vmid is not unique across a cluster, so the node is in the key.
    """
    if kind == "proxmox":
        return f"{scope or ''}/{vm_id}"
    return str(vm_id or "")


def _job_match_keys(job) -> set:
    """Identity keys a completed hypervisor deploy Job lays claim to.

    Two, and either matching means "same VM". The exact one holds because
    ``job_service.set_completed`` merges a deploy's result into the job metadata, so a
    completed proxmox_deploy carries its vmid and a nutanix_deploy its uuid. The
    casefolded name is the fallback for a job predating that merge, and is deliberately
    the same join api/proxmox.py and api/nutanix.py already use to inherit a deploy-time
    workgroup — so this page and those cannot disagree about what one VM is.
    """
    spec = _HV_DEPLOY_JOBS.get(job.job_type)
    if spec is None:
        return set()
    kind, id_key = spec
    meta = job.metadata_dict or {}
    keys = set()
    conn_id = str(meta.get("connection_id") or "")
    vm_id = str(meta.get(id_key) or "")
    if conn_id and vm_id:
        # str() on both sides: Proxmox stores an int vmid in metadata and the agent
        # reports a string.
        keys.add(("hvid", conn_id, vm_id))
    name = str(meta.get("vm_name") or meta.get("name") or "").strip().casefold()
    if name:
        scope = str(meta.get("node") or "").casefold() if kind == "proxmox" else ""
        keys.add(("name", kind, scope, name))
    return keys


def _hv_match_keys(conn, row) -> set:
    """The same identity keys, from the cache side."""
    keys = {("hvid", conn.id, str(row.vm_id or ""))}
    name = (row.name or "").strip().casefold()
    if name:
        scope = (row.scope or "").casefold() if conn.kind == "proxmox" else ""
        keys.add(("name", conn.kind, scope, name))
    return keys


def _hv_item(conn, row, workgroup: Optional[str], ips: list) -> dict:
    kind = (conn.kind or "").lower()
    if kind in _SCOPE_IS_A_REGION:
        region = (row.scope or "") or (conn.site or "")
    else:
        region = conn.site or ""
    return {
        "id": f"hv:{conn.id}:{row.vm_id}",
        # The hypervisor kind, not a generic "onprem": a Proxmox VM the dashboard
        # DEPLOYED already reports cloud "proxmox", and a second label would list one
        # hypervisor under two Provider values in the same dropdown.
        "cloud": kind,
        "kind": "vm",
        "name": row.name or row.vm_id,
        "region": region,
        # Normalised through the one function that knows all six products' spellings, so
        # a capitalisation change upstream cannot render every VM stopped. Neither value
        # is in expiry_policy's reapable-state set, which is a free extra safety layer.
        "state": "running" if hypervisor_view_service.is_running(
            kind, row.power_state) else "stopped",
        "workgroup": (workgroup or "").lower() or None,
        # Load-bearing, not an oversight. `visible_to` falls back to comparing
        # `deployed_by` against the caller for a row with no workgroup, and None can
        # never match — which makes an untagged synced VM admin-only, exactly the rule
        # every hypervisor page already keeps.
        "deployed_by": None,
        # The cache knows when it was SYNCED, which is not when the VM was created.
        "created_at": None,
        "expires_at": None,
        "source": expiry_policy.SYNCED_HYPERVISOR_SOURCE,
        "job_id": None,
        "ip": ips[0] if ips else "",
        "detail_href": _HV_PAGES.get(kind, "/connections"),
        # Which agent, if any, can reach this VM — and which connection it was synced from.
        # `_target_spec` needs both: a VM behind an agent-bound connection is on a network
        # the dashboard has no route to, so a Config-Management run against it has to be
        # queued FOR that agent rather than for the local runner. `agent_id` is None for a
        # dashboard-direct connection, which is what keeps the existing behaviour.
        # getattr, like _db_item's: these projections are exercised against plain
        # stand-ins as well as ORM rows, and a row from a build before the column
        # existed is the same shape.
        "agent_id": getattr(conn, "agent_id", None),
        "connection_id": conn.id,
        # The guest's OS as the hypervisor reported it, used only to choose SSH or WinRM.
        # Absent on a connection that does not sync guest details, and the run form's
        # transport picker is what covers that.
        "guest_os": getattr(row, "guest_os", "") or "",
    }


def _hypervisor_items(db: Session, claimed: set) -> list:
    """Synced hypervisor VMs, minus any a deploy Job already accounts for.

    One row query plus one bulk override lookup per kind present — at most six, whatever
    the size of the estate.

    Only ACTIVE connections: a sync only ever touches those, and ``_prune`` only removes
    rows a pass touched, so deactivating a connection freezes its cache rather than
    emptying it. Listing frozen rows as current inventory would be the lie.
    """
    from . import workgroup_override_service as wos

    rows = (db.query(HypervisorVMCache, HypervisorConnection)
            .join(HypervisorConnection,
                  HypervisorConnection.id == HypervisorVMCache.connection_id)
            .filter(HypervisorConnection.is_active.is_(True))
            .all())
    if not rows:
        return []

    by_kind: dict = {}
    for row, conn in rows:
        by_kind.setdefault((conn.kind or "").lower(), []).append((row, conn))

    items = []
    for kind, pairs in by_kind.items():
        if kind not in wos.ALLOWED_PROVIDERS:
            # `esxi` is a valid agent kind but not a connection kind, and get_many raises
            # on an unknown provider. Skip rather than take the whole page down.
            continue
        keys = [_hv_override_key(kind, row.vm_id, row.scope) for row, _ in pairs]
        try:
            overrides = wos.get_many(db, kind, keys)
        except Exception:  # noqa: BLE001
            logger.warning("could not read %s workgroup overrides for the inventory", kind)
            overrides = {}
        for row, conn in pairs:
            if _hv_match_keys(conn, row) & claimed:
                continue      # a deploy Job already lists this VM, with its timer
            try:
                ips = json.loads(row.ip_addresses or "[]")
            except (TypeError, ValueError):
                ips = []
            key = _hv_override_key(kind, row.vm_id, row.scope)
            items.append(_hv_item(conn, row, overrides.get(key), ips))
    return items


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
    # Identity keys the deploy Jobs claim, so the hypervisor cache does not list the same
    # VM twice. The Job row wins: it carries the auto-delete timer, the deploy-time
    # workgroup and the job link, none of which the cache has. Collected after the
    # `destroyed` guard, so a VM the dashboard destroyed but the agent has not re-synced
    # yet still shows up from the cache — which is the honest reading of that state.
    claimed: set = set()
    for job in vm_jobs:
        if job.metadata_dict.get("destroyed"):
            continue
        claimed |= _job_match_keys(job)
        items.append(_vm_item(job))

    for row in (db.query(CloudDatabase)
                .filter(CloudDatabase.status.notin_(("deleted", "decommissioned"))).all()):
        items.append(_db_item(row))

    for row in db.query(K8sCluster).filter(K8sCluster.status != "deleted").all():
        items.append(_k8s_item(row))

    for row in (db.query(VirtualDesktop)
                .filter(VirtualDesktop.status.notin_(("deprovisioning", "deleted"))).all()):
        items.append(_desktop_item(row))

    items.extend(_hypervisor_items(db, claimed))

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
    set; an item without a workgroup (database / k8s / desktop, or a synced hypervisor
    VM) is visible only to the user who created it.

    A synced hypervisor VM has no creator, so that last clause makes it admin-only until
    an admin assigns a workgroup override — deliberately, and the same rule every
    hypervisor page keeps: an agent can report any VM it likes, and none of them widen
    what a non-admin sees until someone tags them."""
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

# On-prem kinds that api/config_mgmt.py exposes a GROUP target for. `workstation` is
# absent there: it has no group target, so a Workstation VM with no address has nowhere
# to be pointed at all, and the reason string has to say so rather than suggest one.
_GROUP_TARGET_KINDS = ("proxmox", "vsphere", "hyperv", "nutanix", "xcpng")

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
            if item.get("source") == expiry_policy.SYNCED_HYPERVISOR_SOURCE:
                # "its deploy job stored none" would be a lie: this row has no deploy job.
                # Three separate things have to be true before a synced VM has an address,
                # and the reason has to name whichever one is missing rather than send the
                # operator to look at the other two.
                if item.get("agent_id"):
                    return (f"the {cloud or 'hypervisor'} inventory sync reports no address "
                            f"for this VM. An address needs all three of: the guest powered "
                            f"on, guest tools/Integration Services installed in it, and "
                            f"`sync_guest_details: true` on this connection in the agent's "
                            f"connections.yaml. Set that, Sync Now, and it becomes "
                            f"selectable.")
                tail = (f"Configure it through the {cloud} group target on the Config "
                        f"Management page."
                        if cloud in _GROUP_TARGET_KINDS else
                        "There is no group target for it either — power it on and "
                        "re-sync, or target it by IP from the Config Management page.")
                return (f"the {cloud or 'hypervisor'} inventory sync reports no address "
                        f"for this VM (only a powered-on guest with tools installed "
                        f"reports one). {tail}")
            return ("no recorded IP address — its deploy job stored none. Proxmox and "
                    "Nutanix VMs are configured through their hypervisor group target "
                    "on the Config Management page, not selected individually.")
        # A VM behind an agent-bound connection is on a network the dashboard has no route
        # to, so the run has to be queued FOR that agent — the local runner would resolve
        # the address and then time out. This is the one case where an address alone is not
        # enough to aim a run.
        if item.get("agent_id"):
            from . import agent_ansible_meta
            return {"agent_id": item["agent_id"],
                    "connection_id": item.get("connection_id") or "",
                    "target_id": (item.get("id") or "").split(":")[-1],
                    "target": ip,
                    "transport": agent_ansible_meta.transport_for_guest_os(
                        item.get("guest_os")),
                    "cloud": ""}
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
        # An on-prem database bound to an agent runs its localhost play ON THAT AGENT. The
        # dashboard-local runner is the only other option for cloud='local', and it does not
        # exist on a cloud-hosted dashboard — no Docker socket, and no route to the LAN.
        if item.get("agent_id"):
            if not item.get("private_host"):
                return ("this database has no endpoint recorded, so an agent has nothing "
                        "to connect to. Re-register it with its host.")
            return {"target_kind": "database", "target_id": item["id"].split(":", 1)[1],
                    "agent_id": item["agent_id"], "target": item["private_host"],
                    "port": item.get("port") or 0, "transport": "local"}
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
