"""
Entitle resource registration via the entitleio/entitle Terraform provider.

As the dashboard builds infrastructure it registers each resource into Entitle
as its own integration, so end-users can request just-in-time access in Entitle:

  - a Linux cloud VM  → an SSH **ephemeral-accounts** integration
  - a cloud database  → a PostgreSQL / MySQL / Microsoft SQL Server integration
  - (future) an EKS/AKS/GKE cluster → a Kubernetes integration

Shaped like ``terraform_pra_service`` (which manages the PRA Shell Jump / DB
tunnel): inline HCL written to an ephemeral workdir, ``terraform apply``, the new
integration's id pulled from outputs, and the full ``terraform.tfstate`` returned
so a later ``deregister`` can ``terraform destroy`` it. Secrets are passed as
``TF_VAR_*`` so they never land in the HCL on disk. The provider plugin is
pre-cached in ``$TF_PLUGIN_CACHE_DIR`` at image-build time (no runtime download).

Provider/resource schema confirmed against entitleio/entitle v3 docs
(``entitle_integration`` resource): required ``name``, ``application = { name }``,
``connection_json``, ``owner = { id }``, ``workflow = { id }``,
``allowed_durations``; optional ``agent_token = { name }`` for private/internal
systems (our private RDS / PRA-only VMs need this). See
https://registry.terraform.io/providers/entitleio/entitle/latest/docs/resources/integration

Required settings (config_service / .env):
  entitle_api_key            entitleio/entitle provider key (falls back to entitle_api_token)
  entitle_owner_id           UUID of the Entitle user who owns created integrations
  entitle_workflow_id        UUID of the default approval workflow for created integrations
Optional:
  entitle_endpoint           API base (default https://api.entitle.io)
  entitle_agent_token_name   name of an Entitle Agent token for private connectivity
  entitle_allowed_durations  comma list of seconds (default "3600,43200,86400")

⚠️  APPLICATION SLUGS: ``application.name`` is a lowercase slug from Entitle's
    catalog. ``postgresql`` is confirmed from the provider docs; ``mysql`` /
    ``mssql`` / ``ssh`` are best-effort — confirm against the ``entitle_applications``
    data source for your tenant and adjust ``_APP_SLUG`` if they differ. The
    ``connection_json`` keys are likewise application-specific (the DB shape is
    confirmed; the SSH shape follows the BeyondTrust SSH-ephemeral docs).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Terraform binary — baked into the Docker image at build time.
_TERRAFORM = os.environ.get("TERRAFORM_EXECUTABLE", "terraform")

# Provider plugin cache written at image-build time so containers never need to
# download the provider at runtime (same dir the entitleio/sra providers use).
_PLUGIN_CACHE_DIR = os.environ.get("TF_PLUGIN_CACHE_DIR", "/root/.terraform.d/plugin-cache")

# engine / kind → Entitle application catalog slug (lowercase). `postgresql` is
# confirmed from provider docs; the rest are best-effort — confirm via the
# entitle_applications data source for your tenant.
_APP_SLUG = {
    "ssh":        "ssh",
    "postgres":   "postgresql",
    "mysql":      "mysql",
    "sqlserver":  "mssql",
    "kubernetes": "Kubernetes",
}

_DEFAULT_DURATIONS = "3600,43200,86400"  # 1h, 12h, 24h (all valid Entitle values)


class EntitleRegistrationError(Exception):
    """Raised when an Entitle registration Terraform operation fails."""


def _cfg(key: str) -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, "") or ""


def _api_key() -> str:
    """Provider key for entitleio/entitle; fall back to the shared API token."""
    return _cfg("entitle_api_key") or _cfg("entitle_api_token")


def _tf_env(extra_vars: Optional[dict] = None) -> dict:
    """Environment for Terraform calls. Secrets are passed as TF_VAR_* so the
    HCL template never contains them in plain text."""
    env = dict(os.environ)
    env["TF_PLUGIN_CACHE_DIR"] = _PLUGIN_CACHE_DIR
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["TF_CLI_ARGS"] = "-no-color"

    key = _api_key()
    if key:
        env["TF_VAR_entitle_api_key"] = key
    for var, val in (extra_vars or {}).items():
        if val is not None:
            env[f"TF_VAR_{var}"] = str(val)
    return env


def _safe_name(name: str) -> str:
    """A Terraform-identifier-safe slug for the resource label."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower()) or "resource"


