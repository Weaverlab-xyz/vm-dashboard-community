"""Network demo cell endpoints (GCP), gated behind ``pra_enabled`` at router-include
time (see main.py).

A cell is a VyOS router/firewall deployed down the ordinary ``gce_deploy`` path. There
is no parent orchestration job: unlike the OT cell there is nothing to wire once the VM
is up, because a firewall is reached over SSH and the plain deploy already provisions
the Shell Jump, the Password Safe onboarding, the gateway reference, the expiry stamp
and the inventory row. ``services/netcell_service`` argues that at length.

What this module does is therefore small and entirely about refusing bad requests
before anything launches -- the image, the release train, the PRA preflight and the
Password Safe platform -- plus stamping the marker the tab and the tile read.

Deliberately reuses ``api/gcp``'s helpers rather than copying them: the cell's VM is a
plain GCE deploy and must stay subject to the same name validation, zone resolution,
subnetwork checks, workgroup scoping and admission policy as one.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..models.gcp import GCPDeployRequest
from ..models.netcell import (
    NetCellDeployRequest,
    NetCellDeployResponse,
    NetCellInfo,
    NetCellListResponse,
)
from ..services import deploy_batch, job_service, netcell_service
from .auth import get_current_user, require_permission
from .gcp import (
    _accessible_workgroups,
    _gcp_project,
    _region_from_zone,
    _reject_cross_region_subnetwork,
    _resolve_zone,
    _validate_workgroup,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/netcell", tags=["netcell"])


async def _functional_account_platform() -> str:
    """The configured GCP functional account's platform name, or "" if it cannot be
    resolved.

    Best-effort on purpose. A Password Safe outage must not become a refusal to deploy,
    so every failure here answers "" and ``ps_platform_problem`` treats that as fine --
    the same posture ``ps_vm_hook._platform_name_ok`` takes on a blank name.
    """
    try:
        from ..services import ps_api_service, ps_vm_hook
        if not ps_vm_hook.registration_enabled():
            return ""
        name = ps_vm_hook._functional_account_name("gcp")
        if not name:
            return ""
        fa = await ps_api_service.get_functional_account(name)
        return str((fa or {}).get("platform_name") or "")
    except Exception as exc:  # noqa: BLE001
        logger.info("netcell: could not resolve the Password Safe platform (%s)", exc)
        return ""


@router.post("/cell", response_model=NetCellDeployResponse)
async def deploy_cell(
    payload: NetCellDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Deploy a GCP network cell: one ordinary ``gce_deploy`` job carrying the netcell
    marker and a forced Password Safe onboarding method.

    ``pending``, not ``queued`` — the runner claims pending work, and here there is no
    parent to drive the child, which is exactly the simplification this feature buys.
    """
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400,
                            detail="GCP project ID not configured — run the setup wizard.")

    for problem in (netcell_service.image_problem(payload.image_name, payload.image_self_link),
                    netcell_service.release_problem(payload.vyos_release),
                    netcell_service.pra_preflight_problem()):
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    if payload.register_in_passwordsafe:
        platform_problem = netcell_service.ps_platform_problem(
            await _functional_account_platform())
        if platform_problem:
            raise HTTPException(status_code=400, detail=platform_problem)

    zone = _resolve_zone(payload.zone)
    region = _region_from_zone(zone)
    _reject_cross_region_subnetwork(payload.subnetwork, zone, region)
    workgroup = _validate_workgroup(db, current_user, payload.workgroup)
    deploy_batch.validate_name(payload.instance_name, "gcp")
    deploy_batch.reject_name_collisions(db, "gce_deploy", [payload.instance_name])

    # Same action key as the plain GCE deploy, so every existing guardrail — allowed
    # regions, size caps, change windows — covers the cell's VM unchanged.
    from ..services import admission_service
    admission_service.enforce(
        "gcp:gce:deploy",
        request={"region": region, "zone": zone,
                 "instance_type": payload.machine_type,
                 "image": payload.image_self_link,
                 "name": payload.instance_name, "count": 1, "batch": False},
        actor=current_user, db=db,
    )

    tags = list(dict.fromkeys((payload.network_tags or [])
                              + [netcell_service.NETCELL_NETWORK_TAG]))
    req = GCPDeployRequest(
        image_self_link=payload.image_self_link,
        image_name=payload.image_name,
        instance_name=payload.instance_name,
        machine_type=payload.machine_type,
        zone=zone,
        subnetwork=payload.subnetwork,
        # No external IP, ever. The point of the demo is that the device carries no
        # inbound rule and is still reachable — through the Gateway, and only there.
        create_external_ip=False,
        disk_size_gb=payload.disk_size_gb,
        network_tags=tags,
        workgroup=workgroup,
        # A router is not an Entitle SSH target: Entitle's ephemeral accounts are
        # created with useradd on the guest, which VyOS does not manage that way.
        register_in_entitle=False,
        register_in_passwordsafe=payload.register_in_passwordsafe,
        passwordsafe_method=netcell_service.NETCELL_PS_METHOD,
        jump_group=payload.jump_group,
        jumpoint_name=payload.jumpoint_name,
        count=1,
    )

    job = job_service.create_job(
        db,
        job_type="gce_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "project_id":      project_id,
            "zone":            zone,
            "region":          region,
            "instance_name":   payload.instance_name,
            "machine_type":    payload.machine_type,
            "image_self_link": payload.image_self_link,
            "image_name":      payload.image_name,
            "workgroup":       workgroup,
            "netcell":         True,
            "netcell_params": {
                "vyos_release": payload.vyos_release,
                "ruleset":      payload.ruleset,
                "jump_group":   payload.jump_group,
                "jumpoint_name": payload.jumpoint_name,
            },
            # Read by aws_vm_service off metadata on its own path; carried here too so
            # the row says what it asked for without anyone parsing `req`.
            "passwordsafe_method": netcell_service.NETCELL_PS_METHOD,
            "req": req.model_dump(),
        },
    )
    job_service.set_cloud_resource_id(db, job.id, payload.instance_name)
    job_service.log_audit(
        db, current_user.username, "netcell_deploy",
        details={"instance_name": payload.instance_name, "zone": zone,
                 "vyos_release": payload.vyos_release, "workgroup": workgroup},
    )
    return NetCellDeployResponse(
        job_id=job.id, status="pending",
        message=f"Deploying network cell {payload.instance_name}…",
        passwordsafe_method=(netcell_service.NETCELL_PS_METHOD
                             if payload.register_in_passwordsafe else ""),
        notes=netcell_service.deploy_notes(payload.register_in_passwordsafe),
    )


