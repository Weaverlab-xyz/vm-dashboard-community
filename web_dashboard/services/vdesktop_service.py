"""Virtual-desktop pool lifecycle.

Phase 0 shipped the DB scaffold. **Phase 1 wires Azure** pool provisioning to the
existing VM path: ``create_pool`` fans out to ``azure_service.deploy_vm`` (one
**private** VM per seat, tagged ``POOL_TAG=<pool>``) and fills ``vm_resource_id``;
``scale_pool`` / ``delete_pool`` provision / terminate via ``azure_service``.
AWS / GCP create seat *records* only (not provisioned until their Phase 1).
Phase 2 registers each seat on the PRA Jumpoint (``pra_jump_id``).

Provisioning + teardown are **async** (``deploy_vm`` / ``terminate_vm`` are slow);
the API schedules ``provision_seats`` / ``teardown_seats`` as background tasks.
"""
import logging
import re
import uuid

from sqlalchemy.orm import Session

from ..database import VirtualDesktop

logger = logging.getLogger(__name__)

# Tag stamped on each backing VM so live pool state is recoverable from the cloud.
POOL_TAG = "dashboard:desktop_pool"

VALID_CLOUDS = ("aws", "azure", "gcp")
# Clouds that actually provision VMs in Phase 1 (others create records only).
PROVISIONING_CLOUDS = ("azure",)

_AZURE_REQUIRED = ("location", "resource_group", "subnet_id", "vm_size")


class VDesktopError(Exception):
    pass


def _row_to_dict(row: VirtualDesktop) -> dict:
    return {
        "id":             row.id,
        "cloud":          row.cloud,
        "pool_name":      row.pool_name,
        "kind":           row.kind,
        "vm_resource_id": row.vm_resource_id,
        "status":         row.status,
        "assigned_user":  row.assigned_user,
        "pra_jump_id":    row.pra_jump_id,
        "created_by":     row.created_by,
        "created_at":     row.created_at.isoformat() if row.created_at else "",
    }


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_desktops(db: Session) -> list[dict]:
    rows = db.query(VirtualDesktop).order_by(VirtualDesktop.created_at.desc()).all()
    return [_row_to_dict(r) for r in rows]


def get_seat(db: Session, seat_id: str) -> dict | None:
    row = db.query(VirtualDesktop).filter(VirtualDesktop.id == seat_id).first()
    return _row_to_dict(row) if row else None


def get_pool(db: Session, name: str) -> list[dict]:
    rows = (db.query(VirtualDesktop).filter(VirtualDesktop.pool_name == name)
            .order_by(VirtualDesktop.created_at).all())
    return [_row_to_dict(r) for r in rows]


def list_pools(db: Session) -> list[dict]:
    rows = db.query(VirtualDesktop).all()
    pools: dict[str, dict] = {}
    for r in rows:
        p = pools.setdefault(r.pool_name, {
            "pool_name": r.pool_name, "cloud": r.cloud, "kind": r.kind,
            "count": 0, "statuses": {},
        })
        p["count"] += 1
        p["statuses"][r.status] = p["statuses"].get(r.status, 0) + 1
    return list(pools.values())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_azure_spec(spec: dict) -> dict:
    spec = dict(spec or {})
    missing = [k for k in _AZURE_REQUIRED if not spec.get(k)]
    # Windows seats authenticate with generated per-seat passwords, not SSH keys.
    if (spec.get("os_type") or "Linux").lower() != "windows" and not spec.get("ssh_public_key"):
        missing.append("ssh_public_key")
    if missing:
        raise VDesktopError(f"Azure pool requires: {', '.join(missing)}.")
    has_image = spec.get("image_id") or (
        spec.get("image_publisher") and spec.get("image_offer") and spec.get("image_sku"))
    if not has_image:
        raise VDesktopError("Azure pool requires image_id or a marketplace image (publisher/offer/sku).")
    return spec


def _vm_name_for(pool_name: str, seat_id: str) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", (pool_name or "").lower()).strip("-")[:40] or "desktop"
    return f"{base}-{seat_id[:8]}"


def _parse_vm_id(vm_resource_id: str):
    """rg, name from an Azure VM ARM id (or None, name when unparseable)."""
    parts = (vm_resource_id or "").strip("/").split("/")
    rg = None
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            rg = parts[i + 1]
    return rg, (parts[-1] if parts else None)