def _durations_hcl() -> str:
    raw = _cfg("entitle_allowed_durations") or _DEFAULT_DURATIONS
    nums = [p.strip() for p in str(raw).split(",") if p.strip()]
    return "[" + ", ".join(nums) + "]"


def _common_attrs_hcl(private: bool) -> str:
    """The required owner/workflow blocks + allowed_durations, plus the
    ``agent_token`` block **only for private targets**.

    Public infrastructure is reachable from Entitle's cloud directly, so it
    registers with no agent (no Kubernetes cluster needed). Private targets
    (our PRA-only VMs / private RDS) require the shared Entitle agent — raise if
    one isn't configured so the operator provisions it first. Always raises if
    owner/workflow are unset (an integration can't be created without them)."""
    owner_id = _cfg("entitle_owner_id")
    workflow_id = _cfg("entitle_workflow_id")
    if not owner_id:
        raise EntitleRegistrationError("entitle_owner_id is not configured")
    if not workflow_id:
        raise EntitleRegistrationError("entitle_workflow_id is not configured")
    agent_block = ""
    if private:
        agent = _cfg("entitle_agent_token_name")
        if not agent:
            raise EntitleRegistrationError(
                "private target requires entitle_agent_token_name — provision the "
                "Entitle agent (Kubernetes) first, or register only public resources"
            )
        agent_block = f"  agent_token = {{ name = {json.dumps(agent)} }}\n"
    return (
        f"  owner    = {{ id = {json.dumps(owner_id)} }}\n"
        f"  workflow = {{ id = {json.dumps(workflow_id)} }}\n"
        f"{agent_block}"
        f"  allowed_durations       = {_durations_hcl()}\n"
        f"  allow_creating_accounts = true\n"
    )


# ── HCL generation ────────────────────────────────────────────────────────────
#
# One `entitle_integration` resource per call. `connection_json` is the
# application-specific connection config — emitted with jsonencode() so the
# sensitive TF_VARs (ssh_private_key / db_password) interpolate without ever
# being written to the HCL file on disk.

