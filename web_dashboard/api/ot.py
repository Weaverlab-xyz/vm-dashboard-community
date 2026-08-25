"""
OT (operational technology) demo endpoints.

Two features, both GCP-scoped in slice 1 and gated behind ``pra_enabled`` at
router-include time (see main.py):

* Standalone OT protocol tunnels — a generic-TCP PRA protocol tunnel to ANY
  reachable OT endpoint, with port presets (Modbus/OPC UA/DNP3/S7/EtherNet-IP).
* The one-click OT demo cell — a queued ``gce_deploy`` child (the VM from the
  Packer-baked ``ot-sim`` image) driven by an ``ot_cell_deploy`` parent job that
  wires the Web Jump + protocol tunnel on top. The endpoint only validates and
  persists; the worker runs everything (services/ot_service.run_cell_deploy).

Deliberately reuses the GCP router's helpers (validation, zone/region resolution)
rather than copying them — the cell's VM is a plain GCE deploy and must stay
subject to exactly the same checks and admission policy as one.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..models.ot import (
    OTCellDeployRequest,
    OTCellDeployResponse,
    OTCellInfo,
    OTCellListResponse,
    OTPresetInfo,
    OTPresetsResponse,
    OTTunnelInfo,
    OTTunnelListResponse,
    OTTunnelRequest,
    OTTunnelResponse,
)
from ..models.gcp import GCPDeployRequest
from ..services import deploy_batch, job_service, ot_service
from ..services.ot_service import OTError
from ..services.terraform_pra_service import TerraformPRAError
from .auth import require_permission
from .gcp import (
    _accessible_workgroups,
    _gcp_project,
    _gcp_region,
    _region_from_zone,
    _reject_cross_region_subnetwork,
    _resolve_zone,
    _validate_workgroup,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ot", tags=["ot"])


# ── Presets ───────────────────────────────────────────────────────────────────

@router.get("/presets", response_model=OTPresetsResponse)
async def list_presets(
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """The OT protocol → port preset table (for the tunnel/cell forms)."""
    return OTPresetsResponse(presets=[
        OTPresetInfo(key=key, label=info["label"], port=info["port"])
        for key, info in ot_service.OT_PORT_PRESETS.items()
    ])


# ── Standalone OT protocol tunnels ────────────────────────────────────────────

@router.get("/tunnels", response_model=OTTunnelListResponse)
async def list_tunnels(
    current_user: User = Depends(require_permission("gcp", "read")),
):
    return OTTunnelListResponse(tunnels=[
        OTTunnelInfo(**{k: v for k, v in t.items() if k in OTTunnelInfo.model_fields})
        for t in ot_service.list_standalone_tunnels()
    ])


@router.post("/tunnels", response_model=OTTunnelResponse)
async def create_tunnel(
    payload: OTTunnelRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Provision a PRA protocol tunnel (tunnel_type=tcp) to an arbitrary OT host.
    Short terraform apply, run in-request like the other small PRA operations."""
    try:
        result = await ot_service.create_standalone_tunnel(
            name=payload.name.strip(),
            hostname=payload.hostname.strip(),
            protocol=payload.protocol,
            remote_port=payload.remote_port,
            local_port=payload.local_port,
            jump_group=payload.jump_group,
            jumpoint_name=payload.jumpoint_name,
            region=_gcp_region(),
            created_by=current_user.username,
        )
    except OTError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TerraformPRAError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    job_service.log_audit(
        db, current_user.username, "ot_tunnel_create",
        details={"name": payload.name, "hostname": payload.hostname,
                 "protocol": payload.protocol,
                 "remote_port": result["remote_port"]},
    )
    return OTTunnelResponse(
        slug=result["slug"],
        tunnel_jump_id=result["tunnel_jump_id"],
        local_port=result["local_port"],
        remote_port=result["remote_port"],
        message=(f"Tunnel created. In the PRA representative console, start the "
                 f"protocol tunnel session and point your client at "
                 f"127.0.0.1:{result['local_port']}."),
    )