def _pool_spec(db: Session, name: str):
    """The Azure spec + job id stored at create-time, for scale-up. (None, None) if absent."""
    from ..database import Job
    jobs = (db.query(Job).filter(Job.job_type == "vdesktop_pool_provision")
            .order_by(Job.created_at.desc()).all())
    for j in jobs:
        md = j.metadata_dict or {}
        if md.get("pool_name") == name and md.get("spec"):
            return md["spec"], j.id
    return None, None


# ── Create / scale / delete (sync DB part; API schedules the async cloud work) ──

def create_pool(db: Session, *, cloud: str, name: str, count: int, created_by: str,
                spec: dict = None) -> dict:
    """Create a pool. For Azure, validate the deploy spec, record it on a
    provision Job (so scale-up can reuse it), and return the seat ids to provision.
    AWS/GCP create pending rows only. The caller schedules ``provision_seats``."""
    name = (name or "").strip()
    if cloud not in VALID_CLOUDS:
        raise VDesktopError(f"Unknown cloud '{cloud}'. Valid: {', '.join(VALID_CLOUDS)}.")
    if not name:
        raise VDesktopError("pool name is required.")
    if count < 1:
        raise VDesktopError("count must be >= 1.")
    if get_pool(db, name):
        raise VDesktopError(f"Pool '{name}' already exists.")

    provision = cloud in PROVISIONING_CLOUDS
    job_id = None
    if provision:
        spec = _validate_azure_spec(spec)
        from . import job_service
        job = job_service.create_job(
            db, job_type="vdesktop_pool_provision", created_by=created_by,
            metadata={"pool_name": name, "cloud": cloud, "count": count, "spec": spec},
        )
        job_id = job.id

    seat_ids = []
    for _ in range(count):
        sid = str(uuid.uuid4())
        db.add(VirtualDesktop(id=sid, cloud=cloud, pool_name=name, kind="vm_pool",
                              status="pending", created_by=created_by))
        seat_ids.append(sid)
    db.commit()
    logger.info("Created desktop pool %s (%s x%d)%s", name, cloud, count,
                " — provisioning" if provision else " — records only")
    return {
        "pool_name": name, "cloud": cloud, "count": count, "seats": get_pool(db, name),
        "job_id": job_id,
        "to_provision": seat_ids if provision else [],
        "spec": spec if provision else None,
    }


def scale_pool(db: Session, name: str, count: int) -> dict:
    """Resize a pool to ``count`` seats. Azure: returns ids to provision (up) or
    tear down (down); the caller schedules the cloud work."""
    if count < 0:
        raise VDesktopError("count must be >= 0.")
    seats = (db.query(VirtualDesktop).filter(VirtualDesktop.pool_name == name)
             .order_by(VirtualDesktop.created_at).all())
    if not seats:
        raise VDesktopError(f"Pool '{name}' not found.")
    cloud = seats[0].cloud
    cur = len(seats)
    out = {"pool_name": name, "count": cur, "to_provision": [], "to_teardown": [], "spec": None}

    if count > cur:
        spec = None
        if cloud in PROVISIONING_CLOUDS:
            spec, _ = _pool_spec(db, name)
            if not spec:
                raise VDesktopError("Pool has no stored Azure spec; cannot scale up.")
        new_ids = []
        for _ in range(count - cur):
            sid = str(uuid.uuid4())
            db.add(VirtualDesktop(id=sid, cloud=cloud, pool_name=name, kind=seats[0].kind,
                                  status="pending", created_by=seats[0].created_by))
            new_ids.append(sid)
        db.commit()
        if cloud in PROVISIONING_CLOUDS:
            out["to_provision"] = new_ids
            out["spec"] = spec
    elif count < cur:
        # Shrink: drop the newest seats. For Azure, terminate them first.
        removable = list(reversed(seats))[: cur - count]
        if cloud in PROVISIONING_CLOUDS:
            for s in removable:
                s.status = "deprovisioning"
            db.commit()
            out["to_teardown"] = [s.id for s in removable]
        else:
            for s in removable:
                db.delete(s)
            db.commit()
    out["count"] = len(get_pool(db, name))
    return out


