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


class DeployComposeRequest(BaseModel):
    """Deploy a Docker Compose file (referenced from the storage backend) to a
    cloud container runtime — ECS Fargate, ACI container group, or GCE COS."""
    provider: str                       # "ecs" | "aci" | "gce"
    name: str                           # deployment name (task family / group / instance)
    compose_backend: str                # storage backend the compose file lives in
    compose_file: str                   # filename within that backend (.yml/.yaml)
    cpu: Optional[float] = None         # optional task/container CPU override (vCPU)
    memory_mb: Optional[int] = None     # optional task/container memory override (MiB)
    overrides: dict = {}                # optional target overrides (cluster/subnet/zone/…)


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


# ── GCP Cloud Run runner jobs (Ansible / promote / k8s) ─────────────────────

class CloudRunJobInfo(BaseModel):
    name: str
    region: str
    purpose: str = ""       # ansible-runner | promote-runner | k8s-runner
    image: str = ""
    status: str = ""        # RUNNING | PENDING | COMPLETED
    created_at: Optional[str] = None


class CloudRunJobListResponse(BaseModel):
    jobs: list[CloudRunJobInfo]
    project_id: str
    count: int


# ── GCP Rancher management node (COS on GCE) ────────────────────────────────

class RancherNodeInfo(BaseModel):
    name: str
    zone: str
    status: str  # RUNNING | TERMINATED | STOPPING | PROVISIONING | ...
    machine_type: str = ""
    image: str = ""
    internal_ip: str = ""
    external_ip: str = ""
    url: str = ""          # https://<external_ip>
    created_at: Optional[str] = None


class RancherNodeResponse(BaseModel):
    nodes: list[RancherNodeInfo]
    project_id: str
    count: int
    configured: bool       # GCP project + bootstrap password present
    server_url: str = ""   # the pinned rancher_server_url (if the node is bootstrapped)
    login_hint: str = ""   # how to log in (username + which configured password); never the secret itself


class RancherDeployRequest(BaseModel):
    # Deploy-time region pick (multi-region). Blank → the persisted node region, else
    # the configured default. zone is optional within the region (blank → the region's
    # first available zone, with same-region capacity fallback).
    region: Optional[str] = None             # GCP region for the Rancher node
    zone: Optional[str] = None               # optional GCP zone within `region`
    # Deploy-time PRA choices (parity with DB/VM deploys). All optional — omitted
    # fields fall back to Settings/config. jump_group + jumpoint by NAME, vault
    # account group by numeric id (the list_pickers() contract).
    web_jump_enabled: bool = False           # broker the Rancher UI via a PRA Web Jump
    jump_group: Optional[str] = None         # PRA Jump Group name
    jumpoint_name: Optional[str] = None      # PRA Jumpoint name
    vault_account_group_id: Optional[int] = None  # PRA Vault account group for the admin credential


class RancherImportRequest(BaseModel):
    name: str              # cluster name to create in Rancher


class RancherImportResponse(BaseModel):
    cluster_id: str
    manifest_url: str
    apply_command: str     # kubectl apply -f <manifest_url> (run against the target cluster)
