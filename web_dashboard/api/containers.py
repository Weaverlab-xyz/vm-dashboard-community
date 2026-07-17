"""
Portainer CE container management endpoints.

  GET  /api/containers/endpoints         — list Portainer environments
  GET  /api/containers                   — list containers on an endpoint
  POST /api/containers/{cid}/start       — start a container (direct, no job)
  POST /api/containers/{cid}/stop        — stop a container (direct, no job)
  DELETE /api/containers/{cid}           — remove a container (direct, no job)
  POST /api/containers/deploy            — create + start container via job
  GET  /api/containers/stacks            — list stacks on an endpoint
  POST /api/containers/stacks            — deploy a compose stack via job
  POST /api/containers/deploy-compose    — deploy a stored compose file to
                                           ECS / ACI / GCE via job
  GET  /api/containers/gce-compose       — list GCE compose COS instances
  POST /api/containers/gce-compose/{name}/stop — delete a GCE compose instance
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import ContainerStateCache, User, get_db  # noqa: F401
from ..models.containers import (
    ACIContainerInstanceInfo,
    ACIContainerListResponse,
    CloudRunJobInfo,
    CloudRunJobListResponse,
    ContainerActionResponse,
    ContainerInfo,
    ContainerListResponse,
    DeployComposeRequest,
    DeployContainerRequest,
    DeployContainerResponse,
    DeployStackRequest,
    DeployStackResponse,
    ECSTaskInfo,
    ECSTaskListResponse,
    GCEJumpointInfo,
    GCEJumpointListResponse,
    PortainerEndpoint,
    PortainerEndpointList,
    RancherImportRequest,
    RancherImportResponse,
    RancherNodeInfo,
    RancherNodeResponse,
    StackInfo,
    StackListResponse,
)
from ..services import (
    aws_service,
    azure_service,
    compose_service,
    container_inventory_service,
    job_service,
    portainer_service,
    storage_service,
)
from ..services.aws_service import AWSError
from ..services.azure_service import AzureError
from ..services.compose_service import ComposeError
from ..services.portainer_service import PortainerError, PortainerNotConfigured
from ..services.storage_service import StorageError
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/containers", tags=["containers"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _fmt_ports(port_bindings: list[dict]) -> list[str]:
    """Convert Docker port binding objects to readable strings."""
    result = []
    for p in port_bindings or []:
        ip = p.get("IP", "0.0.0.0") or "0.0.0.0"
        host = p.get("PublicPort", "")
        container = p.get("PrivatePort", "")
        proto = p.get("Type", "tcp")
        if host:
            result.append(f"{ip}:{host}->{container}/{proto}")
        else:
            result.append(f"{container}/{proto}")
    return result


def _map_container(raw: dict) -> ContainerInfo:
    """Map a raw Docker container JSON object to ContainerInfo."""
    cid = raw.get("Id", "")
    names = [n.lstrip("/") for n in raw.get("Names", [])]
    ports = _fmt_ports(raw.get("Ports", []))
    return ContainerInfo(
        id=cid,
        short_id=cid[:12],
        names=names,
        image=raw.get("Image", ""),
        status=raw.get("Status", ""),
        state=raw.get("State", ""),
        ports=ports,
        created=raw.get("Created", 0),
    )


def _map_endpoint(raw: dict) -> PortainerEndpoint:
    return PortainerEndpoint(
        id=raw.get("Id", 0),
        name=raw.get("Name", ""),
        url=raw.get("URL", "") or raw.get("PublicURL", ""),
        status=raw.get("Status", 2),
    )


def _map_ecs_task(raw: dict) -> ECSTaskInfo:
    arn = raw.get("taskArn", "")
    task_id = arn.split("/")[-1] if "/" in arn else arn
    cluster_arn = raw.get("clusterArn", "")
    cluster = cluster_arn.split("/")[-1] if "/" in cluster_arn else cluster_arn
    task_def = raw.get("taskDefinitionArn", "")
    if "/" in task_def:
        task_def = task_def.split("/")[-1]
    containers = [
        f"{c.get('name', '')} ({c.get('image', '').split('/')[-1] or '?'})"
        for c in raw.get("containers", [])
    ]
    started = raw.get("startedAt")
    stopped = raw.get("stoppedAt")
    return ECSTaskInfo(
        task_arn=arn,
        task_id=task_id,
        cluster=cluster,
        task_definition=task_def,
        last_status=raw.get("lastStatus", "UNKNOWN"),
        desired_status=raw.get("desiredStatus", ""),
        containers=containers,
        started_at=started.isoformat() if started else None,
        stopped_at=stopped.isoformat() if stopped else None,
        cpu=raw.get("cpu", ""),
        memory=raw.get("memory", ""),
    )


def _map_stack(raw: dict) -> StackInfo:
    return StackInfo(
        id=raw.get("Id", 0),
        name=raw.get("Name", ""),
        status=raw.get("Status", 0),
        type=raw.get("Type", 2),
        endpoint_id=raw.get("EndpointId", 0),
    )


# ── Environments ──────────────────────────────────────────────────────────────

@router.get("/endpoints", response_model=PortainerEndpointList)
async def list_endpoints(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """Return all Portainer environments/endpoints."""
    try:
        raw = await portainer_service.list_endpoints()
        return PortainerEndpointList(endpoints=[_map_endpoint(e) for e in raw])
    except PortainerNotConfigured as exc:
        # Structured detail so the page can render a setup card instead of an error
        raise HTTPException(
            status_code=503,
            detail={"code": "portainer_not_configured", "message": str(exc)},
        )
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Containers ────────────────────────────────────────────────────────────────

@router.get("", response_model=ContainerListResponse)
async def list_containers(
    background_tasks: BackgroundTasks,
    endpoint_id: int = Query(..., description="Portainer endpoint/environment ID"),
    all_containers: bool = Query(True, description="Include stopped containers"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "read")),
):
    """
    List containers on the given Portainer endpoint.
    Cold start (empty DB for this endpoint): blocks and populates from Portainer.
    Warm path: returns DB rows instantly, queues a background Portainer sync.
    """
    count = container_inventory_service.count_containers_in_db(db, endpoint_id)
    if count == 0:
        # Cold start — block until DB is populated
        await container_inventory_service.sync_from_portainer(db, endpoint_id)
    else:
        # Warm path — return immediately, sync in background
        background_tasks.add_task(_bg_sync_containers, endpoint_id)

    items = container_inventory_service.get_containers_from_db(db, endpoint_id, all_containers)
    return ContainerListResponse(containers=items, count=len(items))


async def _bg_sync_containers(endpoint_id: int) -> None:
    """Background task: refresh container state for one endpoint from Portainer."""
    db = container_inventory_service.get_fresh_db()
    try:
        await container_inventory_service.sync_from_portainer(db, endpoint_id)
    except Exception as exc:
        logger.warning("bg container sync failed (ep=%d): %s", endpoint_id, exc)
    finally:
        db.close()


@router.post("/{container_id}/start", response_model=ContainerActionResponse)
async def start_container(
    container_id: str,
    endpoint_id: int = Query(...),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Start a container. Fast operation — returns immediately."""
    try:
        await portainer_service.start_container(endpoint_id, container_id)
        return ContainerActionResponse(ok=True, message="Container started")
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/{container_id}/stop", response_model=ContainerActionResponse)
async def stop_container(
    container_id: str,
    endpoint_id: int = Query(...),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Stop a container. Fast operation — returns immediately."""
    try:
        await portainer_service.stop_container(endpoint_id, container_id)
        return ContainerActionResponse(ok=True, message="Container stopped")
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.delete("/{container_id}", response_model=ContainerActionResponse)
async def remove_container(
    container_id: str,
    endpoint_id: int = Query(...),
    force: bool = Query(True),
    current_user: User = Depends(require_permission("containers", "delete")),
):
    """Remove a container."""
    try:
        await portainer_service.remove_container(endpoint_id, container_id, force)
        return ContainerActionResponse(ok=True, message="Container removed")
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Deploy container (async job) ──────────────────────────────────────────────

@router.post("/deploy", response_model=DeployContainerResponse)
async def deploy_container(
    req: DeployContainerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """
    Deploy a container from an image. Creates a background job and returns immediately.
    Image pulls can take several minutes — follow progress at /jobs/{job_id}.
    """
    job = job_service.create_job(
        db,
        job_type="container_deploy",
        created_by=current_user.username,
        metadata={
            "endpoint_id": req.endpoint_id,
            "container_name": req.name,
            "image": req.image,
        },
    )
    background_tasks.add_task(
        _run_deploy_container,
        job.id,
        req.endpoint_id,
        req.name,
        req.image,
        [p.model_dump() for p in req.ports],
        [e.model_dump() for e in req.env],
        req.restart_policy,
    )
    return DeployContainerResponse(
        job_id=job.id,
        status="pending",
        message=f"Deploying container '{req.name}' from {req.image}…",
    )


async def _run_deploy_container(
    job_id: str,
    endpoint_id: int,
    name: str,
    image: str,
    ports: list[dict],
    env: list[dict],
    restart_policy: str,
):
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        job_service.update_progress(db, job_id, 10, f"Pulling image {image}…")

        result = await portainer_service.deploy_container(
            endpoint_id=endpoint_id,
            name=name,
            image=image,
            ports=ports,
            env=env,
            restart_policy=restart_policy,
        )

        job_service.update_progress(db, job_id, 90, f"Container created ({result['container_id'][:12]}), starting…")
        job_service.set_completed(db, job_id, {
            "container_id": result["container_id"],
            "container_name": name,
            "image": image,
        })
    except PortainerError as exc:
        job_service.set_failed(db, job_id, str(exc))
    except Exception as exc:
        logger.exception("Unexpected error deploying container %s", name)
        job_service.set_failed(db, job_id, f"Unexpected error: {exc}")
    finally:
        db.close()


# ── Stacks ────────────────────────────────────────────────────────────────────

@router.get("/stacks", response_model=StackListResponse)
async def list_stacks(
    endpoint_id: int = Query(...),
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List Portainer stacks on the given endpoint."""
    try:
        raw = await portainer_service.list_stacks(endpoint_id)
        items = [_map_stack(s) for s in raw]
        return StackListResponse(stacks=items, count=len(items))
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/stacks", response_model=DeployStackResponse)
async def deploy_stack(
    req: DeployStackRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """
    Deploy a Docker Compose stack via Portainer. Creates a background job.
    """
    job = job_service.create_job(
        db,
        job_type="stack_deploy",
        created_by=current_user.username,
        metadata={
            "endpoint_id": req.endpoint_id,
            "stack_name": req.name,
        },
    )
    background_tasks.add_task(
        _run_deploy_stack,
        job.id,
        req.endpoint_id,
        req.name,
        req.compose_content,
        [e.model_dump() for e in req.env],
    )
    return DeployStackResponse(
        job_id=job.id,
        status="pending",
        message=f"Deploying stack '{req.name}'…",
    )


async def _run_deploy_stack(
    job_id: str,
    endpoint_id: int,
    name: str,
    compose_content: str,
    env: list[dict],
):
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        job_service.update_progress(db, job_id, 20, f"Deploying stack '{name}' to Portainer…")

        stack = await portainer_service.deploy_stack(
            endpoint_id=endpoint_id,
            name=name,
            compose_content=compose_content,
            env=env,
        )

        stack_id = stack.get("Id", "?")
        job_service.update_progress(db, job_id, 90, f"Stack created (ID: {stack_id}), pulling images…")
        job_service.set_completed(db, job_id, {
            "stack_id": stack_id,
            "stack_name": name,
        })
    except PortainerError as exc:
        job_service.set_failed(db, job_id, str(exc))
    except Exception as exc:
        logger.exception("Unexpected error deploying stack %s", name)
        job_service.set_failed(db, job_id, f"Unexpected error: {exc}")
    finally:
        db.close()


# ── Generic Compose → cloud (ECS / ACI / GCE) ───────────────────────────────

@router.post("/deploy-compose", response_model=DeployStackResponse)
async def deploy_compose(
    req: DeployComposeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Deploy a Docker Compose file from the storage backend to ECS, ACI, or GCE.

    The compose file is fetched and translated to the chosen provider's native
    multi-container unit in a background job (see _run_deploy_compose)."""
    provider = (req.provider or "").lower()
    if provider not in ("ecs", "aci", "gce"):
        raise HTTPException(status_code=400, detail="provider must be one of: ecs, aci, gce")
    if not req.compose_backend or not req.compose_file:
        raise HTTPException(status_code=400, detail="compose_backend and compose_file are required")

    job = job_service.create_job(
        db,
        job_type="compose_deploy",
        created_by=current_user.username,
        metadata={
            "provider": provider,
            "name": req.name,
            "compose_backend": req.compose_backend,
            "compose_file": req.compose_file,
        },
    )
    background_tasks.add_task(_run_deploy_compose, job.id, req.model_dump())
    return DeployStackResponse(
        job_id=job.id,
        status="pending",
        message=f"Deploying compose '{req.compose_file}' to {provider.upper()}…",
    )


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


async def _run_deploy_compose(job_id: str, req: dict):
    from ..services import config_service, gcp_service
    from ..services.gcp_service import GCPError

    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        provider = req["provider"].lower()
        name = req["name"]
        overrides = req.get("overrides") or {}

        job_service.update_progress(db, job_id, 10, f"Fetching '{req['compose_file']}'…")
        raw = await storage_service.fetch_asset_in(req["compose_backend"], req["compose_file"])
        spec = compose_service.parse_and_validate(raw.decode("utf-8", errors="replace"))

        job_service.update_progress(db, job_id, 40, f"Deploying {len(spec.services)} service(s) to {provider.upper()}…")

        if provider == "ecs":
            cpu = str(req.get("cpu") and int(req["cpu"] * 1024) or settings.ansible_ecs_cpu)
            memory = str(req.get("memory_mb") or settings.ansible_ecs_memory)
            result = await aws_service.deploy_compose_ecs(
                region=config_service.get("aws_region") or settings.aws_region,
                cluster=overrides.get("cluster") or settings.bt_ecs_cluster,
                family=name,
                services=spec.services,
                cpu=cpu,
                memory=memory,
                subnet_id=overrides.get("subnet_id") or settings.ansible_ecs_subnet_id,
                security_group_ids=overrides.get("security_group_ids")
                    or _split_csv(settings.ansible_ecs_security_group_ids),
                execution_role_arn=overrides.get("execution_role_arn")
                    or settings.ansible_ecs_execution_role_arn,
                assign_public_ip=overrides.get("assign_public_ip", True),
            )
        elif provider == "aci":
            rg = overrides.get("resource_group") or _aci_rg()
            result = await azure_service.deploy_compose_aci(
                rg=rg,
                location=overrides.get("location") or settings.azure_location,
                name=name,
                services=spec.services,
                subnet_id=overrides.get("subnet_id") or settings.azure_aci_subnet_id,
                acr_server=settings.azure_acr_server,
                acr_username=settings.azure_acr_username,
                acr_password=settings.azure_acr_password,
                default_cpu=req.get("cpu") or settings.azure_aci_cpu,
                default_memory_gb=(req["memory_mb"] / 1024.0) if req.get("memory_mb") else settings.azure_aci_memory,
            )
        else:  # gce
            project_id = config_service.get("gcp_project_id") or settings.gcp_project_id
            if not project_id:
                job_service.set_failed(db, job_id, "GCP project not configured.")
                return
            result = await gcp_service.deploy_compose_gce(
                project_id=project_id,
                zone=overrides.get("zone") or config_service.get("gcp_zone") or settings.gcp_zone,
                name=name,
                services=spec.services,
                machine_type=overrides.get("machine_type") or "e2-small",
                subnetwork=overrides.get("subnetwork") or settings.gcp_subnetwork,
                create_external_ip=overrides.get("create_external_ip", False),
            )

        job_service.update_progress(db, job_id, 95, "Deployed, finalizing…")
        job_service.set_completed(db, job_id, {"provider": provider, **result})
    except (StorageError, ComposeError) as exc:
        job_service.set_failed(db, job_id, str(exc))
    except (AWSError, AzureError, GCPError) as exc:
        job_service.set_failed(db, job_id, str(exc))
    except Exception as exc:
        logger.exception("Unexpected error deploying compose %s", req.get("name"))
        job_service.set_failed(db, job_id, f"Unexpected error: {exc}")
    finally:
        db.close()


# ── ECS Tasks ─────────────────────────────────────────────────────────────────

@router.get("/ecs-tasks", response_model=ECSTaskListResponse)
async def list_ecs_tasks(
    include_stopped: bool = Query(False, description="Include STOPPED tasks"),
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List ECS Fargate tasks in the configured cluster."""
    try:
        raw = await aws_service.list_ecs_tasks(
            settings.aws_region, settings.bt_ecs_cluster, include_stopped
        )
        tasks = [_map_ecs_task(t) for t in raw]
        return ECSTaskListResponse(
            tasks=tasks,
            cluster=settings.bt_ecs_cluster,
            count=len(tasks),
        )
    except AWSError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/ecs-tasks/{task_id}/stop", response_model=ContainerActionResponse)
async def stop_ecs_task(
    task_id: str,
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Stop a running ECS Fargate task by its short ID or ARN."""
    try:
        await aws_service.stop_ecs_jumpoint_task(
            settings.aws_region, settings.bt_ecs_cluster, task_id
        )
        return ContainerActionResponse(ok=True, message="ECS task stopped")
    except AWSError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def _aci_rg() -> str:
    """ACI resource group: wizard/Settings (config_service) first, env fallback.
    ACI-specific RG, else the general Azure RG. Mirrors azure.py:_aci_rg."""
    from ..services import config_service
    return (config_service.get("azure_aci_resource_group")
            or config_service.get("azure_resource_group")
            or settings.azure_aci_resource_group
            or settings.azure_resource_group)


# ── ACI Container Instances ────────────────────────────────────────────────────

@router.get("/aci-containers", response_model=ACIContainerListResponse)
async def list_aci_containers(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List Azure Container Instances (ACI) in the configured resource group."""
    try:
        raw = await azure_service.list_aci_container_instances(
            _aci_rg(),
        )
        containers = [
            ACIContainerInstanceInfo(
                container_group_id=c.get("id", ""),
                container_group_name=c.get("name", ""),
                resource_group=c.get("resource_group", ""),
                state=c.get("state", "Unknown"),
                os_type=c.get("os_type", "Linux"),
                cpu=float(c.get("cpu", 0.0)),
                memory=float(c.get("memory", 0.0)),
                containers=c.get("containers", []),
                created_at=c.get("created_at"),
                started_at=c.get("started_at"),
                restart_policy=c.get("restart_policy", "OnFailure"),
            )
            for c in raw
        ]
        rg = _aci_rg()
        return ACIContainerListResponse(
            containers=containers,
            resource_group=rg,
            count=len(containers),
        )
    except AzureError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/aci-containers/{container_group_name}/stop", response_model=ContainerActionResponse)
async def stop_aci_container(
    container_group_name: str,
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Stop a running ACI container group."""
    try:
        await azure_service.stop_aci_container_group(
            _aci_rg(),
            container_group_name,
        )
        return ContainerActionResponse(ok=True, message="ACI container stopped")
    except AzureError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── GCP Jumpoint container instances (COS on GCE) ────────────────────────────
# The BT SRA Jumpoint is an outbound-only daemon, so it runs as a container on
# a Container-Optimised-OS GCE instance — not Cloud Run (which requires an HTTP
# server on $PORT). See gcp_service.run_gce_jumpoint.

def _gcp_project_id() -> str:
    """GCP project, wizard/Settings (config_service) first, env fallback."""
    from ..services import config_service
    return config_service.get("gcp_project_id") or settings.gcp_project_id


@router.get("/gce-jumpoints", response_model=GCEJumpointListResponse)
async def list_gce_jumpoints_endpoint(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List BT Jumpoint container instances (labels.purpose=bt-jumpoint) across zones."""
    from ..services import gcp_service
    from ..services.gcp_service import GCPError

    project_id = _gcp_project_id()
    if not project_id:
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    try:
        raw = await gcp_service.list_gce_jumpoints(project_id)
        instances = [
            GCEJumpointInfo(
                name=i.get("name", ""),
                zone=i.get("zone", ""),
                status=i.get("status", "UNKNOWN"),
                machine_type=i.get("machine_type", ""),
                image=i.get("image", ""),
                internal_ip=i.get("internal_ip", ""),
                external_ip=i.get("external_ip", ""),
                created_at=i.get("created_at"),
            )
            for i in raw
        ]
        return GCEJumpointListResponse(
            instances=instances,
            project_id=project_id,
            count=len(instances),
        )
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/gce-jumpoints/{name}/stop", response_model=ContainerActionResponse)
async def stop_gce_jumpoint_endpoint(
    name: str,
    zone: str = Query(..., description="GCE zone the instance lives in"),
    current_user: User = Depends(require_permission("containers", "delete")),
):
    """Stop (delete) a Jumpoint instance. It is recreated on the next VM deploy."""
    from ..services import gcp_service
    from ..services.gcp_service import GCPError

    project_id = _gcp_project_id()
    if not project_id:
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    try:
        await gcp_service.stop_gce_jumpoint(project_id, zone, name)
        return ContainerActionResponse(ok=True, message="Jumpoint instance deleted")
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/gce-compose", response_model=GCEJumpointListResponse)
async def list_gce_compose_endpoint(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List compose container instances (labels.purpose=compose) across zones."""
    from ..services import gcp_service
    from ..services.gcp_service import GCPError

    project_id = _gcp_project_id()
    if not project_id:
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    try:
        raw = await gcp_service.list_gce_compose(project_id)
        instances = [
            GCEJumpointInfo(
                name=i.get("name", ""),
                zone=i.get("zone", ""),
                status=i.get("status", "UNKNOWN"),
                machine_type=i.get("machine_type", ""),
                image=i.get("image", ""),
                internal_ip=i.get("internal_ip", ""),
                external_ip=i.get("external_ip", ""),
                created_at=i.get("created_at"),
            )
            for i in raw
        ]
        return GCEJumpointListResponse(
            instances=instances,
            project_id=project_id,
            count=len(instances),
        )
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/gce-compose/{name}/stop", response_model=ContainerActionResponse)
async def stop_gce_compose_endpoint(
    name: str,
    zone: str = Query(..., description="GCE zone the instance lives in"),
    current_user: User = Depends(require_permission("containers", "delete")),
):
    """Delete a compose COS instance (reuses the instance-delete path)."""
    from ..services import gcp_service
    from ..services.gcp_service import GCPError

    project_id = _gcp_project_id()
    if not project_id:
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    try:
        await gcp_service.stop_gce_jumpoint(project_id, zone, name)
        return ContainerActionResponse(ok=True, message="Compose instance deleted")
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── GCP Cloud Run runner jobs (Ansible / promote / k8s) ──────────────────────
# The runner jobs self-delete on completion, so this is effectively an in-flight
# view — the GCP analogue of the ECS-tasks / ACI panels. Read-only, 5 most recent.

@router.get("/gce-cloud-run-jobs", response_model=CloudRunJobListResponse)
async def list_gce_cloud_run_jobs_endpoint(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List dashboard-managed Cloud Run runner jobs (Ansible / promote / k8s),
    newest first and capped at 5. These jobs self-delete when they finish, so the
    list is effectively the ones currently in flight."""
    from ..services import gcp_service
    from ..services.gcp_service import GCPError

    project_id = _gcp_project_id()
    if not project_id:
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    try:
        raw = await gcp_service.list_cloud_run_jobs(project_id, limit=5)
        jobs = [
            CloudRunJobInfo(
                name=j.get("name", ""),
                region=j.get("region", ""),
                purpose=j.get("purpose", ""),
                image=j.get("image", ""),
                status=j.get("status", ""),
                created_at=j.get("created_at"),
            )
            for j in raw
        ]
        return CloudRunJobListResponse(jobs=jobs, project_id=project_id, count=len(jobs))
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Rancher management node (privileged container on GCE COS) ────────────────

@router.get("/rancher", response_model=RancherNodeResponse)
async def get_rancher_node(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List the Rancher management-node COS instance(s) (labels.purpose=rancher)
    and report whether the integration is configured + its pinned server URL."""
    from ..services import config_service, gcp_service
    from ..services.gcp_service import GCPError

    project_id = _gcp_project_id()
    bootstrap = config_service.get("rancher_bootstrap_password")
    configured = bool(project_id and bootstrap)
    server_url = config_service.get("rancher_server_url") or ""
    if not project_id:
        # Not configured yet — return an empty, not-configured shell (no 503, so
        # the tab can render the setup card like Portainer does).
        return RancherNodeResponse(nodes=[], project_id="", count=0,
                                   configured=False, server_url=server_url)
    try:
        raw = await gcp_service.list_gce_rancher(project_id)
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    nodes = [
        RancherNodeInfo(
            name=i.get("name", ""), zone=i.get("zone", ""),
            status=i.get("status", "UNKNOWN"), machine_type=i.get("machine_type", ""),
            image=i.get("image", ""), internal_ip=i.get("internal_ip", ""),
            external_ip=i.get("external_ip", ""), url=i.get("url", ""),
            created_at=i.get("created_at"),
        )
        for i in raw
    ]
    return RancherNodeResponse(nodes=nodes, project_id=project_id, count=len(nodes),
                               configured=configured, server_url=server_url)


@router.get("/rancher/firewall")
async def get_rancher_firewall(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "read")),
):
    """Read-only breakdown of the Rancher node's firewall source set: the manual
    CIDRs, the auto-discovered provisioned-cluster egress IPs, the dashboard-managed
    Web-Jump Jumpoint IP, and the effective merged allow-list — so the operator can
    see exactly which sources reach the node and why."""
    from ..services import rancher_node_service
    return rancher_node_service.firewall_status(db)


@router.post("/rancher/deploy", response_model=DeployContainerResponse)
async def deploy_rancher_node(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Deploy (or reuse) the Rancher management node. Enqueues a durable
    `rancher_node_deploy` job (VM boot + Rancher bootstrap can take minutes);
    follow progress at /jobs/{job_id}. The job reads its knobs from Settings."""
    from ..services import config_service

    if not _gcp_project_id():
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    if not config_service.get("rancher_bootstrap_password"):
        raise HTTPException(
            status_code=400,
            detail="Set a Rancher bootstrap password in Settings → Kubernetes before deploying.")
    job = job_service.create_job(
        db, job_type="rancher_node_deploy", created_by=current_user.username, metadata={})
    return DeployContainerResponse(
        job_id=job.id, status="pending", message="Deploying the Rancher management node…")


@router.post("/rancher/{name}/stop", response_model=DeployContainerResponse)
async def stop_rancher_node(
    name: str,
    zone: str = Query("", description="GCE zone (blank → configured Rancher zone)"),
    force: bool = Query(False, description="Tear down even if clusters are still imported"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "delete")),
):
    """Tear down the Rancher node (VM + firewall) and clean up Entitle / PRA /
    config. Enqueues a durable `rancher_node_teardown` job. Refuses (unless
    `force`) while clusters are still imported."""
    if not _gcp_project_id():
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    job = job_service.create_job(
        db, job_type="rancher_node_teardown", created_by=current_user.username,
        metadata={"name": name, "zone": zone, "force": force})
    return DeployContainerResponse(
        job_id=job.id, status="pending", message=f"Tearing down Rancher node '{name}'…")


@router.post("/rancher/import", response_model=RancherImportResponse)
async def import_cluster_into_rancher(
    req: RancherImportRequest,
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Create an imported cluster in Rancher and return the registration manifest
    URL + the `kubectl apply` command to run against the target cluster. The
    cattle-cluster-agent dials OUT to the public node, so private clusters on any
    cloud / on-prem work as long as they have egress."""
    from ..services import rancher_service
    from ..services.rancher_service import RancherError, RancherNotConfigured

    try:
        cluster_id, manifest_url = await rancher_service.create_import_cluster_direct(name=req.name)
    except RancherNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except RancherError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return RancherImportResponse(
        cluster_id=cluster_id, manifest_url=manifest_url,
        apply_command=f"kubectl apply -f {manifest_url}")
