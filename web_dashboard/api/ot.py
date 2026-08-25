"""
OT (operational technology) demo endpoints.

Two features, gated behind ``pra_enabled`` at router-include time (see main.py):

* Standalone OT protocol tunnels — a generic-TCP PRA protocol tunnel to ANY
  reachable OT endpoint, with port presets (Modbus/OPC UA/DNP3/S7/EtherNet-IP).
  Each tunnel rides one cloud's shared gateway (``cloud`` on the request).
* The one-click OT demo cell — a queued VM-deploy child (the VM from the
  Packer-baked ``ot-sim`` image; ``gce_deploy`` / ``ec2_deploy`` /
  ``azure_deploy`` per cloud) driven by an ``ot_cell_deploy`` parent job that
  wires the Web Jump + protocol tunnel on top. The endpoints only validate and
  persist; the worker runs everything (services/ot_service.run_cell_deploy).

Deliberately reuses each cloud router's helpers (validation, region resolution)
rather than copying them — the cell's VM is a plain deploy on its cloud and must
stay subject to exactly the same checks and admission policy as one.

Permissions: the per-cloud deploy endpoints use the normal
``require_permission(<cloud>, "write")`` dependency. The endpoints that span
clouds (cells list, tunnels, rewire) authenticate via ``get_current_user`` and
check the TARGET cloud's permission in-handler (``_require_cloud``) — the same
admin / NULL-unrestricted / effective-permissions rules require_permission
applies, just keyed on a request field instead of a fixed scope.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..models.ot import (
    OTCellDeployRequest,
    OTCellDeployRequestAWS,
    OTCellDeployRequestAzure,
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
from ..models.azure import AzureDeployRequest
from ..services import deploy_batch, job_service, ot_service
from ..services.ot_service import OTError
from ..services.terraform_pra_service import TerraformPRAError
from .auth import get_current_user, require_permission
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


def _require_cloud(user: User, cloud: str, level: str) -> str:
    """Validate ``cloud`` and enforce ``<cloud>:<level>`` the way
    require_permission would — admins and NULL-permission (legacy unrestricted)
    users pass, everyone else needs the level in their effective permissions.
    Returns the normalised cloud name."""
    cloud = (cloud or "gcp").strip().lower()
    if cloud not in ot_service.CELL_CHILD_JOB_TYPE:
        raise HTTPException(status_code=400, detail=f"unknown cloud {cloud!r} — "
                            "one of gcp, aws, azure")
    if user.is_effective_admin:
        return cloud
    perms = user.effective_permissions_dict
    if perms and level not in perms.get(cloud, []):
        raise HTTPException(status_code=403,
                            detail=f"Requires '{cloud}:{level}' permission.")
    return cloud


def _tunnel_region(cloud: str) -> str:
    """The region the shared gateway ensure/teardown should target for ``cloud``
    (each cloud's configured default — standalone tunnels have no placement of
    their own)."""
    if cloud == "aws":
        from .aws import _resolve_region as _aws_resolve_region
        return _aws_resolve_region(None)
    if cloud == "azure":
        from .azure import _resolve_location
        return _resolve_location(None)
    return _gcp_region()


# ── Presets ───────────────────────────────────────────────────────────────────

@router.get("/presets", response_model=OTPresetsResponse)
async def list_presets(
    current_user: User = Depends(get_current_user),
):
    """The OT protocol → port preset table (for the tunnel/cell forms). Static
    data, served to any authenticated user — the forms exist on every cloud page
    (same reasoning as /api/pra/pickers)."""
    return OTPresetsResponse(presets=[
        OTPresetInfo(key=key, label=info["label"], port=info["port"])
        for key, info in ot_service.OT_PORT_PRESETS.items()
    ])


# ── Standalone OT protocol tunnels ────────────────────────────────────────────

@router.get("/tunnels", response_model=OTTunnelListResponse)
async def list_tunnels(
    cloud: str = "gcp",
    current_user: User = Depends(get_current_user),
):
    cloud = _require_cloud(current_user, cloud, "read")
    return OTTunnelListResponse(tunnels=[
        OTTunnelInfo(**{k: v for k, v in t.items() if k in OTTunnelInfo.model_fields})
        for t in ot_service.list_standalone_tunnels(cloud)
    ])


@router.post("/tunnels", response_model=OTTunnelResponse)
async def create_tunnel(
    payload: OTTunnelRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Provision a PRA protocol tunnel (tunnel_type=tcp) to an arbitrary OT host.
    Short terraform apply, run in-request like the other small PRA operations."""
    cloud = _require_cloud(current_user, payload.cloud, "write")
    try:
        result = await ot_service.create_standalone_tunnel(
            name=payload.name.strip(),
            hostname=payload.hostname.strip(),
            protocol=payload.protocol,
            remote_port=payload.remote_port,
            local_port=payload.local_port,
            jump_group=payload.jump_group,
            jumpoint_name=payload.jumpoint_name,
            region=_tunnel_region(cloud),
            created_by=current_user.username,
            cloud=cloud,
        )
    except OTError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TerraformPRAError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    job_service.log_audit(
        db, current_user.username, "ot_tunnel_create",
        details={"name": payload.name, "hostname": payload.hostname,
                 "protocol": payload.protocol, "cloud": cloud,
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
    current_user: User = Depends(get_current_user),
):
    # Whose gateway the tunnel rides decides both the permission and which
    # cloud's idle-teardown to run afterwards.
    cloud = _require_cloud(current_user, ot_service.tunnel_cloud(slug), "delete")
    result = await ot_service.delete_standalone_tunnel(slug)
    if result.get("removed"):
        job_service.log_audit(db, current_user.username, "ot_tunnel_delete",
                              details={"slug": slug, "cloud": cloud})
        # The tunnel may have been the last thing holding the shared gateway.
        try:
            from ..services import jumpoint_host_service
            await jumpoint_host_service.teardown_jumpoint_host_if_idle(
                db, cloud, _tunnel_region(cloud))
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort by contract
            logger.warning("OT tunnel delete: idle gateway check failed: %s", exc)
    return result


# ── The OT demo cell ──────────────────────────────────────────────────────────

# NB: each deploy endpoint below creates its ot_cell_deploy parent INLINE, in the
# same function as its queued child — not through a shared helper. That keeps every
# endpoint inside test_worker_dispatch's structural queued-children rule, which
# only sees a parent and its child when both create_job calls sit in one function.

@router.post("/cell", response_model=OTCellDeployResponse)
async def deploy_cell(
    payload: OTCellDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Deploy a GCP OT demo cell: one queued ``gce_deploy`` child (the runner
    claims only ``pending``, so the ``ot_cell_deploy`` parent created below is the
    sole driver) plus the parent that wires the PRA layer once the VM is up."""
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
        details={"instance_name": payload.instance_name, "zone": zone, "cloud": "gcp",
                 "protocol": payload.protocol, "workgroup": workgroup},
    )

    parent = job_service.create_job(
        db,
        job_type="ot_cell_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "cloud":      "gcp",
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


@router.post("/cell/aws", response_model=OTCellDeployResponse)
async def deploy_cell_aws(
    payload: OTCellDeployRequestAWS,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """Deploy an AWS OT demo cell: one queued ``ec2_deploy`` child plus the
    ``ot_cell_deploy`` parent. Same validation and admission gate as the plain
    EC2 deploy. EC2 has no external-IP knob — the subnet decides — so the form
    must point at the private sandbox subnet for the air-gap story to hold."""
    from .aws import (_resolve_region as _aws_resolve_region,
                      _validate_workgroup as _aws_validate_workgroup)
    try:
        ot_service.resolve_ports(payload.protocol, payload.plc_port,
                                 payload.tunnel_local_port)
    except OTError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    region = _aws_resolve_region(payload.region)
    workgroup = _aws_validate_workgroup(db, current_user, payload.workgroup)
    deploy_batch.validate_name(payload.instance_name, "aws")
    deploy_batch.reject_name_collisions(db, "ec2_deploy", [payload.instance_name])

    from ..services import admission_service
    admission_service.enforce(
        "aws:ec2:deploy",
        request={"region": region, "instance_type": payload.instance_type,
                 "image": payload.ami_id, "name": payload.instance_name,
                 "count": 1, "batch": False},
        actor=current_user, db=db,
    )

    # The child's metadata is exactly what aws_vm_service.run() rebuilds an
    # ec2_deploy from — anything omitted is silently skipped at deploy time.
    child = job_service.create_job(
        db,
        job_type="ec2_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        # `queued`, not `pending` — see deploy_cell above.
        status="queued",
        metadata={
            "ami_id":                   payload.ami_id,
            "image_name":               payload.ami_name,
            "instance_name":            payload.instance_name,
            "instance_type":            payload.instance_type,
            "region":                   region,
            "subnet_id":                payload.subnet_id,
            "security_group_ids":       payload.security_group_ids,
            "workgroup":                workgroup,
            "register_in_entitle":      payload.register_in_entitle,
            "register_in_passwordsafe": payload.register_in_passwordsafe,
            "jump_group":               payload.jump_group,
            "jumpoint_name":            payload.jumpoint_name,
            "ot_cell":                  True,
            "ot_params": {
                "protocol":          payload.protocol,
                "plc_port":          payload.plc_port,
                "tunnel_local_port": payload.tunnel_local_port,
                "hmi_port":          payload.hmi_port,
                "jump_group":        payload.jump_group,
                "jumpoint_name":     payload.jumpoint_name,
            },
        },
    )
    job_service.log_audit(
        db, current_user.username, "ot_cell_deploy",
        details={"instance_name": payload.instance_name, "region": region,
                 "cloud": "aws", "protocol": payload.protocol, "workgroup": workgroup},
    )

    parent = job_service.create_job(
        db,
        job_type="ot_cell_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "cloud":     "aws",
            "region":    region,
            "workgroup": workgroup,
            "children":  [{"job_id": child.id,
                           "instance_name": payload.instance_name}],
        },
    )
    return OTCellDeployResponse(
        job_id=parent.id, vm_job_id=child.id, status="pending",
        message=f"Deploying OT cell {payload.instance_name}…",
    )


@router.post("/cell/azure", response_model=OTCellDeployResponse)
async def deploy_cell_azure(
    payload: OTCellDeployRequestAzure,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """Deploy an Azure OT demo cell: one queued ``azure_deploy`` child plus the
    ``ot_cell_deploy`` parent. Same validation and admission gate as the plain
    Azure deploy; the SSH public key is resolved server-side from the configured
    Key Vault (the same key every Azure deploy injects)."""
    from .azure import (_cfg as _azure_cfg, _reject_cross_region_network,
                        _resolve_location, _rg_for,
                        _validate_workgroup as _azure_validate_workgroup)
    from ..services import azure_service
    try:
        ot_service.resolve_ports(payload.protocol, payload.plc_port,
                                 payload.tunnel_local_port)
    except OTError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    loc = _resolve_location(payload.location)
    rg = _rg_for(loc)
    await _reject_cross_region_network(payload.subnet_id, payload.nsg_ids, loc)
    workgroup = _azure_validate_workgroup(db, current_user, payload.workgroup)
    deploy_batch.validate_name(payload.vm_name, "azure")
    deploy_batch.reject_name_collisions(db, "azure_deploy", [payload.vm_name])

    from ..services import admission_service
    admission_service.enforce(
        "azure:vm:deploy",
        request={"region": loc, "instance_type": payload.vm_size,
                 "image": payload.image_id, "name": payload.vm_name,
                 "count": 1, "batch": False},
        actor=current_user, db=db,
    )

    # The Linux deploy path requires a public key at launch; resolve it here the
    # way the deploy modal does (Key Vault, unified → legacy secret), so the OT
    # form doesn't need its own fetch round-trip.
    kv_url = _azure_cfg("azure_key_vault_url")
    if not kv_url:
        raise HTTPException(status_code=400,
                            detail="Key Vault not configured (azure_key_vault_url) — "
                                   "the cell VM needs the standard SSH key injected.")
    try:
        ssh_public_key = await azure_service.resolve_azure_ssh_public_key(
            kv_url, _azure_cfg("azure_ssh_keypair_secret_name"),
            _azure_cfg("azure_ssh_key_secret_name"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400,
                            detail=f"SSH public key unavailable from Key Vault: {exc}")

    # No public IP, ever — access is PRA-brokered only, which is the point.
    child_req = AzureDeployRequest(
        image_id=payload.image_id,
        vm_name=payload.vm_name,
        vm_size=payload.vm_size,
        location=loc,
        resource_group=rg,
        subnet_id=payload.subnet_id,
        nsg_ids=payload.nsg_ids,
        create_public_ip=False,
        os_type="Linux",
        ssh_public_key=ssh_public_key,
        workgroup=workgroup,
        register_in_entitle=payload.register_in_entitle,
        register_in_passwordsafe=payload.register_in_passwordsafe,
        jump_group=payload.jump_group,
        jumpoint_name=payload.jumpoint_name,
        count=1,
    )
    child = job_service.create_job(
        db,
        job_type="azure_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        # `queued`, not `pending` — see deploy_cell above.
        status="queued",
        metadata={
            "image_id":       payload.image_id,
            "image_name":     payload.image_name,
            "vm_name":        payload.vm_name,
            "vm_size":        payload.vm_size,
            "location":       loc,
            "resource_group": rg,
            "subnet_id":      payload.subnet_id,
            "nsg_ids":        payload.nsg_ids,
            "create_public_ip": False,
            "os_type":        "Linux",
            "ssh_username":   child_req.ssh_username,
            "workgroup":      workgroup,
            "ot_cell":        True,
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
    job_service.set_cloud_resource_id(db, child.id, payload.vm_name)
    job_service.log_audit(
        db, current_user.username, "ot_cell_deploy",
        details={"instance_name": payload.vm_name, "location": loc, "cloud": "azure",
                 "protocol": payload.protocol, "workgroup": workgroup},
    )

    parent = job_service.create_job(
        db,
        job_type="ot_cell_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "cloud":          "azure",
            "location":       loc,
            "resource_group": rg,
            "workgroup":      workgroup,
            "children":       [{"job_id": child.id,
                                "instance_name": payload.vm_name}],
        },
    )
    return OTCellDeployResponse(
        job_id=parent.id, vm_job_id=child.id, status="pending",
        message=f"Deploying OT cell {payload.vm_name}…",
    )


@router.post("/cell/{vm_job_id}/rewire")
async def rewire_cell(
    vm_job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run only the missing wiring steps (Web Jump / tunnel / checkout pair) of
    an existing cell — the recovery path a failed ``ot_cell_deploy`` names in its
    error. Works for any cloud's cell; permission is the cell cloud's write."""
    child = job_service.get_job(db, vm_job_id)
    cloud = ot_service.cell_cloud_for_job_type(child.job_type) if child else ""
    if child is None or not cloud or not child.metadata_dict.get("ot_cell"):
        raise HTTPException(status_code=404, detail=f"{vm_job_id} is not an OT cell VM job")
    _require_cloud(current_user, cloud, "write")
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
            "cloud":      cloud,
            "project_id": meta.get("project_id"),
            "zone":       meta.get("zone"),
            "region":     meta.get("region"),
            "location":   meta.get("location"),
        },
    )
    job_service.log_audit(db, current_user.username, "ot_cell_rewire",
                          details={"vm_job_id": vm_job_id, "cloud": cloud})
    return {"job_id": parent.id, "status": "pending",
            "message": "Re-wiring the cell's missing PRA pieces…"}


@router.get("/cells", response_model=OTCellListResponse)
async def list_cells(
    cloud: str = "gcp",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every non-destroyed OT cell on ``cloud`` (its VM-deploy child row is the
    record). Defaults to GCP so pre-multi-cloud callers see what they always saw."""
    cloud = _require_cloud(current_user, cloud, "read")
    job_type = ot_service.CELL_CHILD_JOB_TYPE[cloud]
    accessible = _accessible_workgroups(current_user)
    rows = (db.query(Job)
              .filter(Job.job_type == job_type)
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
            cloud=cloud,
            instance_name=meta.get("instance_name") or meta.get("vm_name") or "",
            # The placement the cloud's destroy endpoint needs alongside the name:
            # GCP zone, AWS region, Azure location.
            zone=meta.get("zone") or meta.get("region") or meta.get("location") or "",
            instance_id=str(meta.get("instance_id") or ""),
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
