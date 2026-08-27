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

⚠️  APPLICATION NAMES: ``application.name`` must be the **lowercased** display
    name of an application in the tenant's Entitle catalog. The entitleio/entitle
    v3 provider validates this field client-side at plan time — a value with any
    uppercase letter fails immediately with "Lowercase Validation Failed / value
    must be all lowercase" (before any API call). At apply time the provider then
    case-insensitively matches the lowercase value against the catalog, so the
    words must also be right or you get a 404
    ``{"errorId":"resource.notFound","message":"Application not found"}``.
    This tenant's ``entitle_applications`` data source returns human display names
    (``SSH Ephemeral Accounts``, ``Postgres``, ``MySql``, ``Microsoft SQL Server``,
    ``Kubernetes``, ``Rancher``) — so ``_APP_SLUG`` holds those names LOWERCASED.
    Confirm against the ``entitle_applications`` data source for your tenant (note
    the cloud-specific variants ``SSH Standing Accounts`` / ``GCP Postgres`` exist
    too) and adjust ``_APP_SLUG`` if they differ.

    ``connection_json`` keys are application-specific and DIFFER PER DB ENGINE
    (see ``_db_connection_json_hcl``), matching Entitle's connector docs:
      - postgresql: host, port, username, password, [database]  (ephemeral accounts)
      - mssql:      server ("host,port"), user, password, [database], [version]  (ephemeral accounts)
      - mysql:      host, port, user, password, [mysql_version]  (persistent roles, NOT ephemeral)
    The mssql ``server`` vs separate ``port`` split and the exact version keys
    should be confirmed against the tenant before first live use.
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
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Terraform binary — baked into the Docker image at build time.
_TERRAFORM = os.environ.get("TERRAFORM_EXECUTABLE", "terraform")

# Provider plugin cache written at image-build time so containers never need to
# download the provider at runtime (same dir the entitleio/sra providers use).
_PLUGIN_CACHE_DIR = os.environ.get("TF_PLUGIN_CACHE_DIR", "/root/.terraform.d/plugin-cache")