def _provider_endpoint() -> str:
    """Endpoint for the entitleio/entitle provider. Prefer an explicit ``entitle_endpoint``;
    otherwise derive it from the shared ``entitle_api_url`` normalized to scheme+host (the
    provider appends its own version paths, so a ``/v1`` base would double-version). Blank →
    the provider's built-in default (https://api.entitle.io)."""
    ep = _cfg("entitle_endpoint")
    if ep:
        return ep.rstrip("/")
    api_url = _cfg("entitle_api_url")
    if api_url:
        from urllib.parse import urlsplit
        parts = urlsplit(api_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return ""


def _provider_header(extra_vars: str = "") -> str:
    endpoint = _provider_endpoint()
    endpoint_line = f'  endpoint = {json.dumps(endpoint)}\n' if endpoint else ""
    return f"""\
terraform {{
  required_providers {{
    entitle = {{
      source  = "entitleio/entitle"
      version = "~> 3.0"
    }}
  }}
}}

variable "entitle_api_key" {{ sensitive = true }}
{extra_vars}
provider "entitle" {{
  api_key = var.entitle_api_key
{endpoint_line}}}
"""


def _generate_ssh_hcl(*, name: str, hostname: str, sudo_user: str, port: int, private: bool) -> str:
    label = _safe_name(name)
    header = _provider_header('variable "ssh_private_key" { sensitive = true }\n')
    return header + f"""
resource "entitle_integration" {json.dumps(label)} {{
  name        = {json.dumps(name[:50])}
  application = {{ name = {json.dumps(_APP_SLUG["ssh"])} }}
  connection_json = jsonencode({{
    host       = {json.dumps(hostname)}
    port       = {port}
    user       = {json.dumps(sudo_user)}
    privateKey = var.ssh_private_key
  }})
{_common_attrs_hcl(private)}}}

output "integration_id" {{
  value = entitle_integration.{label}.id
}}
"""


def _generate_db_hcl(*, engine: str, name: str, host: str, port: int,
                     username: str, database: str, private: bool) -> str:
    slug = _APP_SLUG.get(engine)
    if not slug or engine == "ssh":
        raise EntitleRegistrationError(
            f"DB registration for engine {engine!r} not supported "
            f"(supported: postgres, mysql, sqlserver)"
        )
    label = _safe_name(name)
    header = _provider_header('variable "db_password" { sensitive = true }\n')
    db_line = f"    database = {json.dumps(database)}\n" if database else ""
    return header + f"""
resource "entitle_integration" {json.dumps(label)} {{
  name        = {json.dumps(name[:50])}
  application = {{ name = {json.dumps(slug)} }}
  connection_json = jsonencode({{
    host     = {json.dumps(host)}
    port     = {port}
    username = {json.dumps(username)}
    password = var.db_password
{db_line}  }})
{_common_attrs_hcl(private)}}}

output "integration_id" {{
  value = entitle_integration.{label}.id
}}
"""


def _generate_k8s_hcl(*, name: str, host: str, user_prefix: str, private: bool) -> str:
    """The generic Entitle **Kubernetes** integration (covers EKS/AKS/GKE via the K8s
    API). ``private`` = the API server isn't reachable from Entitle's cloud, so use
    **In-Cluster** access via the agent (``connection_json`` is just ``user_prefix``);
    otherwise **External Access** with host + a service-account token + CA."""
    label = _safe_name(name)
    slug = _APP_SLUG["kubernetes"]
    if private:
        header = _provider_header()
        conn = (
            "  connection_json = jsonencode({\n"
            f"    user_prefix = {json.dumps(user_prefix)}\n"
            "  })\n"
        )
    else:
        header = _provider_header(
            'variable "k8s_token" { sensitive = true }\n'
            'variable "k8s_ca_cert" { sensitive = true }\n')
        conn = (
            "  connection_json = jsonencode({\n"
            f"    host                = {json.dumps(host)}\n"
            "    token               = var.k8s_token\n"
            "    ssl_ca_cert_content = var.k8s_ca_cert\n"
            f"    user_prefix         = {json.dumps(user_prefix)}\n"
            "  })\n"
        )
    return header + f"""
resource "entitle_integration" {json.dumps(label)} {{
  name        = {json.dumps(name[:50])}
  application = {{ name = {json.dumps(slug)} }}
{conn}{_common_attrs_hcl(private)}}}

output "integration_id" {{
  value = entitle_integration.{label}.id
}}
"""


# ── Terraform plumbing ────────────────────────────────────────────────────────

def _run_tf(args: list, work_dir: str, env: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_TERRAFORM] + args,
        cwd=work_dir, capture_output=True, text=True, timeout=timeout, env=env,
    )


def _apply_hcl_sync(hcl: str, tf_vars: dict) -> dict:
    """Write HCL, init+apply, return ``{integration_id, outputs, tf_state_json}``.

    ``outputs`` is the full ``terraform output`` map (values unwrapped); ``integration_id``
    is kept as a convenience for the registration callers."""
    env = _tf_env(tf_vars)
    with tempfile.TemporaryDirectory(prefix="entitle_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(hcl)

        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise EntitleRegistrationError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")

        apply = _run_tf(["apply", "-auto-approve"], work_dir, env, timeout=120)
        if apply.returncode != 0:
            raise EntitleRegistrationError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}")

        out = _run_tf(["output", "-json"], work_dir, env, timeout=30)
        outputs: dict = {}
        if out.returncode == 0 and out.stdout.strip():
            try:
                outputs = {k: v.get("value") for k, v in json.loads(out.stdout).items()}
            except (json.JSONDecodeError, AttributeError):
                pass

        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        integration_id = str(outputs.get("integration_id") or "") or None
        return {"integration_id": integration_id, "outputs": outputs, "tf_state_json": tf_state_json}


