"""
Pydantic models for the Containers (Portainer CE) section.
"""
from typing import Optional
from pydantic import BaseModel


class PortainerEndpoint(BaseModel):
    id: int
    name: str
    url: str
    status: int  # 1=up, 2=down


class PortainerEndpointList(BaseModel):
    endpoints: list[PortainerEndpoint]


class ContainerInfo(BaseModel):
    id: str           # full 64-char container ID
    short_id: str     # first 12 chars
    names: list[str]  # Docker names (usually ["/name"])
    image: str
    status: str       # human-readable e.g. "Up 2 hours", "Exited (0) 3 minutes ago"
    state: str        # "running" | "exited" | "paused" | "created" | ...
    ports: list[str]  # formatted "0.0.0.0:8080->80/tcp"
    created: int      # unix timestamp


class ContainerListResponse(BaseModel):
    containers: list[ContainerInfo]
    count: int


class PortMapping(BaseModel):
    host: int
    container: int
    protocol: str = "tcp"


class EnvVar(BaseModel):
    key: str
    value: str


class DeployContainerRequest(BaseModel):
    endpoint_id: int
    name: str
    image: str                          # "nginx:latest" or "registry.example.com/app:v1"
    ports: list[PortMapping] = []
    env: list[EnvVar] = []
    restart_policy: str = "unless-stopped"


class DeployContainerResponse(BaseModel):
    job_id: str
    status: str
    message: str


class StackInfo(BaseModel):
    id: int
    name: str
    status: int      # 1=active, 2=inactive
    type: int        # 1=swarm, 2=standalone compose
    endpoint_id: int


class StackListResponse(BaseModel):
    stacks: list[StackInfo]
    count: int


class DeployStackRequest(BaseModel):
    endpoint_id: int
    name: str
    compose_content: str
    env: list[EnvVar] = []


class DeployStackResponse(BaseModel):
    job_id: str
    status: str
    message: str


class ContainerActionResponse(BaseModel):
    ok: bool
    message: str


class ECSTaskInfo(BaseModel):
    task_arn: str
    task_id: str           # short UUID at end of ARN
    cluster: str
    task_definition: str   # "family:revision"
    last_status: str       # RUNNING | STOPPED | PENDING | DEPROVISIONING
    desired_status: str
    containers: list[str]  # "name (image)" strings
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    cpu: str = ""
    memory: str = ""


class ECSTaskListResponse(BaseModel):
    tasks: list[ECSTaskInfo]
    cluster: str
    count: int


class ACIContainerInstanceInfo(BaseModel):
    container_group_id: str
    container_group_name: str
    resource_group: str
    state: str                    # Running, Stopped, Succeeded, Failed, etc.
    os_type: str = "Linux"        # Linux | Windows
    cpu: float = 0.0
    memory: float = 0.0
    containers: list[str] = []    # "name (image)" strings
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    restart_policy: str = "OnFailure"


class ACIContainerListResponse(BaseModel):
    containers: list[ACIContainerInstanceInfo]
    resource_group: str
    count: int


# ── GCP Jumpoint container instances (COS on GCE) ───────────────────────────

class GCEJumpointInfo(BaseModel):
    name: str
    zone: str
    status: str  # RUNNING | TERMINATED | STOPPING | PROVISIONING | ...
    machine_type: str = ""
    image: str = ""
    internal_ip: str = ""
    external_ip: str = ""
    created_at: Optional[str] = None


class GCEJumpointListResponse(BaseModel):
    instances: list[GCEJumpointInfo]
    project_id: str
    count: int