def delete_pool(db: Session, name: str) -> dict:
    """Delete a pool. Azure: mark seats deprovisioning + return ids to tear down
    (the caller schedules teardown, which terminates the VMs then drops the rows).
    AWS/GCP: drop rows immediately."""
    seats = db.query(VirtualDesktop).filter(VirtualDesktop.pool_name == name).all()
    n = len(seats)
    if n == 0:
        return {"deleted_seats": 0, "to_teardown": []}
    cloud = seats[0].cloud
    if cloud in PROVISIONING_CLOUDS:
        for s in seats:
            s.status = "deprovisioning"
        db.commit()
        return {"deleted_seats": n, "to_teardown": [s.id for s in seats]}
    for s in seats:
        db.delete(s)
    db.commit()
    logger.info("Deleted desktop pool %s (%d records)", name, n)
    return {"deleted_seats": n, "to_teardown": []}


# ── PRA brokering helpers (Phase 2) ─────────────────────────────────────────

def _cfg(key: str, fallback: str = "") -> str:
    from ..config import settings
    from . import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


def _pra_configured() -> bool:
    """True when the PRA API creds are present (mirror cloud_database_service)."""
    return bool(_cfg("bt_api_host") and _cfg("bt_client_id") and _cfg("bt_client_secret"))


def _resolve_pra_targets(spec: dict) -> dict:
    """Jump Group / Jumpoint / Vault account-group for a pool's RDP jumps —
    Azure-specific config wins over the shared defaults (mirrors the deploy path)."""
    jump_group = (spec.get("jump_group") or "").strip() or \
        _cfg("azure_bt_jump_group_name") or _cfg("bt_jump_group_name")
    jumpoint = (spec.get("jumpoint_name") or "").strip() or \
        _cfg("azure_jumpoint_name") or _cfg("bt_jumpoint_name")
    raw_group = str(spec.get("vault_account_group_id")
                    or _cfg("azure_desktops_vault_account_group_id") or "").strip()
    try:
        vault_group_id = int(raw_group) if raw_group else None
    except ValueError:
        vault_group_id = None
    return {"jump_group": jump_group, "jumpoint": jumpoint, "vault_group_id": vault_group_id}


# ── Async cloud work (scheduled by the API as background tasks) ─────────────────

