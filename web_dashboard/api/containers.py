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
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import ContainerStateCache, User, get_db  # noqa: F401
from ..models.containers import (
    ACIContainerInstanceInfo,
    ACIContainerListResponse,
    CloudRunServiceInfo,
    CloudRunServiceListResponse,
    ContainerActionResponse,
    ContainerInfo,
    ContainerListResponse,
    DeployContainerRequest,
    DeployContainerResponse,
    DeployStackRequest,
    DeployStackResponse,
    ECSTaskInfo,
    ECSTaskListResponse,
    PortainerEndpoint,
    PortainerEndpointList,
    StackInfo,
    StackListResponse,
)
from ..services import aws_service, azure_service, container_inventory_service, job_service, portainer_service
from ..services.aws_service import AWSError
from ..services.azure_service import AzureError
from ..services.portainer_service import PortainerError
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
    workgroup: str = Query("weaverlab"),
    current_user: User = Depends(require_permission("containers", "read")),
):
    """Return all Portainer environments/endpoints."""
    try:
        raw = await portainer_service.list_endpoints(workgroup=workgroup)
        return PortainerEndpointList(endpoints=[_map_endpoint(e) for e in raw])
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Containers ────────────────────────────────────────────────────────────────

@router.get("", response_model=ContainerListResponse)
async def list_containers(
    background_tasks: BackgroundTasks,
    endpoint_id: int = Query(..., description="Portainer endpoint/environment ID"),
    all_containers: bool = Query(True, description="Include stopped containers"),
    workgroup: str = Query("weaverlab"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("containers", "read")),
):
    """
    List containers on the given Portainer endpoint.
    Cold start (empty DB for this endpoint): blocks and populates from Portainer.
    Warm path: returns DB rows instantly, queues a background Portainer sync.
    """
    count = container_inventory_service.count_containers_in_db(db, workgroup, endpoint_id)
    if count == 0:
        # Cold start — block until DB is populated
        await container_inventory_service.sync_from_portainer(db, workgroup, endpoint_id)
    else:
        # Warm path — return immediately, sync in background
        background_tasks.add_task(_bg_sync_containers, workgroup, endpoint_id)

    items = container_inventory_service.get_containers_from_db(db, workgroup, endpoint_id, all_containers)
    return ContainerListResponse(containers=items, count=len(items))


async def _bg_sync_containers(workgroup: str, endpoint_id: int) -> None:
    """Background task: refresh container state for one endpoint from Portainer."""
    db = container_inventory_service.get_fresh_db()
    try:
        await container_inventory_service.sync_from_portainer(db, workgroup, endpoint_id)
    except Exception as exc:
        logger.warning("bg container sync failed (wg=%s ep=%d): %s", workgroup, endpoint_id, exc)
    finally:
        db.close()