@router.get("/cells", response_model=NetCellListResponse)
def list_cells(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every non-destroyed network cell. The deploy row IS the cell — there is no
    separate record to reconcile it against, which is the other half of not having a
    parent job."""
    accessible = _accessible_workgroups(current_user)
    rows = (db.query(Job)
              .filter(Job.job_type == "gce_deploy")
              .order_by(Job.created_at.desc())
              .all())
    cells = []
    for row in rows:
        meta = row.metadata_dict
        if not netcell_service.is_cell(meta) or meta.get("destroyed"):
            continue
        if row.status == "cancelled":
            continue
        if accessible is not None and (row.workgroup or "").lower() not in accessible:
            continue
        params = netcell_service.cell_params(meta)
        cells.append(NetCellInfo(
            job_id=row.id,
            instance_name=meta.get("instance_name") or "",
            zone=meta.get("zone") or "",
            region=meta.get("region") or "",
            machine_type=meta.get("machine_type") or "",
            status=row.status,
            created_by=row.created_by or "",
            created_at=row.created_at.isoformat() if row.created_at else "",
            workgroup=row.workgroup or "",
            vyos_release=str(params.get("vyos_release") or ""),
            ruleset=str(params.get("ruleset") or ""),
            private_ip=str(meta.get("private_ip") or ""),
            shell_jump_id=str(meta.get("shell_jump_id") or meta.get("jump_id") or ""),
            ps_managed_system_id=str(meta.get("ps_managed_system_id") or ""),
        ))
    return NetCellListResponse(cells=cells)