# engine / kind → Entitle application catalog name, **lowercased**. These are the
# display names returned by this tenant's `entitle_applications` data source
# (`SSH Ephemeral Accounts`, `Postgres`, `MySql`, `Microsoft SQL Server`,
# `Kubernetes`) lowercased, because the entitleio/entitle v3 provider validates
# `application.name` as all-lowercase at plan time (an uppercase letter → instant
# "Lowercase Validation Failed") and then case-insensitively matches the catalog
# at apply time (a wrong name → 404 "Application not found"). The SSH name is
# overridable via `entitle_ssh_app_slug` (parallel to `entitle_rancher_app_slug`)
# for tenants whose catalog differs.
_APP_SLUG = {
    "rest":       "rest api",          # ⚠️ tenant-specific — see _rest_app_slug()
    "ssh":        "ssh ephemeral accounts",
    "postgres":   "postgres",
    "mysql":      "mysql",
    "sqlserver":  "microsoft sql server",
    "oracle":     "oracle database",   # OCI Autonomous DB; confirm against the tenant catalog (name varies)
    "kubernetes": "kubernetes",
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


# Everything an Entitle registration reads that differs per TENANT. Resolved once into a
# plain dict and threaded through, because unlike the PRA and Password Safe services these
# values reach the run through two different channels — the api key rides TF_VAR_*, while
# the endpoint, owner, workflow and agent name are written into the HCL itself. A single
# override on `_tf_env` would therefore have covered the credential and silently left the
# *destination* pointing at the install's own tenant.
#
# ``None`` means "read the configured singletons", which is every existing caller.
CTX_KEYS = ("api_key", "endpoint", "owner_id", "workflow_id", "agent_token_name",
            "ssh_sudo_user")


def tenant_ctx(*, api_key: str, endpoint: str = "", owner_id: str = "",
               workflow_id: str = "", agent_token_name: str = "",
               ssh_sudo_user: str = "") -> dict:
    """Build the per-tenant context the functions below accept. One spelling."""
    return {"api_key": api_key, "endpoint": endpoint, "owner_id": owner_id,
            "workflow_id": workflow_id, "agent_token_name": agent_token_name,
            "ssh_sudo_user": ssh_sudo_user}


def _hcl_fields(ctx: Optional[dict]) -> dict:
    """The tenant fields the HCL generators need — and **never the API key.**

    The generators below turn these into a file on disk. The key does not belong in that
    file (it rides ``TF_VAR_entitle_api_key``, which is the whole point of the
    ``variable`` block), and handing a credential-bearing dict to a function whose job is
    to write text is how it ends up in one by a later edit. So the split is structural
    rather than a convention: a generator cannot render a secret it was never given.
    """
    return {"endpoint": _ctx(ctx, "endpoint"),
            "owner_id": _ctx(ctx, "owner_id"),
            "workflow_id": _ctx(ctx, "workflow_id"),
            "agent_token_name": _ctx(ctx, "agent_token_name")}


def _ctx(ctx: Optional[dict], key: str) -> str:
    """One field, from the tenant context when there is one.

    A context with **no api key** is refused rather than falling back: registering a
    customer's host into the install's own Entitle tenant is the silent cross-tenant
    mistake the registry exists to prevent, and it would look like success.
    """
    if ctx is not None:
        if not str(ctx.get("api_key") or "").strip():
            raise EntitleRegistrationError(
                "an Entitle tenant context was supplied with no API key. Refusing rather "
                "than falling back to the configured tenant.")
        return str(ctx.get(key) or "").strip()
    if key == "api_key":
        return _api_key()
    return _cfg({"endpoint": "entitle_endpoint", "owner_id": "entitle_owner_id",
                 "workflow_id": "entitle_workflow_id",
                 "agent_token_name": "entitle_agent_token_name",
                 "ssh_sudo_user": "entitle_ssh_sudo_user"}[key])


def _tf_env(extra_vars: Optional[dict] = None, ctx: Optional[dict] = None) -> dict:
    """Environment for Terraform calls. Secrets are passed as TF_VAR_* so the
    HCL template never contains them in plain text."""
    env = dict(os.environ)
    env["TF_PLUGIN_CACHE_DIR"] = _PLUGIN_CACHE_DIR
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["TF_CLI_ARGS"] = "-no-color"

    key = _ctx(ctx, "api_key")
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


def _common_attrs_hcl(private: bool, *, allow_creating_accounts: bool = True,
                      allow_changing_account_permissions: Optional[bool] = None,
                      fields: Optional[dict] = None) -> str:
    """The required owner/workflow blocks + allowed_durations, plus the
    ``agent_token`` block **only for private targets**.

    Public infrastructure is reachable from Entitle's cloud directly, so it
    registers with no agent (no Kubernetes cluster needed). Private targets
    (our PRA-only VMs / private RDS) require the shared Entitle agent — raise if
    one isn't configured so the operator provisions it first. Always raises if
    owner/workflow are unset (an integration can't be created without them).

    ``allow_creating_accounts`` is the **ephemeral-account** switch — Entitle mints
    a short-lived account/role on the target per grant. Defaults ``True`` (SSH /
    Kubernetes / Rancher all use it); the MySQL DB path passes ``False`` because
    Entitle's MySQL connector assigns persistent roles rather than ephemeral
    accounts.

    ``allow_changing_account_permissions`` is app-specific and OMITTED by default
    (``None``) so we don't disturb apps that accept the provider's default of
    ``true`` — e.g. the Kubernetes connector, live-validated with it unset. The
    **SSH Ephemeral Accounts** app rejects that default (API 400 "This application
    restricts changing accounts permissions"), so the SSH path passes ``False``."""
    fields = fields or {}
    owner_id = fields.get("owner_id") or _cfg("entitle_owner_id")
    workflow_id = fields.get("workflow_id") or _cfg("entitle_workflow_id")
    if not owner_id:
        raise EntitleRegistrationError("entitle_owner_id is not configured")
    if not workflow_id:
        raise EntitleRegistrationError("entitle_workflow_id is not configured")
    agent_block = ""
    if private:
        agent = fields.get("agent_token_name") or _cfg("entitle_agent_token_name")
        if not agent:
            raise EntitleRegistrationError(
                "private target requires entitle_agent_token_name — provision the "
                "Entitle agent (Kubernetes) first, or register only public resources"
            )
        agent_block = f"  agent_token = {{ name = {json.dumps(agent)} }}\n"
    changing_line = ""
    if allow_changing_account_permissions is not None:
        changing_line = (
            f"  allow_changing_account_permissions = "
            f"{str(bool(allow_changing_account_permissions)).lower()}\n"
        )
    return (
        f"  owner    = {{ id = {json.dumps(owner_id)} }}\n"
        f"  workflow = {{ id = {json.dumps(workflow_id)} }}\n"
        f"{agent_block}"
        f"  allowed_durations       = {_durations_hcl()}\n"
        f"  allow_creating_accounts = {str(bool(allow_creating_accounts)).lower()}\n"
        f"{changing_line}"
    )


# ── HCL generation ────────────────────────────────────────────────────────────
#
# One `entitle_integration` resource per call. `connection_json` is the
# application-specific connection config — emitted with jsonencode() so the
# sensitive TF_VARs (ssh_private_key / db_password) interpolate without ever
# being written to the HCL file on disk.

def _provider_endpoint(fields: Optional[dict] = None) -> str:
    """Endpoint for the entitleio/entitle provider. Prefer an explicit ``entitle_endpoint``;
    otherwise derive it from the shared ``entitle_api_url`` normalized to scheme+host (the
    provider appends its own version paths, so a ``/v1`` base would double-version). Blank →
    the provider's built-in default (https://api.entitle.io)."""
    ep = (fields or {}).get("endpoint") or _cfg("entitle_endpoint")
    if ep:
        return ep.rstrip("/")
    api_url = _cfg("entitle_api_url")
    if api_url:
        from urllib.parse import urlsplit
        parts = urlsplit(api_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return ""


def _provider_header(extra_vars: str = "", fields: Optional[dict] = None) -> str:
    endpoint = _provider_endpoint(fields)
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


def _generate_ssh_hcl(*, name: str, hostname: str, sudo_user: str, port: int, private: bool,
                      fields: Optional[dict] = None) -> str:
    label = _safe_name(name)
    header = _provider_header('variable "ssh_private_key" { sensitive = true }\n', fields)
    # connection_json for the "SSH Ephemeral Accounts" connector is host/key/user
    # (see docs.beyondtrust.com/entitle/docs/entitle-integration-ssh_ephemeral_accounts);
    # the private key is `key`, NOT `privateKey`, and there is no `port` field.
    app_name = _cfg("entitle_ssh_app_slug") or _APP_SLUG["ssh"]
    return header + f"""
resource "entitle_integration" {json.dumps(label)} {{
  name        = {json.dumps(name[:50])}
  application = {{ name = {json.dumps(app_name)} }}
  connection_json = jsonencode({{
    host = {json.dumps(hostname)}
    user = {json.dumps(sudo_user)}
    key  = var.ssh_private_key
  }})
{_common_attrs_hcl(private, allow_changing_account_permissions=False, fields=fields)}}}

output "integration_id" {{
  value = entitle_integration.{label}.id
}}
"""


def _db_connection_json_hcl(*, engine: str, host: str, port: int,
                            username: str, database: str, version: str) -> str:
    """Emit the ``connection_json = jsonencode({...})`` block with the
    **engine-correct** connection keys. ``password`` stays a raw ``var.db_password``
    reference (interpolated by jsonencode at apply time) so the secret never lands
    in the HCL on disk — which is why this is built as an HCL string, not a dict.

    Per Entitle's connector docs the key names differ by engine:
      - postgresql: host, port, user,     password, options{}   (NO top-level database)
      - mysql:      host, port, user,     password, [mysql_version]
      - mssql:      server (host[,port]), user, password, [database], [version]
    """
    lines: list[str] = []
    if engine == "sqlserver":
        # The mssql connector takes `server` (host[,port]) + `user`; no separate `port`.
        server = f"{host},{port}" if port else host
        lines.append(f"    server   = {json.dumps(server)}")
        lines.append(f"    user     = {json.dumps(username)}")
        lines.append("    password = var.db_password")
        if database:
            lines.append(f"    database = {json.dumps(database)}")
        if version:
            lines.append(f"    version  = {json.dumps(version)}")
    elif engine == "mysql":
        lines.append(f"    host     = {json.dumps(host)}")
        lines.append(f"    port     = {port}")
        lines.append(f"    user     = {json.dumps(username)}")
        lines.append("    password = var.db_password")
        if version:
            lines.append(f"    mysql_version = {json.dumps(version)}")
    else:  # postgres
        # Entitle's Postgres connector schema is {user, password, host, port,
        # options{resource_types_constraints, databases_constraints}}. It expects
        # `user` — NOT `username` — and has NO top-level `database` field; sending
        # either makes the payload fail schema matching with API 400 "Didn't find
        # matching connection schema". Unlike the MySQL and SQL Server connectors,
        # the Postgres connector's canonical config ALWAYS carries a top-level
        # `options` object, and omitting it likewise fails the schema match — so we
        # emit it with empty constraint arrays (no resource/database scoping; the
        # ephemeral role gets the connector's default access). Scope to specific
        # databases via `options.databases_constraints`, not a top-level `database`.
        # `database` is accepted here for signature parity with the other engines
        # but is intentionally unused for postgres.
        # See docs.beyondtrust.com/entitle/docs/entitle-integration-postgressql
        lines.append(f"    host     = {json.dumps(host)}")
        lines.append(f"    port     = {port}")
        lines.append(f"    user     = {json.dumps(username)}")
        lines.append("    password = var.db_password")
        lines.append("    options = {")
        lines.append("      resource_types_constraints = []")
        lines.append("      databases_constraints      = []")
        lines.append("    }")
    body = "\n".join(lines)
    return f"  connection_json = jsonencode({{\n{body}\n  }})\n"


def _generate_db_hcl(*, engine: str, name: str, host: str, port: int,
                     username: str, database: str, version: str, private: bool) -> str:
    slug = _APP_SLUG.get(engine)
    if not slug or engine == "ssh":
        raise EntitleRegistrationError(
            f"DB registration for engine {engine!r} not supported "
            f"(supported: postgres, mysql, sqlserver)"
        )
    label = _safe_name(name)
    header = _provider_header('variable "db_password" { sensitive = true }\n')
    conn = _db_connection_json_hcl(engine=engine, host=host, port=port,
                                   username=username, database=database, version=version)
    # Ephemeral (JIT) accounts for postgres/sqlserver; mysql assigns persistent roles.
    allow_creating = engine != "mysql"
    return header + f"""
resource "entitle_integration" {json.dumps(label)} {{
  name        = {json.dumps(name[:50])}
  application = {{ name = {json.dumps(slug)} }}
{conn}{_common_attrs_hcl(private, allow_creating_accounts=allow_creating)}}}

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


def _generate_rancher_hcl(*, name: str, url: str, verify: bool, private: bool) -> str:
    """Entitle **Rancher** integration. connection_json = {url, access_token,
    secret_key, verify} — Rancher's API access+secret key PAIR (sensitive TF_VARs;
    see docs.beyondtrust.com/entitle/docs/entitle-integration-rancher). ``private``
    (the Rancher server isn't reachable from Entitle's cloud — the internal-LB case)
    attaches the shared agent_token via _common_attrs_hcl."""
    label = _safe_name(name)
    slug = _cfg("entitle_rancher_app_slug") or "rancher"
    header = _provider_header(
        'variable "rancher_access_token" { sensitive = true }\n'
        'variable "rancher_secret_key" { sensitive = true }\n')
    conn = (
        "  connection_json = jsonencode({\n"
        f"    url          = {json.dumps(url)}\n"
        "    access_token = var.rancher_access_token\n"
        "    secret_key   = var.rancher_secret_key\n"
        f"    verify       = {str(bool(verify)).lower()}\n"
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
    """Run one terraform subcommand in ``work_dir``.

    ``init`` is serialized on the shared plugin cache via ``terraform.plugin_cache_lock``:
    the tempdir is per-call but TF_PLUGIN_CACHE_DIR is the single cache baked into the
    image, and parallel inits race to place the same provider binary (ETXTBSY). Same
    reasoning as terraform_pra_service._run_tf — see the longer note there."""
    def _go() -> subprocess.CompletedProcess:
        return subprocess.run(
            [_TERRAFORM] + args,
            cwd=work_dir, capture_output=True, text=True, timeout=timeout, env=env,
        )

    if args and args[0] == "init":
        from .terraform import plugin_cache_lock
        with plugin_cache_lock():
            return _go()
    return _go()


_REDACTED = "**REDACTED-BY-DASHBOARD**"

# Attribute names whose value is a secret, and the keys inside a `connection_json` blob
# that are. The second list is what makes this different from the Password Safe and PRA
# scrubbers: an Entitle integration keeps its credential INSIDE a JSON-encoded string
# attribute, so redacting attribute names alone reaches nothing.
#
# `key` is the SSH private key ("the private key is `key`, NOT `privateKey`" — see
# _generate_ssh_hcl); the rest are the database, REST and Kubernetes connectors' own
# spellings. Redacting a name no connector uses costs nothing, so this list errs wide.
_SECRET_ATTRS = ("password", "private_key", "passphrase", "token", "secret")
_SECRET_JSON_KEYS = ("key", "privateKey", "private_key", "password", "token",
                     "secret", "clientSecret", "apiKey")


def _scrub_state(tf_state_json: Optional[str]) -> Optional[str]:
    """Redact secret values from a Terraform state before it is stored.

    Terraform records sensitive attributes in state as **plaintext**, and this module's
    states are stashed in the database — `pov_environment_vms.entitle_tf_state`, a
    cloud-database job row, an `app_config` key. So an SSH integration's state would
    otherwise hold the private key it was created with, at rest, for the life of the POV.

    Destroy is by **id** against a provider-only config (see :func:`_destroy_sync`), so
    the values are not needed to tear the integration down — the same argument
    ``ps_resource_service._scrub_state`` and ``terraform_pra_service._scrub_tf_state``
    make.

    Fails **closed**: on any parse error the state is dropped rather than stored with a
    plaintext secret in it. That costs an automated teardown and leaves an integration to
    remove by hand, which is the better of the two failures.

    Deliberately NOT applied to the agent-token mint — see :func:`_agent_token_from_state`,
    which depends on that one state staying intact.
    """
    if not tf_state_json:
        return None
    try:
        state = json.loads(tf_state_json)
        for res in state.get("resources") or []:
            for inst in res.get("instances") or []:
                attrs = inst.get("attributes") or {}
                for name in _SECRET_ATTRS:
                    if attrs.get(name):
                        attrs[name] = _REDACTED
                blob = attrs.get("connection_json")
                if isinstance(blob, str) and blob.strip():
                    attrs["connection_json"] = _scrub_connection_json(blob)
        return json.dumps(state)
    except Exception as exc:  # noqa: BLE001
        logger.error("entitle: failed to scrub Terraform state — dropping it; the "
                     "integration may need removing by hand at teardown: %s", exc)
        return None


def _chmod_600(path: Path) -> None:
    """Best-effort owner-only permissions. A no-op on Windows, where the mode bits do not
    mean what they do on POSIX and the failure to set them is not worth an exception."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        logger.debug("entitle: could not chmod %s", path)


def _scrub_connection_json(blob: str) -> str:
    """Redact the secret-bearing keys inside a ``connection_json`` string.

    Returns the blob unchanged when it is not a JSON object — a connector this build does
    not know about should not have its configuration mangled, and the outer scrub has
    already redacted anything at attribute level.
    """
    try:
        conn = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return blob
    if not isinstance(conn, dict):
        return blob
    for name in _SECRET_JSON_KEYS:
        if conn.get(name):
            conn[name] = _REDACTED
    return json.dumps(conn)


def _apply_hcl_sync(hcl: str, tf_vars: dict, ctx: Optional[dict] = None,
                    scrub: bool = True) -> dict:
    """Write HCL, init+apply, return ``{integration_id, outputs, tf_state_json}``.

    ``outputs`` is the full ``terraform output`` map (values unwrapped); ``integration_id``
    is kept as a convenience for the registration callers."""
    env = _tf_env(tf_vars, ctx)
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
        if scrub:
            tf_state_json = _scrub_state(tf_state_json)
        integration_id = str(outputs.get("integration_id") or "") or None
        return {"integration_id": integration_id, "outputs": outputs, "tf_state_json": tf_state_json}


def _destroy_sync(tf_state_json: str, ctx: Optional[dict] = None) -> None:
    """Restore stored state and ``terraform destroy`` the integration.

    A resource present in state but absent from configuration is destroyed by
    ``terraform destroy``, so only the provider block is needed here — no need to
    reconstruct the full resource (and its now-rotated secrets) from state.

    **The state is scrubbed again on the way in**, and that is not belt-and-braces. Two
    real cases reach here with secrets intact: a row written before :func:`_scrub_state`
    existed, and any caller handing over a state this module did not produce. Since the
    destroy works from resource **ids**, redacting first means no credential is written to
    disk at all — which is the whole of what the sink below could otherwise leak.
    """
    try:
        json.loads(tf_state_json)
    except json.JSONDecodeError as e:
        raise EntitleRegistrationError(f"tf_state_json is not valid JSON: {e}") from e

    safe_state = _scrub_state(tf_state_json)
    if not safe_state:
        # _scrub_state only returns None on a parse failure, which the check above has
        # already ruled out — so this is unreachable in practice and refusing is still
        # right: writing the unscrubbed original as a fallback is exactly the behaviour
        # this function is avoiding.
        raise EntitleRegistrationError(
            "the stored Entitle state could not be prepared for destroy; remove the "
            "integration in Entitle by hand.")

    env = _tf_env(None, ctx)
    with tempfile.TemporaryDirectory(prefix="entitle_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(_provider_header("", _hcl_fields(ctx)))
        state_file = Path(work_dir, "terraform.tfstate")
        state_file.write_text(safe_state)
        # 0600 as well as the 0700 TemporaryDirectory around it. Terraform rewrites this
        # file as it works and does not preserve the mode, so this is about the window
        # before it does rather than a lasting guarantee — cheap, and the file is a
        # state document on a host that may have other tenants.
        _chmod_600(state_file)
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
    ctx: Optional[dict] = None,
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
                            port=port, private=private, fields=_hcl_fields(ctx))
    return await asyncio.to_thread(_apply_hcl_sync, hcl,
                                   {"ssh_private_key": private_key}, ctx)


async def register_database(
    *, engine: str, name: str, host: str, port: int, username: str,
    password: str, database: str = "", version: str = "",
    private: bool = True, tag: str = "vm-dashboard",
) -> dict:
    """Register a managed database as an Entitle DB integration
    (PostgreSQL / MySQL / Microsoft SQL Server). ``private`` controls whether an
    ``agent_token`` is attached (``False`` = publicly reachable, no agent).

    ``version`` is the engine version the connector wants (mysql ``mysql_version`` /
    mssql ``version``); optional and omitted from the connection_json when empty
    (postgres needs none). postgres/sqlserver register with ephemeral-account
    creation enabled; mysql uses persistent role assignment."""
    if not password:
        raise EntitleRegistrationError("DB service-account password is empty")
    hcl = _generate_db_hcl(engine=engine, name=name, host=host, port=port,
                           username=username, database=database, version=version,
                           private=private)
    return await asyncio.to_thread(_apply_hcl_sync, hcl, {"db_password": password})


# ── REST integration (Entitle Remote Adapter) ────────────────────────────────
#
# Registers a Cloud Functions adapter as an Entitle REST integration. This is the
# path for every target Entitle has no native connector for, and for the ones whose
# connector cannot do what we need — MySQL (persistent roles only) and the managed
# SQL Server flavors (its ephemeral accounts assume a server-level login plus USE,
# which is not how Azure SQL Database works).
#
# The adapter's contract is documented at
# docs.beyondtrust.com/entitle/docs/open-api-definition; the routes below match
# what web_dashboard/functions/fnworkloads/db_grant.py serves.

# Entitle validates connection_json against a per-MODE JSON schema with
# additionalProperties: false, so these are NOT a base plus extras: a Standing-mode
# key in an Ephemeral payload is rejected outright ("must NOT have additional
# properties"). The Terraform provider does not run that validation, which is how
# the mixed payload this used to emit created an integration that saved cleanly and
# then failed every resource sync with "Missing host scope!" — passing at the only
# moment anyone was watching. One tuple per mode, nothing shared.
#
# Ephemeral is a shorter LIFECYCLE, not just a shorter list. Entitle never calls
# give_access or revoke_access in that mode: create_actor IS the grant and
# delete_actor is the revoke, which is what db_grant._create_actor implements.
_REST_STANDING_ROUTES = (
    ("get_assets_path", "/get_assets"),
    ("get_actors_path", "/get_actors"),
    ("get_all_permissions_path", "/get_all_permissions"),
    ("give_access_path", "/give_access"),
    ("revoke_access_path", "/revoke_access"),
)

# No get_actors_path on purpose: in Ephemeral mode Entitle owns the account
# lifecycle and tracks its own actors, and sending the key fails validation.
_REST_EPHEMERAL_ROUTES = (
    ("get_assets_path", "/get_assets"),
    ("get_all_permissions_path", "/get_all_permissions"),
    ("create_actor_path", "/create_actor"),
    ("delete_actor_path", "/delete_actor"),
)


def _rest_routes(ephemeral: bool) -> tuple:
    """The route fields this mode accepts — see the note above on why it is either
    one set or the other and never a union."""
    return _REST_EPHEMERAL_ROUTES if ephemeral else _REST_STANDING_ROUTES


def _rest_app_slug() -> str:
    """The catalog name of the REST application, lowercased.

    ⚠️  TENANT-SPECIFIC, and unconfirmed against a live catalog — the same caveat
        as the DB/SSH slugs above. The entitleio/entitle provider validates this
        client-side as all-lowercase at plan time, then case-insensitively matches
        the catalog at apply time, so a wrong value fails as a 404
        "Application not found" rather than anything more helpful. Check your
        tenant's ``entitle_applications`` data source and set
        ``entitle_rest_app_slug`` if it differs from the default.
    """
    return (_cfg("entitle_rest_app_slug") or _APP_SLUG["rest"]).strip().lower()


def _split_base_url(base_url: str) -> tuple:
    """``(schema, host, prefix)`` from the adapter's endpoint.

    Entitle takes either a full URL per path field or ``schema`` + ``host`` with
    relative paths. The split form is used because the full-URL form is what a live
    tenant answered "Missing host scope!" to, and because the host is then one value
    to eyeball rather than the same string repeated across seven fields.

    ``prefix`` is any path the endpoint itself carries, and it stays on the front of
    every route: Azure serves under ``/api``, so dropping it would 404 every call.
    Preserving it is the whole reason the full-URL form looked attractive.
    """
    # strip() first: a whitespace-only value is truthy and would generate paths like
    # "   /give_access" that fail only at the first real grant. The trailing slash is
    # dropped from the PATH after parsing rather than from the whole string before
    # it — "https://".rstrip("/") is "https:", which urlsplit then reads as a host.
    base = (base_url or "").strip()
    if not base:
        raise EntitleRegistrationError("REST registration needs the adapter's base URL")
    parts = urlsplit(base if "://" in base else f"https://{base}")
    if not parts.netloc:
        raise EntitleRegistrationError(
            "REST registration needs a host in the adapter's base URL "
            f"(got {base_url!r})")
    return (parts.scheme or "https"), parts.netloc, parts.path.rstrip("/")


def _rest_connection_json_hcl(*, base_url: str, ephemeral: bool,
                              auth_header: str) -> str:
    """``connection_json`` for a REST integration, in that mode's own key set.

    The bearer secret is referenced as ``var.rest_secret`` rather than interpolated,
    so it never lands in the HCL written to disk — the same discipline the DB path
    uses for ``db_password``.
    """
    schema, host, prefix = _split_base_url(base_url)
    lines = [f"    schema = {json.dumps(schema)}",
             f"    host   = {json.dumps(host)}"]
    lines += [f"    {field} = {json.dumps(prefix + path)}"
              for field, path in _rest_routes(ephemeral)]
    # Token auth: Entitle sends these verbatim on every request, which is exactly
    # what fnruntime.auth verifies. The alternative the docs offer (oauth_data) buys
    # nothing here — the adapter has no OAuth server in front of it.
    lines.append(
        f"    headers = {{ {json.dumps(auth_header)} = \"Bearer ${{var.rest_secret}}\" }}")
    body = "\n".join(lines)
    return f"  connection_json = jsonencode({{\n{body}\n  }})\n"


def _generate_rest_hcl(*, name: str, base_url: str, private: bool,
                       ephemeral: bool, auth_header: str) -> str:
    label = _safe_name(name)
    header = _provider_header('variable "rest_secret" { sensitive = true }\n')
    conn = _rest_connection_json_hcl(base_url=base_url, ephemeral=ephemeral,
                                     auth_header=auth_header)
    # allow_creating_accounts follows `ephemeral` directly. Note this is where the
    # MySQL limitation goes away: the constraint was never MySQL's, it was Entitle's
    # MySQL CONNECTOR's, and a REST adapter does not use that connector.
    return header + f"""
resource "entitle_integration" {json.dumps(label)} {{
  name        = {json.dumps(name[:50])}
  application = {{ name = {json.dumps(_rest_app_slug())} }}
{conn}{_common_attrs_hcl(private, allow_creating_accounts=ephemeral)}}}

output "integration_id" {{
  value = entitle_integration.{label}.id
}}
"""


async def register_rest(*, name: str, base_url: str, shared_secret: str,
                        private: bool = False, ephemeral: bool = True,
                        auth_header: str = "Authorization") -> dict:
    """Register a Cloud Functions adapter as an Entitle REST integration.

    ``base_url`` is the function's endpoint with no route on it — the adapter routes
    on the path, so Entitle appends ``/give_access`` and friends.

    ``private`` defaults to **False**, unlike the other register_* helpers: the
    whole point of the adapter is that it is an internet-reachable endpoint Entitle
    can call directly, even when the resource behind it is private. It is the
    FUNCTION that is VPC-attached, not the integration. Pass ``private=True`` only
    if the function's own ingress is restricted and Entitle needs the agent.

    ``ephemeral`` selects Entitle's **Ephemeral Accounts** connection mode: the
    create_actor/delete_actor route set (see _REST_EPHEMERAL_ROUTES) plus
    ``allow_creating_accounts``. Those two have to agree — a payload carrying the
    ephemeral routes with ``allow_creating_accounts = false`` describes a lifecycle
    Entitle will not run, and the reverse describes one it cannot.

    ⚠️  Entitle's UI picks the mode from an explicit **Connection** dropdown (four
        options: Standing/Ephemeral × get_all_permissions/get_asset_permissions).
        Whether the API takes a discriminator of its own, or infers the mode from
        ``allow_creating_accounts`` and the key set as this assumes, is UNCONFIRMED
        against a live tenant. If a generated integration comes back showing
        "Standing Accounts" in that dropdown, the discriminator is real and belongs
        here — check the integration's Settings after the first registration.

    Returns ``{integration_id, tf_state_json}``; stash the state so ``deregister``
    can remove it.
    """
    if not shared_secret:
        raise EntitleRegistrationError(
            "REST registration needs the adapter's shared secret — without it "
            "every call Entitle makes would be rejected by the function")
    hcl = _generate_rest_hcl(name=name, base_url=base_url, private=private,
                             ephemeral=ephemeral, auth_header=auth_header)
    return await asyncio.to_thread(_apply_hcl_sync, hcl, {"rest_secret": shared_secret})


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


async def register_rancher(*, name: str, server_url: str, api_token: str,
                           verify: bool = False, private: bool = True) -> dict:
    """Register the central Rancher as an Entitle **Rancher** integration. Rancher's
    API bearer (``token-xxxxx:yyyyy``) IS the access+secret key pair the connector
    wants — split on ``:``. ``private`` (internal Rancher, unreachable from Entitle's
    cloud) attaches the shared agent_token. Returns {integration_id, tf_state_json};
    stash the state so :func:`deregister` can remove it."""
    access, _sep, secret = (api_token or "").partition(":")
    if not (access and secret):
        raise EntitleRegistrationError(
            "Rancher api_token must be a Rancher API key pair 'access:secret' (e.g. token-xxxxx:yyyyy)")
    hcl = _generate_rancher_hcl(name=name, url=server_url, verify=verify, private=private)
    return await asyncio.to_thread(
        _apply_hcl_sync, hcl,
        {"rancher_access_token": access, "rancher_secret_key": secret})


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


def _agent_token_from_state(tf_state_json: str) -> tuple:
    """Recover ``(token, name)`` from a previous mint's stored ``terraform.tfstate``.

    Entitle returns the token value only at creation, but Terraform records sensitive
    outputs and attributes in state as PLAINTEXT — which is why the Password Safe path
    scrubs a ``token`` attribute before stashing state (``ps_resource_service._scrub_state``)
    and why :func:`ensure_agent_token` deliberately does not. Keeping it intact is what
    makes the mint recoverable: the ref can resolve empty while this state survives (an
    external secrets-backend ref whose secret was deleted, a cleared ``entitle/agent-token``
    row, a partially restored config store), and a second mint under the same name is a hard
    ``400 Resource already exists`` from Entitle. Returns ``("", "")`` when there is nothing
    to recover — every failure here is non-fatal, the caller just falls through to minting."""
    if not tf_state_json:
        return "", ""
    try:
        state = json.loads(tf_state_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning("entitle_agent_token_tf_state is not valid JSON — cannot recover the token")
        return "", ""
    if not isinstance(state, dict):
        return "", ""
    token, name = "", ""
    out = (state.get("outputs") or {}).get("token")
    if isinstance(out, dict) and out.get("value"):
        token = str(out["value"])
    # The name lives only on the resource; the token is here too when the state was
    # captured without the output block.
    for res in state.get("resources") or []:
        if res.get("type") != "entitle_agent_token":
            continue
        for inst in res.get("instances") or []:
            attrs = inst.get("attributes") or {}
            token = token or str(attrs.get("token") or "")
            name = name or str(attrs.get("name") or "")
    return token, name


async def mint_agent_token(name: str) -> dict:
    """Mint a fresh Entitle Agent token via the provider. Returns ``{token, tf_state_json}``.

    The token value is returned only at creation — stash it immediately. Requires the
    provider key (``entitle_api_key`` / ``entitle_api_token``). Stash ``tf_state_json`` so
    the token can later be destroyed/rotated via :func:`deregister`."""
    if not _api_key():
        raise EntitleRegistrationError(
            "entitle_api_key (or entitle_api_token) is not configured — cannot mint an agent token")
    try:
        # `scrub=False` is load-bearing rather than an oversight: Entitle returns an agent
        # token's value only at creation, and `_agent_token_from_state` recovers it from
        # this state when the stored ref resolves empty. Redacting it here would turn a
        # recoverable mint into a hard `400 Resource already exists` on the next attempt.
        res = await asyncio.to_thread(_apply_hcl_sync, _agent_token_hcl(name), {},
                                      None, False)
    except EntitleRegistrationError as exc:
        # Entitle rejects a duplicate agent-token NAME. We always apply into an empty
        # workdir, so this means the tenant already holds that name while we hold no
        # copy of its value — unrecoverable here (create-only secret, no data source),
        # so spell out the remedies: the job page renders error_message and nothing else.
        if "already exists" not in str(exc).lower():
            raise
        raise EntitleRegistrationError(
            f"an Entitle Agent token named '{name}' already exists in the tenant, but this "
            "dashboard holds no copy of its value. Entitle returns the value only at "
            "creation, so it cannot be read back and re-minting the same name is refused. "
            "Fix by one of: delete that token in Entitle and retry (this breaks any agent "
            "still using it); set ENTITLE_AGENT_TOKEN_NAME to an unused name; or set "
            "ENTITLE_AGENT_TOKEN_REF to the existing token value. Neither key is editable "
            "in the Settings panel — both are env/.env only."
        ) from exc
    token = (res.get("outputs") or {}).get("token")
    if not token:
        raise EntitleRegistrationError("agent-token mint returned no 'token' output")
    return {"token": str(token), "tf_state_json": res.get("tf_state_json")}


async def ensure_agent_token(name: str = "") -> str:
    """Return the Entitle agent token value, minting + persisting one if none exists.

    If ``entitle_agent_token_ref`` already resolves to a value, return it. If it does not
    but a previous mint's ``entitle_agent_token_tf_state`` is still stored for the SAME
    name, recover the value from that state (:func:`_agent_token_from_state`) and restore
    the ref — re-minting an existing name is refused by Entitle, so recovery is the only
    idempotent path. A *different* requested name skips recovery: that is how an operator
    forces a fresh token.

    Otherwise mint a token, stash the value in the encrypted config store, and record the
    ref (``entitle_agent_token_ref`` → ``config://entitle/agent-token``), the name
    (``entitle_agent_token_name``, reused for private-target registration), and the mint's
    ``terraform.tfstate`` (``entitle_agent_token_tf_state``) for later destroy/rotation."""
    from . import config_service
    existing = _resolve_token_ref(_cfg("entitle_agent_token_ref"))
    if existing:
        return existing
    # The ref resolved empty. Before minting — which Entitle refuses outright if a token
    # of this name already exists — try to recover the value from the previous mint's
    # state, and restore the ref/name the mint would have written.
    token_name = name or _cfg("entitle_agent_token_name") or "vm-dashboard-agent"
    recovered, recovered_name = _agent_token_from_state(_cfg("entitle_agent_token_tf_state"))
    if recovered and recovered_name and recovered_name != token_name:
        # Asking for a different name is how an operator forces a FRESH token (the
        # documented way out of an unrecoverable name conflict). Stale state must not
        # silently win that argument.
        logger.info("stored agent-token state is for '%s' but '%s' was requested — minting instead",
                    recovered_name, token_name)
        recovered = ""
    if recovered:
        config_service.set(_AGENT_TOKEN_CONFIG_KEY, recovered)
        config_service.set("entitle_agent_token_ref", f"config://{_AGENT_TOKEN_CONFIG_KEY}")
        if recovered_name and not _cfg("entitle_agent_token_name"):
            config_service.set("entitle_agent_token_name", recovered_name)
        logger.info("Entitle agent token recovered from stored terraform state (name=%s) — no mint",
                    recovered_name or "unknown")
        return recovered
    minted = await mint_agent_token(token_name)
    config_service.set(_AGENT_TOKEN_CONFIG_KEY, minted["token"])
    config_service.set("entitle_agent_token_ref", f"config://{_AGENT_TOKEN_CONFIG_KEY}")
    config_service.set("entitle_agent_token_name", token_name)
    if minted.get("tf_state_json"):
        config_service.set("entitle_agent_token_tf_state", minted["tf_state_json"])
    logger.info("Entitle agent token minted + stashed (name=%s)", token_name)
    return minted["token"]


async def destroy_agent_token() -> str:
    """Destroy the auto-minted Entitle Agent token and clear its stash. Returns the
    destroyed token's name ("" when there was nothing of ours to destroy).

    Entitle refuses to re-mint an existing name and the value can never be read back,
    so a token that outlives the agent it bootstrapped wedges every future install on
    an unrecoverable "already exists". Call this whenever the agent's lifecycle ends —
    the ``remove`` action and the decommission of its hosting cluster.

    Only OUR mint is destroyed: the stored ``entitle_agent_token_tf_state`` is the
    proof of ownership, so an operator-supplied token (``entitle_agent_token_ref``
    pre-set, no state) is never touched. On a failed destroy the stash is KEPT — the
    state is the only remaining handle on the tenant-side token (a retry can destroy
    it, and the next install can still recover the value); clearing it would recreate
    exactly the orphan this function exists to prevent."""
    from . import config_service
    state = _cfg("entitle_agent_token_tf_state")
    if not state:
        return ""
    _, name = _agent_token_from_state(state)
    await asyncio.to_thread(_destroy_sync, state)
    config_service.set("entitle_agent_token_tf_state", "")
    config_service.set(_AGENT_TOKEN_CONFIG_KEY, "")
    config_service.set("entitle_agent_token_name", "")
    # Only un-point the ref when it points at our stash — an external/operator ref
    # (aws_sm://…, a custom key) stays theirs even after our mint is gone.
    if config_service.get("entitle_agent_token_ref") == f"config://{_AGENT_TOKEN_CONFIG_KEY}":
        config_service.set("entitle_agent_token_ref", "")
    logger.info("Entitle agent token destroyed (name=%s) — the name is free to re-mint",
                name or "unknown")
    return name or "unknown"


async def deregister(tf_state_json: str, ctx: Optional[dict] = None) -> None:
    """Destroy a previously registered Entitle integration using its stored state.

    ``ctx`` must be the SAME tenant it was registered against — a destroy pointed at
    another authenticates fine, removes nothing, and reports success."""
    await asyncio.to_thread(_destroy_sync, tf_state_json, ctx)