@router.post("/{container_id}/start", response_model=ContainerActionResponse)
async def start_container(
    container_id: str,
    endpoint_id: int = Query(...),
    workgroup: str = Query("weaverlab"),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Start a container. Fast operation — returns immediately."""
    try:
        await portainer_service.start_container(endpoint_id, container_id, workgroup=workgroup)
        return ContainerActionResponse(ok=True, message="Container started")
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/{container_id}/stop", response_model=ContainerActionResponse)
async def stop_container(
    container_id: str,
    endpoint_id: int = Query(...),
    workgroup: str = Query("weaverlab"),
    current_user: User = Depends(require_permission("containers", "write")),
):
    """Stop a container. Fast operation — returns immediately."""
    try:
        await portainer_service.stop_container(endpoint_id, container_id, workgroup=workgroup)
        return ContainerActionResponse(ok=True, message="Container stopped")
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.delete("/{container_id}", response_model=ContainerActionResponse)
async def remove_container(
    container_id: str,
    endpoint_id: int = Query(...),
    force: bool = Query(True),
    workgroup: str = Query("weaverlab"),
    current_user: User = Depends(require_permission("containers", "delete")),
):
    """Remove a container."""
    try:
        await portainer_service.remove_container(endpoint_id, container_id, force, workgroup=workgroup)
        return ContainerActionResponse(ok=True, message="Container removed")
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Deploy container (async job) ──────────────────────────────────────────────

@router.post("/deploy", response_model=DeployContainerResponse)
async def deploy_container(
    req: DeployContainerRequest,
    background_tasks: BackgroundTasks,
    workgroup: str = Query("weaverlab"),
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
        workgroup,
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
    workgroup: str = "weaverlab",
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
            workgroup=workgroup,
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
    workgroup: str = Query("weaverlab"),
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List Portainer stacks on the given endpoint."""
    try:
        raw = await portainer_service.list_stacks(endpoint_id, workgroup=workgroup)
        items = [_map_stack(s) for s in raw]
        return StackListResponse(stacks=items, count=len(items))
    except PortainerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/stacks", response_model=DeployStackResponse)
async def deploy_stack(
    req: DeployStackRequest,
    background_tasks: BackgroundTasks,
    workgroup: str = Query("weaverlab"),
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
        workgroup,
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
    workgroup: str = "weaverlab",
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
            workgroup=workgroup,
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


# ── ACI Container Instances ────────────────────────────────────────────────────

@router.get("/aci-containers", response_model=ACIContainerListResponse)
async def list_aci_containers(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List Azure Container Instances (ACI) in the configured resource group."""
    try:
        raw = await azure_service.list_aci_container_instances(
            settings.azure_aci_resource_group or settings.azure_resource_group,
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
        rg = settings.azure_aci_resource_group or settings.azure_resource_group
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
            settings.azure_aci_resource_group or settings.azure_resource_group,
            container_group_name,
        )
        return ContainerActionResponse(ok=True, message="ACI container stopped")
    except AzureError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── GCP Cloud Run services ────────────────────────────────────────────────────

@router.get("/cloud-run-services", response_model=CloudRunServiceListResponse)
async def list_cloud_run_services_endpoint(
    current_user: User = Depends(require_permission("containers", "read")),
):
    """List GCP Cloud Run services in the configured project + region."""
    from ..services import gcp_service
    from ..services.gcp_service import GCPError

    project_id = settings.gcp_project_id
    if not project_id:
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    region = (
        getattr(settings, "gcp_ansible_cloud_run_region", "")
        or settings.gcp_region
        or "us-central1"
    )
    try:
        raw = await gcp_service.list_cloud_run_services(project_id, region)
        services = [
            CloudRunServiceInfo(
                name=s.get("name", ""),
                region=s.get("region", region),
                image=s.get("image", ""),
                uri=s.get("uri", ""),
                ready=bool(s.get("ready", False)),
                traffic_percent=int(s.get("traffic_percent", 0)),
                create_time=s.get("create_time"),
                update_time=s.get("update_time"),
                last_modifier=s.get("last_modifier", ""),
            )
            for s in raw
        ]
        return CloudRunServiceListResponse(
            services=services,
            project_id=project_id,
            region=region,
            count=len(services),
        )
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/cloud-run-services/{name}/stop", response_model=ContainerActionResponse)
async def delete_cloud_run_service_endpoint(
    name: str,
    current_user: User = Depends(require_permission("containers", "delete")),
):
    """Delete a Cloud Run service. (Cloud Run already scales to zero on idle —
    delete is the actionable lifecycle operation for a service you don't want.)"""
    from ..services import gcp_service
    from ..services.gcp_service import GCPError

    project_id = settings.gcp_project_id
    if not project_id:
        raise HTTPException(status_code=503, detail="GCP project not configured.")
    region = (
        getattr(settings, "gcp_ansible_cloud_run_region", "")
        or settings.gcp_region
        or "us-central1"
    )
    try:
        await gcp_service.delete_cloud_run_service(project_id, region, name)
        return ContainerActionResponse(ok=True, message="Cloud Run service deleted")
    except GCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
