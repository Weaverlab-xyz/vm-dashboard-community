"""
Curated tool registry for the chat assistant.

v1 was read-only. v2 adds a small, vetted set of mutating tools (VM start/stop,
job cancel) gated by a two-phase confirmation flow — the chat router emits a
`pending_action` SSE event, the UI shows Confirm/Cancel, and only on explicit
confirm does the dashboard endpoint fire.

To add a new tool: append a ChatTool to TOOLS. The chat router dispatches
purely by name, so no other code changes are needed.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class ChatTool:
    name: str
    description: str
    path_template: str
    method: str = "GET"                                    # "GET" | "POST" | "DELETE"
    query_params: list[str] = field(default_factory=list)  # allow-list of query-string fields
    path_params: list[str] = field(default_factory=list)   # names of {placeholders} in path_template
    body_params: list[str] = field(default_factory=list)   # allow-list of JSON body fields (POST only)
    parameters_schema: dict = field(default_factory=dict)  # JSON Schema for the function arguments
    requires_confirmation: bool = False                    # gate mutation behind explicit user click
    # Optional: renders "Stop VM 'web-01' (Hydra)" etc. for the confirm card. Receives raw args.
    describe_action: Optional[Callable[[dict], str]] = None


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def _describe_vm_start(args: dict) -> str:
    p = args.get("vmx_path", "?")
    return f"Start VM: {p}"


def _describe_vm_stop(args: dict) -> str:
    p = args.get("vmx_path", "?")
    return f"Stop VM: {p}"


def _describe_cancel_job(args: dict) -> str:
    j = args.get("job_id", "?")
    return f"Cancel job {j}"


TOOLS: list[ChatTool] = [
    # ── Read-only ────────────────────────────────────────────────────────────
    ChatTool(
        name="get_dashboard_stats",
        description=(
            "Returns aggregate VM counts: total, running, stopped, offline. "
            "Use this for ANY question about counts or totals of VMs. "
            "Examples: 'how many VMs are running', 'what's the VM count', "
            "'give me a status overview', 'how many machines do we have'."
        ),
        path_template="/api/vms/dashboard-stats",
        parameters_schema=_schema({}),
    ),
    ChatTool(
        name="list_vms",
        description=(
            "Returns the full list of virtual machines (name, path, workgroup, last-known "
            "power state). Use when the user wants details, names, or a listing — NOT for "
            "counts (use get_dashboard_stats for counts). "
            "Also use this when you need a VM's vmx_path before calling start_vm or stop_vm. "
            "Examples: 'list all VMs', 'show me the VMs in Hydra', 'what VMs do we have', "
            "'which machines are in the Weaverlab workgroup'."
        ),
        path_template="/api/vms",
        query_params=["workgroup"],
        parameters_schema=_schema({
            "workgroup": {
                "type": "string",
                "description": "Optional workgroup filter. Common values: 'Hydra', 'Weaverlab'.",
            },
        }),
    ),
    ChatTool(
        name="get_running_vms",
        description=(
            "Returns ONLY virtual machines currently in the 'running' power state. "
            "Faster than list_vms when the user only cares about what's powered on. "
            "Examples: 'what's running right now', 'which VMs are powered on', "
            "'show running machines', 'what's up'."
        ),
        path_template="/api/vms/running",
        parameters_schema=_schema({}),
    ),
    ChatTool(
        name="list_jobs",
        description=(
            "Returns background jobs (deployments, config-mgmt runs, image builds) with "
            "filters for status and workgroup. Each job has id, type, status, created_at, "
            "duration_seconds. Default page_size is 20, sorted newest first. "
            "Examples: 'show me failed jobs', 'what jobs are running', 'recent deploys', "
            "'jobs from this week' (use page_size=50 and filter client-side by created_at)."
        ),
        path_template="/api/jobs",
        query_params=["status", "workgroup", "page", "page_size"],
        parameters_schema=_schema({
            "status": {
                "type": "string",
                "enum": ["pending", "running", "completed", "failed", "cancelled"],
                "description": "Filter to a single status. Omit to see all statuses.",
            },
            "workgroup": {"type": "string", "description": "Filter to a single workgroup name."},
            "page": {"type": "integer", "minimum": 1, "description": "1-based page index, default 1."},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Items per page, default 20."},
        }),
    ),
    ChatTool(
        name="get_job",
        description=(
            "Fetch a single job's full details by its UUID. Use after list_jobs returned a "
            "job the user wants to drill into, or when the user pastes a job ID. "
            "Example: 'tell me more about job abc123…', 'why did that job fail'."
        ),
        path_template="/api/jobs/{job_id}",
        path_params=["job_id"],
        parameters_schema=_schema(
            {"job_id": {"type": "string", "description": "Job UUID, taken from list_jobs output or user input."}},
            required=["job_id"],
        ),
    ),
    ChatTool(
        name="list_aws_instances",
        description=(
            "Returns EC2 instances deployed via this dashboard, with live state pulled from "
            "AWS (running/stopped/terminated), public IP, instance type, and the deploying "
            "user. Only includes dashboard-deployed instances, not arbitrary EC2 in the account. "
            "Examples: 'show our EC2 instances', 'what's deployed in AWS', 'list cloud VMs', "
            "'which AWS instances are running'."
        ),
        path_template="/api/aws/instances",
        parameters_schema=_schema({}),
    ),
    ChatTool(
        name="list_aws_amis",
        description=(
            "Returns AMIs (Amazon Machine Images) owned by the account in the configured "
            "AWS region. Use when the user asks about available images for EC2 deploys. "
            "Examples: 'what AMIs do we have', 'list available AWS images', "
            "'show me our custom AMIs'."
        ),
        path_template="/api/aws/amis",
        parameters_schema=_schema({}),
    ),
    ChatTool(
        name="list_azure_vms",
        description=(
            "Returns Azure VMs deployed via this dashboard with live power state. "
            "Like list_aws_instances but for Azure. "
            "Examples: 'show Azure VMs', 'what's running in Azure', 'list our cloud VMs in Azure'."
        ),
        path_template="/api/azure/vms",
        parameters_schema=_schema({}),
    ),
    ChatTool(
        name="list_azure_images",
        description=(
            "Returns private Azure images (Shared Image Gallery + standalone managed images) "
            "available for VM deployment. Does NOT include public Marketplace images. "
            "Examples: 'list Azure images', 'what private images do we have in Azure', "
            "'show me our image gallery'."
        ),
        path_template="/api/azure/images",
        parameters_schema=_schema({}),
    ),
    ChatTool(
        name="list_containers",
        description=(
            "Returns containers from the Portainer environments the user has access to "
            "(name, image, state, host). Covers both on-prem Docker hosts and cloud-deployed "
            "containers. "
            "Examples: 'list containers', 'what's running in Portainer', "
            "'show docker containers', 'which containers are up'."
        ),
        path_template="/api/containers",
        parameters_schema=_schema({}),
    ),

    # ── Mutating (require user confirmation in UI before executing) ──────────
    ChatTool(
        name="start_vm",
        description=(
            "Power ON a single VMware VM by its vmx_path. The vmx_path is the absolute path "
            "to the .vmx file and is returned by list_vms. Always call list_vms first if you "
            "don't already have the path — do NOT invent or guess a path. "
            "This requires user confirmation in the UI before it actually runs. "
            "Examples: 'start web-01', 'power on the Hydra DB VM', 'boot up vm foo'."
        ),
        path_template="/api/vms/start",
        method="POST",
        body_params=["vmx_path", "ip_wait_timeout"],
        parameters_schema=_schema(
            {
                "vmx_path": {
                    "type": "string",
                    "description": "Absolute path to the VM's .vmx file, taken verbatim from list_vms output.",
                },
                "ip_wait_timeout": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 600,
                    "description": "Seconds to wait for the VM to acquire an IP. Default 120. Omit unless user specifies.",
                },
            },
            required=["vmx_path"],
        ),
        requires_confirmation=True,
        describe_action=_describe_vm_start,
    ),
    ChatTool(
        name="stop_vm",
        description=(
            "Power OFF a single VMware VM by its vmx_path (graceful shutdown). The vmx_path "
            "is returned by list_vms — call list_vms first if you don't have the path. "
            "This requires user confirmation in the UI before it actually runs. "
            "Examples: 'stop web-01', 'shut down the Hydra DB VM', 'power off vm foo'."
        ),
        path_template="/api/vms/stop",
        method="POST",
        body_params=["vmx_path"],
        parameters_schema=_schema(
            {
                "vmx_path": {
                    "type": "string",
                    "description": "Absolute path to the VM's .vmx file, taken verbatim from list_vms output.",
                },
            },
            required=["vmx_path"],
        ),
        requires_confirmation=True,
        describe_action=_describe_vm_stop,
    ),
    ChatTool(
        name="cancel_job",
        description=(
            "Cancel a pending or running background job by its UUID. Use when the user asks "
            "to stop, kill, or cancel a job they started. Only the job's creator or an admin "
            "can cancel it — if the user lacks permission the tool returns 403 and you should "
            "tell them so rather than retrying. "
            "This requires user confirmation in the UI before it actually runs. "
            "Examples: 'cancel job abc123', 'kill that running deploy', 'stop the pending job'."
        ),
        path_template="/api/jobs/{job_id}",
        method="DELETE",
        path_params=["job_id"],
        parameters_schema=_schema(
            {"job_id": {"type": "string", "description": "Job UUID from list_jobs output or user input."}},
            required=["job_id"],
        ),
        requires_confirmation=True,
        describe_action=_describe_cancel_job,
    ),
]


TOOLS_BY_NAME: dict[str, ChatTool] = {t.name: t for t in TOOLS}


def to_ollama_tool_spec(tool: ChatTool) -> dict:
    """Render a ChatTool as an Ollama/OpenAI-style `tools[]` entry."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters_schema,
        },
    }


def all_tool_specs() -> list[dict]:
    return [to_ollama_tool_spec(t) for t in TOOLS]


def resolve_request(
    tool: ChatTool, arguments: dict
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """
    Given validated arguments, return (path, query_params_dict, json_body_or_None).

    Path params are substituted into path_template; query/body params are
    filtered to the tool's allow-lists so the model can't smuggle extra fields.
    `json_body` is None for GET/DELETE tools without a body.
    """
    path = tool.path_template
    for name in tool.path_params:
        val = arguments.get(name)
        if val is None:
            raise ValueError(f"tool {tool.name}: missing required path arg {name!r}")
        path = path.replace("{" + name + "}", str(val))

    query = {
        k: arguments[k]
        for k in tool.query_params
        if k in arguments and arguments[k] is not None
    }

    body: dict[str, Any] | None = None
    if tool.body_params:
        body = {
            k: arguments[k]
            for k in tool.body_params
            if k in arguments and arguments[k] is not None
        }

    return path, query, body


def describe(tool: ChatTool, arguments: dict) -> str:
    """Human-readable summary of the action for the confirm card."""
    if tool.describe_action:
        try:
            return tool.describe_action(arguments)
        except Exception:
            pass
    return f"{tool.name}({arguments})"