def _destroy_sync(tf_state_json: str) -> None:
    """Restore stored state and ``terraform destroy`` the integration.

    A resource present in state but absent from configuration is destroyed by
    ``terraform destroy``, so only the provider block is needed here — no need to
    reconstruct the full resource (and its now-rotated secrets) from state."""
    try:
        json.loads(tf_state_json)
    except json.JSONDecodeError as e:
        raise EntitleRegistrationError(f"tf_state_json is not valid JSON: {e}") from e

    env = _tf_env()
    with tempfile.TemporaryDirectory(prefix="entitle_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(_provider_header())
        Path(work_dir, "terraform.tfstate").write_text(tf_state_json)
        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise EntitleRegistrationError(
                f"terraform init (destroy) failed: {init.stderr.strip() or init.stdout.strip()}")
        destroy = _run_tf(["destroy", "-auto-approve"], work_dir, env, timeout=120)
        if destroy.returncode != 0:
            raise EntitleRegistrationError(
                f"terraform destroy failed: {destroy.stderr.strip() or destroy.stdout.strip()}")


# ── Public async API ──────────────────────────────────────────────────────────

async def register_ssh_host(
    *, name: str, hostname: str, sudo_user: str, private_key: str,
    port: int = 22, private: bool = True, tag: str = "vm-dashboard",
) -> dict:
    """Register a Linux VM as an Entitle SSH ephemeral-accounts integration.

    ``private_key`` is the resolved PEM (callers resolve the configured
    ``entitle_ssh_private_key_ref``). ``private`` controls whether an
    ``agent_token`` is attached — pass ``False`` for a publicly reachable host
    (no agent/cluster needed). Returns ``{integration_id, tf_state_json}`` — stash
    ``tf_state_json`` so ``deregister`` can remove it on teardown. (``tag`` is
    accepted for call-site symmetry with the PRA service; the Entitle schema has
    no per-integration tag field.)
    """
    if not sudo_user:
        raise EntitleRegistrationError("entitle_ssh_sudo_user is not configured")
    if not private_key:
        raise EntitleRegistrationError("entitle_ssh_private_key_ref resolved empty")
    hcl = _generate_ssh_hcl(name=name, hostname=hostname, sudo_user=sudo_user,
                            port=port, private=private)
    return await asyncio.to_thread(_apply_hcl_sync, hcl, {"ssh_private_key": private_key})


async def register_database(
    *, engine: str, name: str, host: str, port: int, username: str,
    password: str, database: str = "", private: bool = True, tag: str = "vm-dashboard",
) -> dict:
    """Register a managed database as an Entitle DB integration
    (PostgreSQL / MySQL / Microsoft SQL Server). ``private`` controls whether an
    ``agent_token`` is attached (``False`` = publicly reachable, no agent)."""
    if not password:
        raise EntitleRegistrationError("DB service-account password is empty")
    hcl = _generate_db_hcl(engine=engine, name=name, host=host, port=port,
                           username=username, database=database, private=private)
    return await asyncio.to_thread(_apply_hcl_sync, hcl, {"db_password": password})


async def register_kubernetes(*, name: str, private: bool = True,
                              user_prefix: str = "entitle", host: str = "",
                              token: str = "", ca_cert: str = "",
                              tag: str = "k8s-cluster") -> dict:
    """Register a managed cluster (EKS/AKS/GKE) as an Entitle **Kubernetes** integration.

    ``private`` → In-Cluster access via the agent (only ``user_prefix`` needed; the
    agent must be installed). Otherwise External Access: ``host`` (API server) + a
    service-account ``token`` + ``ca_cert`` (PEM). Returns ``{integration_id,
    tf_state_json}`` — stash the state so ``deregister`` can remove it.
    """
    if not private and not (host and token):
        raise EntitleRegistrationError(
            "External-access Kubernetes registration needs host + a service-account token")
    hcl = _generate_k8s_hcl(name=name, host=host, user_prefix=user_prefix, private=private)
    tf_vars = {} if private else {"k8s_token": token, "k8s_ca_cert": ca_cert}
    return await asyncio.to_thread(_apply_hcl_sync, hcl, tf_vars)


# ── Agent token (bootstrap for the k8s agent + private-target registration) ─────
#
# The Entitle Agent token is sensitive and returned only at creation. We mint it
# with the entitleio/entitle ``entitle_agent_token`` resource (same provider/plumbing
# as the integrations above), stash the value in the encrypted config store, and record
# the ref + name so BOTH the k8s agent install (token VALUE) and private integrations
# (token NAME) can use it. See docs/design/entitle-resource-registration.md.

_AGENT_TOKEN_CONFIG_KEY = "entitle/agent-token"


def _agent_token_hcl(name: str) -> str:
    label = _safe_name(name)
    return _provider_header() + f"""
resource "entitle_agent_token" {json.dumps(label)} {{
  name = {json.dumps(name)}
}}

output "token" {{
  value     = entitle_agent_token.{label}.token
  sensitive = true
}}
"""


def _resolve_token_ref(ref: str) -> str:
    """Resolve an agent-token ref to its value: external backend (``aws_sm://`` …),
    ``config://<key>``, a bare config key, or an inline literal."""
    from . import config_service
    if not ref:
        return ""
    if config_service.is_reference(ref):
        return config_service.resolve_reference(ref)
    if ref.startswith("config://"):
        return config_service.get(ref[len("config://"):])
    return config_service.get(ref) or ref


async def mint_agent_token(name: str) -> dict:
    """Mint a fresh Entitle Agent token via the provider. Returns ``{token, tf_state_json}``.

    The token value is returned only at creation — stash it immediately. Requires the
    provider key (``entitle_api_key`` / ``entitle_api_token``). Stash ``tf_state_json`` so
    the token can later be destroyed/rotated via :func:`deregister`."""
    if not _api_key():
        raise EntitleRegistrationError(
            "entitle_api_key (or entitle_api_token) is not configured — cannot mint an agent token")
    res = await asyncio.to_thread(_apply_hcl_sync, _agent_token_hcl(name), {})
    token = (res.get("outputs") or {}).get("token")
    if not token:
        raise EntitleRegistrationError("agent-token mint returned no 'token' output")
    return {"token": str(token), "tf_state_json": res.get("tf_state_json")}


async def ensure_agent_token(name: str = "") -> str:
    """Return the Entitle agent token value, minting + persisting one if none exists.

    If ``entitle_agent_token_ref`` already resolves to a value, return it. Otherwise mint
    a token, stash the value in the encrypted config store, and record the ref
    (``entitle_agent_token_ref`` → ``config://entitle/agent-token``), the name
    (``entitle_agent_token_name``, reused for private-target registration), and the mint's
    ``terraform.tfstate`` (``entitle_agent_token_tf_state``) for later destroy/rotation."""
    from . import config_service
    existing = _resolve_token_ref(_cfg("entitle_agent_token_ref"))
    if existing:
        return existing
    token_name = name or _cfg("entitle_agent_token_name") or "vm-dashboard-agent"
    minted = await mint_agent_token(token_name)
    config_service.set(_AGENT_TOKEN_CONFIG_KEY, minted["token"])
    config_service.set("entitle_agent_token_ref", f"config://{_AGENT_TOKEN_CONFIG_KEY}")
    config_service.set("entitle_agent_token_name", token_name)
    if minted.get("tf_state_json"):
        config_service.set("entitle_agent_token_tf_state", minted["tf_state_json"])
    logger.info("Entitle agent token minted + stashed (name=%s)", token_name)
    return minted["token"]


async def deregister(tf_state_json: str) -> None:
    """Destroy a previously registered Entitle integration using its stored state."""
    await asyncio.to_thread(_destroy_sync, tf_state_json)
