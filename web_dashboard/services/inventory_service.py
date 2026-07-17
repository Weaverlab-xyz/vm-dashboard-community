"""Cross-provider deployment inventory — a normalized, read-only view of every
resource the dashboard has deployed, assembled from its own DB records (no live
cloud calls).

Cloud VMs + on-prem Proxmox/Nutanix VMs come from completed, non-destroyed deploy
Jobs; cloud databases, K8s clusters, and virtual-desktop seats come from their
inventory tables. Each row is normalized to one dict shape. RBAC filtering is the
API layer's job (see :func:`visible_to`), not the collector's.
"""
import logging

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
        "job_id": job.id,
        "detail_href": href,
    }


def _db_item(row) -> dict:
    return {
        "id": f"clouddb:{row.id}",
        "cloud": row.cloud,
        "kind": "database",
        "name": f"{row.engine} {row.instance_id or row.id[:8]}".strip(),
        "region": row.region or "",
        "state": row.status,
        "workgroup": None,
        "deployed_by": row.created_by,
        "created_at": _iso(row.created_at),
        "job_id": None,
        "detail_href": "/databases",
    }


def _k8s_item(row) -> dict:
    return {
        "id": f"k8s:{row.id}",
        "cloud": row.cloud,
        "kind": "k8s",
        "name": row.name,
        "region": row.region or "",
        "state": row.status,
        "workgroup": None,
        "deployed_by": row.created_by,
        "created_at": _iso(row.created_at),
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

    return items


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
