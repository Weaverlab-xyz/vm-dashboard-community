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
    status: str = ""        # RUNNING | PENDING (finished jobs are not listed)
    created_at: Optional[str] = None


class CloudRunJobListResponse(BaseModel):
    jobs: list[CloudRunJobInfo]
    project_id: str
    count: int


# ── GCP Rancher management node (COS on GCE) ────────────────────────────────

class RancherNodeInfo(BaseModel):
    name: str
    cloud: str = "gcp"     # which cloud the node is in (aws|azure|gcp)
    zone: str              # GCE zone / AWS AZ / Azure location the node landed in
    status: str  # RUNNING | TERMINATED | STOPPING | PROVISIONING | ...
    machine_type: str = ""
    image: str = ""
    internal_ip: str = ""
    external_ip: str = ""
    url: str = ""          # https://<external_ip>
    created_at: Optional[str] = None


class RancherNodeResponse(BaseModel):
    nodes: list[RancherNodeInfo]
    cloud: str = "gcp"     # the cloud the node is hosted on
    account: str = ""      # cloud-neutral "where": GCP project / AWS account region scope / Azure subscription
    # Legacy alias for `account`, kept because the Containers page still reads it.
    # Blank on a non-GCP node rather than misreporting a project that doesn't exist.
    project_id: str
    count: int
    configured: bool       # node cloud credentials + bootstrap password present
    server_url: str = ""   # the pinned rancher_server_url (if the node is bootstrapped)
    login_hint: str = ""   # how to log in (username + which configured password); never the secret itself


class RancherDeployRequest(BaseModel):
    # Deploy-time cloud pick. Blank → the persisted node cloud (gcp for installs that
    # predate this field). Rancher is a SINGLE management plane, so choosing a
    # different cloud RELOCATES the node rather than adding a second one.
    cloud: Optional[str] = None              # aws | azure | gcp
    # Deploy-time region pick (multi-region). Blank → the persisted node region, else
    # the configured default. zone is optional within the region (blank → the region's
    # first available zone, with same-region capacity fallback) and is GCP-only —
    # on AWS the subnet pins the AZ and Azure has no zone in this shape.
    region: Optional[str] = None             # region for the Rancher node
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


# ── Managed Portainer CE server (COS on GCE) ────────────────────────────────

class PortainerNodeInfo(BaseModel):
    name: str
    cloud: str = "gcp"     # which cloud the node is in (aws|azure|gcp)
    zone: str              # GCE zone / AWS AZ / Azure location the node landed in
    status: str  # RUNNING | TERMINATED | STOPPING | PROVISIONING | ...
    machine_type: str = ""
    image: str = ""
    internal_ip: str = ""
    external_ip: str = ""
    url: str = ""          # https://<external_ip>:9443
    created_at: Optional[str] = None


class PortainerNodeResponse(BaseModel):
    nodes: list[PortainerNodeInfo]
    cloud: str = "gcp"     # the cloud the node is hosted on
    account: str = ""      # cloud-neutral "where": GCP project / AWS account / Azure subscription
    # Legacy alias for `account`, kept because the Containers page still reads it.
    project_id: str
    count: int
    configured: bool       # node cloud credentials present (all a deploy needs — no pre-set secret)
    server_url: str = ""   # the pinned portainer_url (if a node is deployed)
    token_configured: bool = False  # a portainer_pat is stored, so the Containers tab can talk to it
    login_hint: str = ""   # how to log in; the auto-generated password is echoed, an operator-set one never is
    # Durable state. The teardown confirmation has to differ on this: with a data disk
    # the users/environments/settings SURVIVE, without one they are gone for good.
    data_disk_enabled: bool = False  # /data is on a persistent disk that outlives the VM
    data_disk_name: str = ""         # that disk's name (blank when durable state is off)
    # Set when the stored URL no longer matches the running node: Edge keys are derived
    # from the node URL, so every agent joined before the change has stopped checking in.
    stale_edge_keys: str = ""


class PortainerDeployRequest(BaseModel):
    # Deploy-time cloud pick. Blank → the persisted node cloud (gcp for installs that
    # predate this field). One Portainer server manages many Docker hosts, so choosing
    # a different cloud RELOCATES the node rather than adding a second one.
    cloud: Optional[str] = None              # aws | azure | gcp
    # Deploy-time region pick (multi-region). Blank → the persisted node region, else
    # the configured default. zone is optional within the region (blank → the region's
    # first available zone, with same-region capacity fallback) and is GCP-only.
    region: Optional[str] = None             # region for the Portainer node
    zone: Optional[str] = None               # optional GCP zone within `region`
    # Deploy-time PRA choices (parity with the Rancher node). All optional — omitted
    # fields fall back to Settings/config. jump_group + jumpoint by NAME, vault
    # account group by numeric id (the list_pickers() contract).
    web_jump_enabled: bool = False           # broker the Portainer UI via a PRA Web Jump
    jump_group: Optional[str] = None         # PRA Jump Group name
    jumpoint_name: Optional[str] = None      # PRA Jumpoint name
    vault_account_group_id: Optional[int] = None  # PRA Vault account group for the admin credential


class PortainerEdgeRequest(BaseModel):
    """Register an Edge-agent environment on the configured Portainer."""
    name: str                                # environment name shown in Portainer
    agent_image: Optional[str] = None        # blank -> portainer/agent:latest
    checkin_interval: Optional[int] = None   # seconds; blank -> Portainer's default (5)


class PortainerEdgeResponse(BaseModel):
    """Everything needed to join the host, including the one-time Edge key.

    The key is derived from the node's CURRENT url and is never stored — a node whose
    external IP changes invalidates every key issued before the change.
    """
    endpoint_id: int
    name: str
    edge_id: str
    edge_key: str                            # shown once; not persisted anywhere
    server_url: str                          # the address the agent will dial
    tunnel_port: int                         # 8000, already open in the node firewall
    join_command: str                        # docker run ... to paste on the host


class PortainerImportRequest(BaseModel):
    """A Portainer migration bundle, produced by
    ``python -m web_dashboard.scripts.portainer_migrate export``.

    base64-in-JSON rather than multipart, matching the one upload shape this app
    already has (``api/storage.py``). The route enforces a size cap and validates the
    decoded document before queuing anything.
    """
    filename: str = ""                       # for the job result only; not trusted as a path
    content_b64: str                         # the bundle JSON, base64-encoded
    # Stacks are DEPLOYED, not stored — Portainer has no save-without-running call —
    # so they are skipped unless the operator names an environment that already
    # exists. Environment connections themselves are never imported.
    endpoint_id: Optional[int] = None        # target environment for the bundle's stacks