async def provision_seats(pool_name: str, job_id: str, seat_ids: list, spec: dict) -> None:
    """Provision an Azure VM per seat via ``azure_service.deploy_vm`` (private),
    fill ``vm_resource_id`` + ``running``, and tag the VM with ``POOL_TAG``.

    Windows pools get a generated per-seat admin password, vaulted via the
    secrets backend before the seat's VM is created; the (backend, ref) pairs
    are merged into the pool provision job's ``seat_passwords`` map so
    ``GET /api/azure/vms/{name}/admin-password`` can resolve them. The spec
    itself stays credential-free (it is persisted in job metadata for
    scale-up; see ``_pool_spec``)."""
    import asyncio
    from ..database import SessionLocal
    from . import azure_service, job_service
    db = SessionLocal()
    is_windows = (spec.get("os_type") or "Linux").lower() == "windows"
    seat_passwords: dict = {}
    try:
        if job_id:
            job_service.set_running(db, job_id)
        ok = 0
        for sid in seat_ids:
            row = db.query(VirtualDesktop).filter(VirtualDesktop.id == sid).first()
            if row is None:
                continue
            vm_name = _vm_name_for(pool_name, sid)
            try:
                admin_password = ""
                if is_windows:
                    admin_password = azure_service.generate_windows_admin_password()
                    backend, ref = await asyncio.to_thread(
                        azure_service.store_windows_admin_password, vm_name, sid[:8], admin_password,
                    )
                res = await azure_service.deploy_vm(
                    rg=spec["resource_group"], location=spec["location"], vm_name=vm_name,
                    vm_size=spec["vm_size"], image_id=spec.get("image_id", "") or "",
                    subnet_id=spec["subnet_id"], nsg_ids=spec.get("nsg_ids") or [],
                    create_public_ip=bool(spec.get("create_public_ip", False)),
                    ssh_username=spec.get("ssh_username") or "azureuser",
                    ssh_public_key=spec.get("ssh_public_key") or "",
                    image_publisher=spec.get("image_publisher"), image_offer=spec.get("image_offer"),
                    image_sku=spec.get("image_sku"), image_version=spec.get("image_version"),
                    os_type=spec.get("os_type") or "Linux",
                    admin_password=admin_password,
                    trusted_launch=bool(spec.get("trusted_launch")),
                )
                row.vm_resource_id = res.get("vm_id") or vm_name
                row.status = "running"
                db.commit()
                # Phase 2: broker the seat over PRA — register an agentless Remote
                # RDP jump item at the VM's private IP, with a Vault account for
                # credential injection. Best-effort: a running seat with no jump
                # item is debuggable; never fail the seat over brokering.
                private_ip = res.get("private_ip")
                if is_windows and private_ip and not row.pra_jump_id and _pra_configured():
                    try:
                        from . import terraform_pra_service as pra, config_service
                        tgt = _resolve_pra_targets(spec)
                        cred_ref = spec.get("pra_credential_ref")
                        client_secret = config_service.resolve_reference(cred_ref) if cred_ref else ""
                        jump = await pra.provision_rdp_jump(
                            name=vm_name, hostname=private_ip,
                            jump_group_name=tgt["jump_group"], jumpoint_name=tgt["jumpoint"],
                            rdp_username=spec.get("ssh_username") or "azureuser",
                            tag="Azure VDI",
                            admin_password=admin_password,
                            vault_account_name=f"{vm_name}-admin",
                            vault_account_group_id=tgt["vault_group_id"],
                            client_secret=client_secret,
                        )
                        row.pra_jump_id = jump.get("rdp_jump_id") or None
                        row.pra_tunnel_state = jump.get("tf_state_json")
                        db.commit()
                    except Exception as pra_err:
                        logger.warning("desktop seat PRA RDP registration failed pool=%s seat=%s: %s",
                                       pool_name, sid, pra_err)
                if is_windows:
                    seat_passwords[vm_name] = {
                        "backend": backend, "ref": ref,
                        "username": spec.get("ssh_username") or "azureuser",
                    }
                try:
                    await azure_service.set_desktop_pool_tag(spec["resource_group"], vm_name, pool_name)
                except Exception as tag_err:
                    logger.warning("desktop pool tag failed vm=%s: %s", vm_name, tag_err)
                ok += 1
            except Exception as exc:
                row.status = "failed"
                db.commit()
                logger.warning("desktop seat provision failed pool=%s seat=%s: %s", pool_name, sid, exc)
        if seat_passwords:
            # Merge into the pool's provision job (scale-ups run with job_id=None,
            # so fall back to the create-time job that _pool_spec resolves).
            from ..database import Job
            pool_job_id = job_id or _pool_spec(db, pool_name)[1]
            j = db.get(Job, pool_job_id) if pool_job_id else None
            if j is not None:
                md = j.metadata_dict
                merged = dict(md.get("seat_passwords") or {})
                merged.update(seat_passwords)
                md["seat_passwords"] = merged
                j.metadata_dict = md
                db.commit()
        if job_id:
            job_service.set_completed(db, job_id, {"provisioned": ok, "requested": len(seat_ids)})
        logger.info("desktop pool %s provisioned %d/%d seats", pool_name, ok, len(seat_ids))
    finally:
        db.close()


async def teardown_seats(seat_ids: list) -> None:
    """Terminate the Azure VM behind each seat (best-effort) then drop the row."""
    from ..database import SessionLocal
    from . import azure_service
    db = SessionLocal()
    try:
        for sid in seat_ids:
            row = db.query(VirtualDesktop).filter(VirtualDesktop.id == sid).first()
            if row is None:
                continue
            if row.cloud == "azure" and row.vm_resource_id:
                rg, name = _parse_vm_id(row.vm_resource_id)
                if rg and name:
                    try:
                        await azure_service.terminate_vm(rg, name)
                    except Exception as exc:
                        logger.warning("desktop seat terminate failed seat=%s vm=%s: %s", sid, name, exc)
            # Phase 2: remove the seat's PRA RDP jump (+ vault account) — state-driven.
            if row.pra_tunnel_state:
                try:
                    from . import terraform_pra_service as pra
                    await pra.remove_rdp_jump(row.pra_tunnel_state)
                except Exception as exc:
                    logger.warning("desktop seat PRA jump removal failed seat=%s: %s", sid, exc)
            db.delete(row)
            db.commit()
    finally:
        db.close()