@router.delete("/tunnels/{slug}")
async def delete_tunnel(
    slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "delete")),
):
    result = await ot_service.delete_standalone_tunnel(slug)
    if result.get("removed"):
        job_service.log_audit(db, current_user.username, "ot_tunnel_delete",
                              details={"slug": slug})
        # The tunnel may have been the last thing holding the shared gateway.
        try:
            from ..services import jumpoint_host_service
            await jumpoint_host_service.teardown_jumpoint_host_if_idle(
                db, "gcp", _gcp_region())
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort by contract
            logger.warning("OT tunnel delete: idle gateway check failed: %s", exc)
    return result


# ── The OT demo cell ──────────────────────────────────────────────────────────

@router.post("/cell", response_model=OTCellDeployResponse)
async def deploy_cell(
    payload: OTCellDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Deploy an OT demo cell: one queued ``gce_deploy`` child (the runner claims
    only ``pending``, so the ``ot_cell_deploy`` parent created below is the sole
    driver) plus the parent that wires the PRA layer once the VM is up."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400,
                            detail="GCP project ID not configured — run the setup wizard.")
    try:
        ot_service.resolve_ports(payload.protocol, payload.plc_port,
                                 payload.tunnel_local_port)
    except OTError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    zone = _resolve_zone(payload.zone)
    region = _region_from_zone(zone)
    _reject_cross_region_subnetwork(payload.subnetwork, zone, region)
    workgroup = _validate_workgroup(db, current_user, payload.workgroup)
    deploy_batch.validate_name(payload.instance_name, "gcp")
    deploy_batch.reject_name_collisions(db, "gce_deploy", [payload.instance_name])

    # Same action key as the plain GCE deploy, so every existing guardrail
    # (regions, size caps, change windows) covers the cell's VM unchanged.
    from ..services import admission_service
    admission_service.enforce(
        "gcp:gce:deploy",
        request={"region": region, "zone": zone,
                 "instance_type": payload.machine_type,
                 "image": payload.image_self_link,
                 "name": payload.instance_name, "count": 1, "batch": False},
        actor=current_user, db=db,
    )

    # The cell lives in the egress-less private subnet — that IS the plant-network
    # story — so never give it an external IP; access is PRA-brokered only.
    tags = list(dict.fromkeys((payload.network_tags or []) + ["ot-sim"]))
    child_req = GCPDeployRequest(
        image_self_link=payload.image_self_link,
        image_name=payload.image_name,
        instance_name=payload.instance_name,
        machine_type=payload.machine_type,
        zone=zone,
        subnetwork=payload.subnetwork,
        create_external_ip=False,
        disk_size_gb=payload.disk_size_gb,
        network_tags=tags,
        workgroup=workgroup,
        register_in_entitle=payload.register_in_entitle,
        register_in_passwordsafe=payload.register_in_passwordsafe,
        jump_group=payload.jump_group,
        jumpoint_name=payload.jumpoint_name,
        count=1,
    )
    child = job_service.create_job(
        db,
        job_type="gce_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        # `queued`, not `pending`: the runner claims on status='pending', so a
        # pending child would be deployed a second time alongside its parent.
        status="queued",
        metadata={
            "project_id":      project_id,
            "zone":            zone,
            "region":          region,
            "instance_name":   payload.instance_name,
            "machine_type":    payload.machine_type,
            "image_self_link": payload.image_self_link,
            "image_name":      payload.image_name,
            "workgroup":       workgroup,
            "ot_cell":         True,
            "ot_params": {
                "protocol":          payload.protocol,
                "plc_port":          payload.plc_port,
                "tunnel_local_port": payload.tunnel_local_port,
                "hmi_port":          payload.hmi_port,
                "jump_group":        payload.jump_group,
                "jumpoint_name":     payload.jumpoint_name,
            },
            "req": child_req.model_dump(),
        },
    )
    job_service.set_cloud_resource_id(db, child.id, payload.instance_name)
    job_service.log_audit(
        db, current_user.username, "ot_cell_deploy",
        details={"instance_name": payload.instance_name, "zone": zone,
                 "protocol": payload.protocol, "workgroup": workgroup},
    )

    parent = job_service.create_job(
        db,
        job_type="ot_cell_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "project_id": project_id,
            "zone":       zone,
            "region":     region,
            "workgroup":  workgroup,
            "children":   [{"job_id": child.id,
                            "instance_name": payload.instance_name}],
        },
    )
    return OTCellDeployResponse(
        job_id=parent.id, vm_job_id=child.id, status="pending",
        message=f"Deploying OT cell {payload.instance_name}…",
    )


