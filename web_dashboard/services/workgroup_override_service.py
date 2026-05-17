"""
Workgroup overrides for VMs the dashboard didn't deploy.

Pre-existing VMs on on-prem hypervisors — and every VM on Hyper-V / vSphere /
XCP-ng, since those providers don't have a deploy flow — have no Job row to
carry a workgroup. Admins assign workgroups to those VMs via the bulk-assign
action on each provider page; the assignment lives here.

`vm_id` is normalized per provider by each *_service module's _override_key()
helper:
  - proxmox  →  "<node>/<vmid>"   (vmid alone isn't unique across a cluster)
  - nutanix  →  vm uuid
  - hyperv   →  vm uuid
  - vsphere  →  managed object reference (moref)
  - xcpng    →  vm uuid
"""
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from ..database import VMWorkgroupOverride, Workgroup

ALLOWED_PROVIDERS = frozenset({"proxmox", "nutanix", "hyperv", "vsphere", "xcpng"})


class OverrideError(ValueError):
    """Raised for validation failures surfaced as 400/404 at the API edge."""


def _check_provider(provider: str) -> str:
    if provider not in ALLOWED_PROVIDERS:
        raise OverrideError(
            f"provider '{provider}' is not allowed. Workgroup overrides only apply to on-prem "
            f"providers: {sorted(ALLOWED_PROVIDERS)}."
        )
    return provider


# ── Queries ───────────────────────────────────────────────────────────────────

def get(db: Session, provider: str, vm_id: str) -> Optional[str]:
    """Return the workgroup name for a single VM, or None if not assigned."""
    _check_provider(provider)
    row = (
        db.query(VMWorkgroupOverride.workgroup)
        .filter(VMWorkgroupOverride.provider == provider, VMWorkgroupOverride.vm_id == vm_id)
        .first()
    )
    return row[0] if row else None


def get_many(db: Session, provider: str, vm_ids: Iterable[str]) -> Dict[str, str]:
    """Return a {vm_id: workgroup} dict for all assigned VMs in the input list."""
    _check_provider(provider)
    ids = list(vm_ids)
    if not ids:
        return {}
    rows = (
        db.query(VMWorkgroupOverride.vm_id, VMWorkgroupOverride.workgroup)
        .filter(VMWorkgroupOverride.provider == provider, VMWorkgroupOverride.vm_id.in_(ids))
        .all()
    )
    return {vm_id: wg for vm_id, wg in rows}


# ── Mutations ─────────────────────────────────────────────────────────────────

def set_many(
    db: Session,
    *,
    provider: str,
    vm_ids: List[str],
    workgroup: str,
    user_username: Optional[str] = None,
) -> int:
    """Upsert overrides for every vm_id in the list. Returns count written."""
    _check_provider(provider)
    if not vm_ids:
        return 0

    canonical = (workgroup or "").strip().lower()
    if not canonical:
        raise OverrideError("workgroup is required.")
    if not db.query(Workgroup.id).filter(Workgroup.name == canonical).first():
        raise OverrideError(f"Unknown workgroup '{workgroup}'.")

    existing = {
        row.vm_id: row
        for row in db.query(VMWorkgroupOverride)
        .filter(VMWorkgroupOverride.provider == provider, VMWorkgroupOverride.vm_id.in_(vm_ids))
        .all()
    }

    now = datetime.utcnow()
    for vm_id in vm_ids:
        row = existing.get(vm_id)
        if row is None:
            db.add(
                VMWorkgroupOverride(
                    provider=provider,
                    vm_id=vm_id,
                    workgroup=canonical,
                    created_by=user_username,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.workgroup = canonical
            row.updated_at = now
    db.commit()
    return len(vm_ids)


def clear_many(db: Session, *, provider: str, vm_ids: List[str]) -> int:
    """Delete overrides for every vm_id in the list. Returns count removed."""
    _check_provider(provider)
    if not vm_ids:
        return 0
    removed = (
        db.query(VMWorkgroupOverride)
        .filter(VMWorkgroupOverride.provider == provider, VMWorkgroupOverride.vm_id.in_(vm_ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed
