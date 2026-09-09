"""Virtual-desktop management API.

Gated on ``vdesktops_enabled``. CRUD over the ``virtual_desktops`` table via
``vdesktop_service``. **All three clouds** provision one private VM per seat,
durably via the job runner, and every seat is brokered as a PRA Jump Item — a
Remote RDP item for a Windows seat (Azure only), a Shell Jump on 22 for a Linux one.

  GET    /api/desktops                       — list seats
  GET    /api/desktops/pools                 — list pool summaries
  POST   /api/desktops/pools                 — create a pool (provisions VMs)
  POST   /api/desktops/pools/{name}/scale    — grow/shrink a pool
  DELETE /api/desktops/pools/{name}          — delete a pool
  GET    /api/desktops/pools/{name}/seats    — seats in one pool
  GET    /api/desktops/seats/{id}/session    — PRA connection info for a seat
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.vdesktop import PoolCreateRequest, PoolScaleRequest
from ..services import job_service, vdesktop_service
from ..services.vdesktop_service import VDesktopError
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/desktops", tags=["desktops"])


@router.get("/__phase0__")
def phase0_status() -> dict:
    """Health check — confirms the router is mounted/reachable."""
    return {
        "phase": 0,
        "ok": True,
        "note": (
            "Virtual-desktop router mounted. AWS, Azure and GCP pools provision VMs "
            "via the job runner; seats are brokered as PRA Jump Items."
        ),
    }


@router.get("")
async def list_desktops(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Every desktop seat across all pools."""
    return {"desktops": vdesktop_service.list_desktops(db)}