@router.post("/cell/{vm_job_id}/rewire")
async def rewire_cell(
    vm_job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Re-run only the missing wiring steps (Web Jump / tunnel) of an existing
    cell — the recovery path a failed ``ot_cell_deploy`` names in its error."""
    child = job_service.get_job(db, vm_job_id)
    if child is None or child.job_type != "gce_deploy" \
            or not child.metadata_dict.get("ot_cell"):
        raise HTTPException(status_code=404, detail=f"{vm_job_id} is not an OT cell VM job")
    if child.metadata_dict.get("destroyed"):
        raise HTTPException(status_code=400, detail="This cell has been destroyed.")
    if child.status != "completed":
        raise HTTPException(status_code=400,
                            detail=f"The cell's VM job is {child.status} — re-wire "
                                   "applies only to a deployed cell.")
    meta = child.metadata_dict
    parent = job_service.create_job(
        db,
        job_type="ot_cell_deploy",
        created_by=current_user.username,
        workgroup=child.workgroup,
        metadata={
            "rewire_child_job_id": vm_job_id,
            "project_id": meta.get("project_id"),
            "zone":       meta.get("zone"),
            "region":     meta.get("region"),
        },
    )
    job_service.log_audit(db, current_user.username, "ot_cell_rewire",
                          details={"vm_job_id": vm_job_id})
    return {"job_id": parent.id, "status": "pending",
            "message": "Re-wiring the cell's missing PRA pieces…"}


@router.get("/cells", response_model=OTCellListResponse)
async def list_cells(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """Every non-destroyed OT cell (its ``gce_deploy`` child row is the record)."""
    accessible = _accessible_workgroups(current_user)
    rows = (db.query(Job)
              .filter(Job.job_type == "gce_deploy")
              .order_by(Job.created_at.desc())
              .all())
    cells = []
    for row in rows:
        meta = row.metadata_dict
        if not meta.get("ot_cell") or meta.get("destroyed"):
            continue
        if row.status == "cancelled":
            continue
        if accessible is not None and (row.workgroup or "").lower() not in accessible:
            continue
        # The PRA-checkout pair counts toward "wired" only when it applies to this
        # cell (skip_reason "") — so a pre-feature cell with Password Safe onboarding
        # shows "wiring incomplete" and its Re-wire button retrofits exactly the
        # missing checkout pieces, while a cell without PS onboarding stays green.
        checkout_pending = (not ot_service.ps_checkout_skip_reason(meta)
                            and not meta.get("ot_ps_synced"))
        cells.append(OTCellInfo(
            vm_job_id=row.id,
            instance_name=meta.get("instance_name") or "",
            zone=meta.get("zone") or "",
            status=row.status,
            private_ip=meta.get("private_ip"),
            hmi_url=meta.get("ot_hmi_url") or "",
            web_jump_id=meta.get("ot_web_jump_id") or "",
            tunnel_jump_id=meta.get("ot_tunnel_jump_id") or "",
            tunnel_protocol=meta.get("ot_tunnel_protocol") or "",
            tunnel_local_port=int(meta.get("ot_tunnel_local_port") or 0),
            tunnel_remote_port=int(meta.get("ot_tunnel_remote_port") or 0),
            shell_jump_id=str(meta.get("bt_shell_jump_id") or ""),
            vault_account_id=str(meta.get("ot_vault_account_id") or ""),
            vault_account_name=meta.get("ot_vault_account_name") or "",
            ps_checkout_synced=bool(meta.get("ot_ps_synced")),
            workgroup=row.workgroup or "",
            expires_at=row.expires_at.isoformat() if row.expires_at else None,
            wiring_complete=bool(meta.get("ot_web_jump_tf_state")
                                 and meta.get("ot_tunnel_tf_state")
                                 and not checkout_pending),
        ))
    return OTCellListResponse(cells=cells)