@router.get("/pools")
async def list_pools(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Pool summaries (name, cloud, kind, seat count, status breakdown)."""
    return {"pools": vdesktop_service.list_pools(db)}


def _cfg(key: str, fallback: str = "") -> str:
    """Read a value from config_service (DB/wizard) with env-var fallback."""
    from ..config import settings
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


async def _configured_ssh_key(cloud: str, *, region: str = "", project_id: str = "") -> str:
    """The SSH public key configured for ``cloud``, or "" if it cannot be fetched.

    Best-effort ON PURPOSE. The pool form on AWS and GCP does not collect a key — the
    single-VM deploy paths on those clouds do not either (``aws_vm_service`` reads
    Secrets Manager, ``gcp_vm_service`` reads Secret Manager), and GCP has no endpoint
    that returns a full key anyway: ``/api/gcp/secrets/ssh-key`` truncates to 80 chars.
    Resolving it here keeps the form honest instead of asking for something it cannot
    show. On any failure this returns "" and ``validate_spec`` raises the accurate
    "...pool requires: ssh_public_key" 400 rather than a secrets-store traceback.
    """
    from ..services import region_config
    try:
        if cloud == "azure":
            from ..services import azure_service
            return await azure_service.resolve_azure_ssh_public_key(
                _cfg("azure_key_vault_url"),
                _cfg("azure_ssh_keypair_secret_name"),
                _cfg("azure_ssh_key_secret_name")) or ""
        if cloud == "aws":
            from ..services import aws_service
            secret = region_config.resolve_region("aws", region)["ssh_key_secret"]
            if not secret:
                return ""
            detail = await aws_service.get_ssh_public_key_from_secret(region, secret)
            return (detail or {}).get("public_key") or ""
        if cloud == "gcp":
            from ..services import gcp_service
            secret = region_config.resolve_region("gcp", region)["ssh_key_secret"]
            if not (secret and project_id):
                return ""
            return await gcp_service.get_ssh_public_key(
                project_id=project_id, secret_name=secret) or ""
    except Exception as exc:                      # noqa: BLE001 - see the docstring
        logger.warning("desktop pool: could not resolve the configured %s SSH key: %s",
                       cloud, exc)
    return ""


async def _azure_spec(payload: PoolCreateRequest) -> dict:
    """The azure_service.deploy_vm spec built from the pool request.

    Resource group + location fall back to the configured Azure defaults
    (``azure_resource_group`` / ``azure_location``) so the pool form can leave them
    blank — the same resolution the Azure deploy path uses. Subnet + VM size fall back
    to the Virtual Desktops panel defaults (``azure_desktops_subnet_id`` /
    ``azure_desktops_vm_size``) so pools land on the non-delegated desktops subnet by
    default instead of whatever the picker lists.

    Multi-region: subnet / VM size / resource group resolve through the chosen region's
    config set, which falls back per-field to the flat keys when a region isn't
    configured — so a pool in westus2 gets the westus2 desktops subnet and size, while
    a single-region setup behaves exactly as before.
    """
    from ..services.region_config import resolve_region

    location = payload.location or _cfg("azure_location") or "centralus"
    region = resolve_region("azure", location)
    return {
        "location": location,
        "resource_group": payload.resource_group or region["resource_group"] or "vm-cli-rg",
        "vm_size": payload.vm_size or payload.size or region["default_vm_size"],
        "image_id": payload.image_id or payload.image,
        "image_publisher": payload.image_publisher, "image_offer": payload.image_offer,
        "image_sku": payload.image_sku, "image_version": payload.image_version,
        "subnet_id": payload.subnet_id or region["desktops_subnet_id"], "nsg_ids": payload.nsg_ids,
        "create_public_ip": payload.create_public_ip,
        "os_type": payload.os_type,
        "trusted_launch": payload.trusted_launch,
        "ssh_username": payload.ssh_username or "azureuser",
        # Azure's picker CAN show a key, so its form posts one; this only fires for an
        # API caller that left it out.
        "ssh_public_key": payload.ssh_public_key or await _configured_ssh_key("azure"),
    }


async def _aws_spec(payload: PoolCreateRequest) -> dict:
    """The aws_service.launch_instance spec built from the pool request.

    Region falls back to the configured ``aws_region``, so a single-region setup can
    leave it blank. AMI and instance type also accept the generic ``image`` / ``size``
    fields, so a caller that does not care which cloud it is talking to can fill those
    two and be understood by either backend.

    Subnet and instance type resolve through the chosen region's config set:
    ``payload -> aws_region.<r>.desktops_subnet_id -> aws_desktops_subnet_id ->
    aws_default_subnet_id``. That last hop is the deliberate difference from Azure,
    which has NO secondary fallback: an Azure sandbox's VM subnet may be DELEGATED
    (aci-subnet cannot host a VM NIC), so inheriting it would produce a pool that
    cannot deploy. Every AWS subnet can host an instance, so inheriting the VM subnet
    is a default an operator can live with rather than a guess.

    No ``ssh_username``: ``launch_instance`` takes none — cloud-init installs the key
    for the AMI's own default user, which is what ``_AwsSeats.default_username``
    records for the PRA side.
    """
    from ..services.region_config import resolve_region

    region_id = payload.region or _cfg("aws_region") or "us-east-1"
    rc = resolve_region("aws", region_id)
    return {
        "region": region_id,
        "ami_id": payload.ami_id or payload.image,
        "instance_type": payload.instance_type or payload.size or rc["desktops_instance_type"],
        "subnet_id": payload.subnet_id or rc["desktops_subnet_id"],
        "security_group_ids": payload.security_group_ids,
        "iam_instance_profile": payload.iam_instance_profile,
        "os_type": payload.os_type,
        "ssh_public_key": payload.ssh_public_key or await _configured_ssh_key(
            "aws", region=region_id),
    }


async def _gcp_spec(payload: PoolCreateRequest) -> dict:
    """The gcp_service.launch_instance spec built from the pool request.

    Project and zone fall back to the configured ``gcp_project`` / ``gcp_zone``,
    matching how the other two builders fall back. Machine type and image also accept
    the generic ``size`` / ``image`` fields.

    Subnetwork and machine type resolve through the region derived FROM THE ZONE — a
    pool commits to a zone and ``resolve_region`` is keyed on regions — then fall back
    ``gcp_desktops_subnetwork -> gcp_subnetwork``. Same reasoning as ``_aws_spec``:
    every GCP subnetwork can host an instance, so inheriting the VM one is a usable
    default rather than a guess.

    ``ssh_username`` resolves to the configured ``gcp_ssh_username`` (default
    ``gcp-user``) and NOT ``_GcpSeats.default_username``, whose "gcpuser" is a name
    nothing else in the product uses.
    """
    from ..services import region_catalog
    from ..services.region_config import resolve_region

    project_id = payload.project_id or _cfg("gcp_project")
    zone = payload.zone or _cfg("gcp_zone") or "us-central1-a"
    gcp_region = region_catalog.region_from_zone(zone)
    rc = resolve_region("gcp", gcp_region)
    return {
        "project_id": project_id,
        "zone": zone,
        "machine_type": payload.machine_type or payload.size or rc["desktops_machine_type"],
        "image_self_link": payload.image_self_link or payload.image,
        "subnetwork": payload.subnetwork or rc["desktops_subnetwork"],
        "create_external_ip": payload.create_external_ip,
        "disk_size_gb": payload.disk_size_gb,
        "network_tags": payload.network_tags,
        "os_type": payload.os_type,
        "ssh_username": payload.ssh_username or _cfg("gcp_ssh_username") or "gcp-user",
        "ssh_public_key": payload.ssh_public_key or await _configured_ssh_key(
            "gcp", region=gcp_region, project_id=project_id),
    }


# Which builder makes a spec for which cloud. A cloud absent here sends `spec=None`,
# which is what a cloud with no seat backend wants — there are none.
#
# These are COROUTINES: each one may resolve the configured SSH key from its cloud's
# secret store, the same way `aws_vm_service` / `gcp_vm_service` do for a single VM.
_SPEC_BUILDERS = {"azure": _azure_spec, "aws": _aws_spec, "gcp": _gcp_spec}


@router.post("/pools", status_code=201)
async def create_pool(
    payload: PoolCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a desktop pool. All three clouds provision one private VM per seat,
    durably via the job runner and tagged for the pool.

    Only Azure supports Windows seats. EC2 hands back Windows credentials as password
    data encrypted to the launch key pair and GCE delivers them through windows-keys
    instance metadata; neither is wired here, so an AWS or GCP Windows pool is refused
    with that reason rather than provisioned into seats nobody can sign into.

    Linux seats are brokered as PRA Shell Jumps with no credential injection — see
    ``vdesktop_service.provision_seats``."""
    builder = _SPEC_BUILDERS.get((payload.cloud or "").lower())
    spec = await builder(payload) if builder else None
    try:
        result = vdesktop_service.create_pool(
            db, cloud=payload.cloud, name=payload.name, count=payload.count,
            created_by=current_user.username, spec=spec,
        )
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # create_pool already enqueued the vdesktop_pool_provision job with seat_ids +
    # spec in its metadata; the in-container job runner claims it. No in-process
    # BackgroundTask (a gunicorn recycle could kill it mid-provision, stranding a
    # pending seat with an untracked VM). Mirrors clouddb/k8s.
    return result


@router.post("/pools/{name}/scale")
async def scale_pool(
    name: str,
    payload: PoolScaleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Grow/shrink a pool to ``count`` seats (VMs are provisioned/terminated via the
    durable job runner)."""
    try:
        result = vdesktop_service.scale_pool(db, name, payload.count)
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Enqueue durable jobs instead of in-process BackgroundTasks (see create_pool).
    if result.get("to_provision"):
        job = job_service.create_job(
            db, job_type="vdesktop_pool_provision", created_by=current_user.username,
            metadata={"pool_name": name, "seat_ids": result["to_provision"],
                      "spec": result["spec"]},
        )
        result["job_id"] = job.id
    if result.get("to_teardown"):
        job = job_service.create_job(
            db, job_type="vdesktop_pool_teardown", created_by=current_user.username,
            metadata={"pool_name": name, "seat_ids": result["to_teardown"]},
        )
        result["job_id"] = job.id
    return result


@router.delete("/pools/{name}")
async def delete_pool(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a pool: terminate the backing VMs (durable, via the job runner) then
    drop the rows. A pool whose cloud has no seat backend has no VMs and its rows drop
    immediately — the unknown-cloud path, not an AWS/GCP one."""
    result = vdesktop_service.delete_pool(db, name)
    if result["deleted_seats"] == 0:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' not found.")
    job_id = None
    if result.get("to_teardown"):
        job = job_service.create_job(
            db, job_type="vdesktop_pool_teardown", created_by=current_user.username,
            metadata={"pool_name": name, "seat_ids": result["to_teardown"]},
        )
        job_id = job.id
    return {"ok": True, "deleted_seats": result["deleted_seats"], "job_id": job_id}


@router.get("/pools/{name}/seats")
async def list_pool_seats(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Seats in one pool (id, vm_resource_id, status, pra_jump_id) — backs the
    per-pool Seats view + the Open-session action."""
    return {"seats": vdesktop_service.get_pool(db, name)}


@router.get("/seats/{seat_id}/session")
async def open_seat_session(
    seat_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """PRA connection info for a seat, plus a link to the PRA console.

    Thin on purpose: the resolution lives in ``vdesktop_service.session_info`` so it
    is testable without FastAPI and so the Jump Group / Gateway come from the SAME
    per-cloud resolver the provisioner used. This endpoint used to read the
    ``azure_*`` keys directly, which named the wrong Jump Group for an AWS or GCP
    seat. The web app cannot drive the rep console, so the rep launches the Jump Item
    there (mirrors the k8s open_console pattern).
    """
    info = vdesktop_service.session_info(db, seat_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Seat '{seat_id}' not found.")
    return info
