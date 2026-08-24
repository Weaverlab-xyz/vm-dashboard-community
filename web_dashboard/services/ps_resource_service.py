"""
BeyondTrust Password Safe resource registration via the BeyondTrust/passwordsafe
Terraform provider.

Optional, per-VM-deploy add-on (mirrors entitle_registration_service.py): when an
operator opts in, a freshly built VM is onboarded into Password Safe as a **managed
system** with **one managed account** — the ``adminuser`` account the bt-ready
provisioners baked into the image.

Onboarding shapes (``method`` on register_managed_system):
  - ``ssh`` — traditional managed system keyed by host_name/ip on an SSH platform; the
    VM's own private key is pushed and SSH key enforcement manages it (needs SSH
    line-of-sight, i.e. a Resource Broker / Jumpoint per VPC).
  - ``ssm`` — the cloud-native "AWS Systems Manager" Password Safe custom plugin:
    Password Safe manages the Linux EC2 instance over AWS SSM SendCommand, so the
    managed system carries ``dns_name = {instance-id}:{region}`` and the account name
    follows ``{name};{suffix}``; no private key is pushed (Change Password mints it).
  - ``azurevm`` — the cloud-native "Azure VM SSH Rotation" Password Safe custom plugin:
    Password Safe writes the key onto the VM via Azure VM Run Command, so the managed
    system carries ``dns_name = tenantId/subscriptionId/resourceGroup/vmName`` and the
    account name is the plain Linux user (``adminuser``); no private key is pushed
    (Change Password mints it).
  - ``gcpvm`` — the cloud-native "GCP VM SSH Rotation" Password Safe custom plugin:
    Password Safe writes the public key into the GCE instance's ``ssh-keys`` metadata
    (the guest agent propagates it to ``authorized_keys``), so the managed system carries
    ``dns_name = projectId/zone/instanceName`` and the account name is the plain Linux
    user (``adminuser``); no private key is pushed (Change Password mints it). ``ssm``,
    ``azurevm`` and ``gcpvm`` are the cloud-API "plugin" methods (see ``_PLUGIN_METHODS``)
    — no SSH reachability required.

Shaped like entitle_registration_service / terraform_pra_service: inline HCL written
to an ephemeral workdir, ``terraform apply``, ids pulled from outputs, the full
``terraform.tfstate`` returned (scrubbed of secrets) so a later ``deregister`` can
``terraform destroy`` it. Secrets ride ``TF_VAR_*`` so they never land in the HCL.

Auth reuses the Password Safe OAuth client the ps-cli / public-API integration is
configured with, plus the provider-required run-as user:
  pscli_api_url            provider ``url``
  pscli_client_id          provider ``client_id``
  pscli_client_secret      provider ``client_secret``
  pscli_api_account_name   provider ``api_account_name`` (REQUIRED run-as user)

Provider/resource schema confirmed against BeyondTrust/passwordsafe v1.3.0:
  - provider requires url + api_account_name (client_id/client_secret for OAuth);
  - passwordsafe_managed_system_by_workgroup requires workgroup_id (string),
    entity_type_id (number), host_name, platform_id (number);
  - passwordsafe_managed_account requires account_name, system_name, and password
    (sensitive) — SSH-key management is expressed via private_key (+ passphrase) and
    dss_auto_management_flag, so we pass a generated placeholder password and let
    ssh_key_enforcement_mode on the system enforce key-only auth.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Terraform binary — baked into the Docker image at build time.
_TERRAFORM = os.environ.get("TERRAFORM_EXECUTABLE", "terraform")
# Provider plugin cache written at image-build time (no runtime download).
_PLUGIN_CACHE_DIR = os.environ.get("TF_PLUGIN_CACHE_DIR", "/root/.terraform.d/plugin-cache")

_REDACTED = "**REDACTED-BY-DASHBOARD**"

# Custom-plugin methods — Password Safe drives the target through a custom plugin
# (AWS SSM / Azure Run Command / GCP instance metadata / a DB client / the PRA Config
# API) rather than SSH, so the managed system carries the plugin's address in
# ``dns_name``/``host_name``, uses a placeholder ip, omits the SSH-only fields
# (remote_client_type / ssh_key_enforcement_mode), and pushes no private key. ``ssh``
# is the traditional method. ``ssm``/``azurevm``/``gcpvm`` are SSH-key-managed (dss
# auto-management on); ``dbssm`` (cloud-DB via the "{engine} SSM Custom Plugin"),
# ``dbazure`` (cloud-DB via the "{engine} Azure Run Command Plugin") and ``pravault``
# (the "PRA Vault Username Password" plugin) are PASSWORD-managed, so their account
# emits dss_auto_management_flag = false.
# ``k8ssa`` (the "Kubernetes Service Account Token" plugin) is password-managed too —
# there the "password" IS the ServiceAccount bearer token.
# ``dbgcp`` (cloud-DB via the "GCP Cloud SQL {engine}" plugins) is password-managed as
# well; unlike its two DB siblings it reaches the instance over Google's control plane
# rather than a jump host, so there is no key material anywhere in its address.
_PLUGIN_METHODS = frozenset({"ssm", "azurevm", "gcpvm", "dbssm", "dbazure", "dbgcp",
                             "pravault", "k8ssa"})
# Methods whose managed account is password-managed (no SSH DSS key auto-management).
_PASSWORD_MANAGED_METHODS = frozenset({"dbssm", "dbazure", "dbgcp", "pravault", "k8ssa"})

# Password Safe's managed-system address column is 255 chars. The cloud-DB plugin addresses
# pack 6-8 fields into it, and the Azure one is close to the ceiling with realistic values
# (two GUIDs, a flexible-server FQDN, a broker cert path). Over the limit the API rejects or
# truncates the address, and a truncated address fails inside the plugin later as an
# unparseable field rather than as a length problem — so check it here, where the number can
# be named.
_MAX_MANAGED_SYSTEM_ADDRESS = 255


def _check_address_length(dns_name: str, method: str) -> None:
    if len(dns_name) > _MAX_MANAGED_SYSTEM_ADDRESS:
        raise PSResourceError(
            f"{method} managed-system address is {len(dns_name)} characters, over Password "
            f"Safe's {_MAX_MANAGED_SYSTEM_ADDRESS}-character limit. Shorten the longest "
            f"field — usually the Resource Broker cert path or the resource group — since a "
            f"truncated address fails later inside the plugin as an unparseable field.")

# The public REST create/update-managed-account path the Terraform provider uses caps
# ``Password`` at 128 characters (400 "Password cannot exceed 128 characters."). This is a
# limit of THAT path only — a plugin's rotation write-back
# (``ManagedAccount_CredentialsNew_Password``) carries multi-KB values, which is how the
# SSH-key plugins store 3.2 KB PEMs. So a credential too long to SEED here is still
# perfectly storable once the plugin rotates it; a k8s ServiceAccount bearer token
# (800–1,200 characters) is exactly that case. Seeding one anyway fails the apply outright,
# so ``register_managed_system`` drops an over-long seed for a placeholder and reports it.
_MAX_SEED_PASSWORD_LEN = 128

# ── Kubernetes Service Account Token address grammar ──────────────────────────
#
# Transcribed from the plugin's Factories/ParameterFactory.cs so a bad address is
# rejected here, at registration, instead of at the first scheduled rotation. The
# plugin rejects an unrecognised option rather than ignoring it (a silently dropped
# option inside a checksum-sealed package is neither diagnosable nor fixable), so
# this validator has to be exact rather than permissive.
#
# Password Safe truncates the address field at 255 characters; the plugin refuses at
# 249 so a truncated address never reaches the cluster lookup.
_K8SSA_MAX_ADDRESS = 249
# Semicolon-separated fields that precede the options, per prefix.
_K8SSA_POSITIONAL = {"eks": 3, "aks": 4, "gke": 4, "k8s": 2}
# Option keys, lowercased exactly as the plugin's ApplyOption switch compares them.
_K8SSA_OPTION_KEYS = frozenset({
    "mode", "ttl", "ns", "dnsendpoint", "allowhostnamemismatch", "servername",
    "rolearn", "aadappid", "ca",
})
# Options the plugin accepts only on one prefix; anywhere else it raises.
_K8SSA_OPTION_PROVIDER = {"rolearn": "eks", "aadappid": "aks", "ca": "k8s"}
_K8SSA_MODES = frozenset({"bound", "longlived"})
_RFC1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class PSResourceError(Exception):
    """Raised when a Password Safe registration Terraform operation fails."""


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


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", (name or "").lower()) or "system"


def _line(key: str, val) -> str:
    """One aligned HCL attribute line (``  key = val``), padding the key so the
    ``=`` lines up across the block — matches the hand-aligned style the tests assert."""
    return f"  {key:<24} = {val}"


def _validate_k8ssa_dns_name(dns_name: str) -> None:
    """Raise PSResourceError unless ``dns_name`` is an address the plugin will parse.

    Mirrors ParameterFactory.ParseAddress: length cap, a known prefix, at least that
    prefix's positional field count, then every trailing field is either the bare mode
    shorthand or ``key=value`` with a recognised, provider-appropriate key. Blank
    trailing fields are skipped, as the plugin skips them, so a trailing ';' is fine."""
    addr = (dns_name or "").strip()
    if not addr:
        raise PSResourceError(
            "Kubernetes ServiceAccount Token onboarding requires a dns_name of the form "
            "'eks;<region>;<cluster>', 'aks;<subscriptionId>;<resourceGroup>;<cluster>', "
            "'gke;<projectId>;<location>;<cluster>' or 'k8s;<apiServerUrl>'")
    if len(addr) > _K8SSA_MAX_ADDRESS:
        raise PSResourceError(
            f"managed system address is {len(addr)} characters, "
            f"{len(addr) - _K8SSA_MAX_ADDRESS} over the {_K8SSA_MAX_ADDRESS} character "
            f"limit the plugin enforces (Password Safe truncates the field at 255)")

    fields = [f.strip() for f in addr.split(";")]
    prefix = fields[0].lower()
    positional = _K8SSA_POSITIONAL.get(prefix)
    if positional is None:
        raise PSResourceError(
            f"managed system address prefix {fields[0]!r} is not recognised — use one of "
            f"eks; aks; gke; k8s;")
    if len(fields) < positional:
        raise PSResourceError(
            f"managed system address {addr!r} has {len(fields)} field(s), expected at "
            f"least {positional} for a {prefix!r} address")
    if any(not f for f in fields[1:positional]):
        raise PSResourceError(
            f"managed system address {addr!r} has an empty positional field — every one of "
            f"the first {positional} fields must be set for a {prefix!r} address")

    for field in fields[positional:]:
        if not field:
            continue
        if field.lower() in _K8SSA_MODES:
            continue
        key, sep, value = field.partition("=")
        if not sep or not key:
            raise PSResourceError(
                f"{field!r} in managed system address {addr!r} is not a recognised option — "
                f"options are 'bound', 'longlived', or key=value with one of: "
                f"{', '.join(sorted(_K8SSA_OPTION_KEYS))}")
        key = key.strip().lower()
        if key not in _K8SSA_OPTION_KEYS:
            raise PSResourceError(
                f"{key!r} in managed system address {addr!r} is not a recognised option key — "
                f"valid keys: {', '.join(sorted(_K8SSA_OPTION_KEYS))}")
        required = _K8SSA_OPTION_PROVIDER.get(key)
        if required and required != prefix:
            raise PSResourceError(
                f"the {key!r} option applies only to {required!r} addresses, but {addr!r} is "
                f"a {prefix!r} address")
        if key == "mode" and value.strip().lower() not in _K8SSA_MODES:
            raise PSResourceError(
                f"token mode {value!r} is not valid — use 'mode=longlived' or 'mode=bound' "
                f"(or the bare shorthand ';bound')")
        # The plugin only requires ttl > 0 here; the API server's own 600s floor is
        # applied by whoever builds the address, not by the parser.
        if key == "ttl" and (not value.strip().isdigit() or int(value.strip()) <= 0):
            raise PSResourceError(
                f"bound token TTL {value!r} is not a positive whole number of seconds "
                f"(example: ttl=43200)")
        if key == "ns" and not _RFC1123_LABEL.match(value.strip()):
            raise PSResourceError(
                f"default namespace {value!r} is not a valid Kubernetes name — lowercase "
                f"letters, digits and hyphens, starting and ending alphanumeric")


# ── GCP Cloud SQL address grammar ─────────────────────────────────────────────
#
# Transcribed from the plugin's Factories/AddressFormat.cs + ParameterFactory.cs, for
# the same reason the k8ssa grammar above is: the plugin treats an unrecognised option
# as an ERROR rather than ignoring it, and an option silently dropped inside a sealed
# package is undiagnosable from the operator's side. Five positional fields, then
# optional key=value options:
#
#   channel;project:region:instance;dbName|-;audience|-;sslTRUE|sslFALSE|-[;option=value]
#
# Field 2 is the Cloud SQL *instance connection name*, not a hostname — project, region
# and instance are derived from it rather than entered separately.
#
# Password Safe truncates the address field at 255; the plugin refuses at 249, and a
# truncated address does not error — it silently becomes a DIFFERENT, WRONG address. So
# this is the tighter of the two limits on purpose (_MAX_MANAGED_SYSTEM_ADDRESS is 255).
_DBGCP_MAX_ADDRESS = 249
_DBGCP_POSITIONAL = 5
_DBGCP_FORMAT = ("channel;project:region:instance;dbName|-;audience|-;"
                 "sslTRUE|sslFALSE|-[;option=value]")
_DBGCP_CHANNELS = frozenset({"admin-api", "data-api", "cloud-run"})
# Both control-plane channels talk to a Google API over TLS and never open a database
# connection, so a database name, an audience or an SSL flag on one is a configuration
# error rather than a no-op — the plugin rejects rather than letting anyone believe they
# turned TLS off.
_DBGCP_CONTROL_PLANE = frozenset({"admin-api", "data-api"})
_DBGCP_OPTION_KEYS = frozenset({"host", "fasecret", "iam", "ver", "verifier"})
# Options the plugin accepts only on one channel; anywhere else it raises.
_DBGCP_OPTION_CHANNEL = {"fasecret": "data-api", "ver": "cloud-run"}
_DBGCP_SSL_FLAGS = frozenset({"ssltrue", "sslfalse"})
# project:region:instance — three non-empty segments, and no ';' smuggled through.
_DBGCP_CONNECTION_NAME = re.compile(r"^[^:;]+:[^:;]+:[^:;]+$")


def _validate_dbgcp_dns_name(dns_name: str) -> None:
    """Raise PSResourceError unless ``dns_name`` is an address the plugin will parse.

    Mirrors the plugin's own pre-flight: length cap, known channel, a well-formed
    instance connection name, then the per-channel rule for each remaining positional
    field, then every trailing field as a recognised ``key=value`` option. Blank
    trailing fields are skipped, as the plugin skips them, so a trailing ';' is fine."""
    addr = (dns_name or "").strip()
    if not addr:
        raise PSResourceError(
            f"GCP Cloud SQL onboarding requires a dns_name of the form '{_DBGCP_FORMAT}'")
    if len(addr) > _DBGCP_MAX_ADDRESS:
        raise PSResourceError(
            f"managed system address is {len(addr)} characters, "
            f"{len(addr) - _DBGCP_MAX_ADDRESS} over the {_DBGCP_MAX_ADDRESS} character "
            f"limit the plugin enforces (Password Safe truncates the field at 255, and a "
            f"truncated address silently becomes a different, wrong address). The instance "
            f"connection name and the audience are the two long fields.")

    fields = [f.strip() for f in addr.split(";")]
    channel = fields[0].lower()
    if channel not in _DBGCP_CHANNELS:
        raise PSResourceError(
            f"managed system address channel {fields[0]!r} is not recognised — use one of "
            f"{', '.join(sorted(_DBGCP_CHANNELS))}")
    if len(fields) < _DBGCP_POSITIONAL:
        raise PSResourceError(
            f"managed system address {addr!r} has {len(fields)} field(s), expected at least "
            f"{_DBGCP_POSITIONAL} — '{_DBGCP_FORMAT}'")

    if not _DBGCP_CONNECTION_NAME.match(fields[1]):
        raise PSResourceError(
            f"instance connection name {fields[1]!r} is not of the form "
            f"'project:region:instance' — get it with "
            f"\"gcloud sql instances describe <instance> --format='value(connectionName)'\"")

    database, audience, ssl_flag = fields[2], fields[3], fields[4]
    if channel == "admin-api":
        if database != "-":
            raise PSResourceError(
                f"the 'admin-api' channel opens no database connection, so field 3 must be "
                f"'-', not {database!r}")
    elif not database or database == "-":
        raise PSResourceError(
            f"the {channel!r} channel requires a database name in field 3")

    if channel == "cloud-run":
        if not audience or audience == "-":
            raise PSResourceError(
                "the 'cloud-run' channel requires the Cloud Run custom audience in field 4")
        low = audience.lower()
        if low.startswith("http://") or low.startswith("https://"):
            rest = audience.split("://", 1)[1]
            if any(c in rest for c in "/?#"):
                raise PSResourceError(
                    f"Cloud Run audience {audience!r} must be a bare origin — a path, query "
                    f"or fragment is rejected, because the audience doubles as the request "
                    f"target and is used verbatim as the token audience")
        if ssl_flag.lower() not in _DBGCP_SSL_FLAGS:
            raise PSResourceError(
                f"field 5 must be 'sslTRUE' or 'sslFALSE' on the 'cloud-run' channel, "
                f"not {ssl_flag!r}")
    else:
        if audience != "-":
            raise PSResourceError(
                f"field 4 (audience) applies only to the 'cloud-run' channel, so it must be "
                f"'-' on {channel!r}, not {audience!r}")
        if ssl_flag != "-":
            raise PSResourceError(
                f"the Cloud SQL Admin and Data APIs are always TLS, so field 5 must be '-' "
                f"on {channel!r}, not {ssl_flag!r} — the plugin rejects a value here rather "
                f"than letting anyone believe they disabled it")

    for field in fields[_DBGCP_POSITIONAL:]:
        if not field:
            continue
        key, sep, value = field.partition("=")
        key = key.strip().lower()
        if not sep or not key or key not in _DBGCP_OPTION_KEYS:
            raise PSResourceError(
                f"{field!r} in managed system address {addr!r} is not a recognised option — "
                f"options are key=value with one of: "
                f"{', '.join(sorted(_DBGCP_OPTION_KEYS))}. The plugin treats an unknown "
                f"option as an error, not as something to ignore.")
        required = _DBGCP_OPTION_CHANNEL.get(key)
        if required and required != channel:
            raise PSResourceError(
                f"the {key!r} option applies only to the {required!r} channel, but {addr!r} "
                f"is a {channel!r} address")
        if not value.strip():
            raise PSResourceError(
                f"the {key!r} option in managed system address {addr!r} has no value")


def _ssm_account_name(name: str, suffix: str) -> str:
    """SSM custom-plugin managed-account name, ``{name};{suffix}``. The suffix is
    ``local`` for IAM-user mode, or the cross-account AssumeRole ARN for EC2 mode."""
    return f"{name or 'adminuser'};{suffix or 'local'}"


def _tf_env(extra_vars: Optional[dict] = None) -> dict:
    """Environment for Terraform calls. The provider OAuth credentials + the run-as
    user ride TF_VAR_* (the destroy path needs them too), as do per-apply secrets."""
    env = dict(os.environ)
    env["TF_PLUGIN_CACHE_DIR"] = _PLUGIN_CACHE_DIR
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["TF_CLI_ARGS"] = "-no-color"
    for cfg_key, tf_var in (
        ("pscli_api_url",          "TF_VAR_ps_url"),
        ("pscli_client_id",        "TF_VAR_ps_client_id"),
        ("pscli_client_secret",    "TF_VAR_ps_client_secret"),
        ("pscli_api_account_name", "TF_VAR_ps_api_account_name"),
    ):
        val = _cfg(cfg_key)
        if val:
            env[tf_var] = val
    for var, val in (extra_vars or {}).items():
        if val is not None:
            env[f"TF_VAR_{var}"] = str(val)
    return env


def _provider_header(extra_vars: str = "") -> str:
    api_version = _cfg("passwordsafe_api_version") or "3.1"
    return f"""\
terraform {{
  required_providers {{
    passwordsafe = {{
      source  = "BeyondTrust/passwordsafe"
      version = "~> 1.0"
    }}
  }}
}}

variable "ps_url"              {{ sensitive = false }}
variable "ps_client_id"        {{ sensitive = true }}
variable "ps_client_secret"    {{ sensitive = true }}
variable "ps_api_account_name" {{ sensitive = false }}
{extra_vars}
provider "passwordsafe" {{
  url              = var.ps_url
  client_id        = var.ps_client_id
  client_secret    = var.ps_client_secret
  api_account_name = var.ps_api_account_name
  api_version      = {json.dumps(api_version)}
}}
"""


def _generate_managed_system_hcl(*, name: str, host_name: str, ip_address: str, port: int,
                                 functional_account_id: int, platform_id: int,
                                 entity_type_id: int, workgroup_id: str,
                                 managed_account_name: str, ssh_key_enforcement_mode: int,
                                 application_host_id: int = 0, method: str = "ssh",
                                 dns_name: str = "", emit_private_key: bool = True,
                                 dss_auto_management: bool = True,
                                 use_own_credentials: bool = False) -> str:
    """HCL onboarding a VM as a managed system + its account. Two shapes via ``method``:

    * ``ssh`` (default) — traditional managed system keyed by host_name/ip on an SSH
      platform; the account's SSH private key + placeholder password ride sensitive
      TF_VARs and ``ssh_key_enforcement_mode`` enforces key-only auth.
    * ``ssm`` / ``azurevm`` / ``gcpvm`` — the cloud-native custom plugins ("AWS Systems
      Manager" / "Azure VM SSH Rotation" / "GCP VM SSH Rotation"): the managed system
      carries the plugin's address in ``dns_name`` (``{instance-id}:{region}`` for ssm,
      ``tenantId/subscriptionId/resourceGroup/vmName`` for azurevm, ``projectId/zone/
      instanceName`` for gcpvm — the field the plugin parses), a placeholder ip, and the
      custom-plugin platform (inherited from the functional account). No private key is
      pushed — Password Safe mints the SSH key via Change Password (over SSM SendCommand /
      Azure Run Command / GCE metadata) — so ``emit_private_key`` is False and the
      private-key TF_VAR is omitted entirely (a declared-but-unset required var fails
      apply under TF_INPUT=0).

    ``application_host_id`` (>0) routes management through a specific application host
    (the traditional Resource Broker path); 0 leaves it to the functional account's platform."""
    label = _safe_name(name)
    extra_vars = 'variable "ps_account_password"    { sensitive = true }\n'
    if emit_private_key:
        extra_vars += 'variable "ps_account_private_key" { sensitive = true }\n'
    header = _provider_header(extra_vars)

    sys_lines = [
        _line("workgroup_id", json.dumps(str(workgroup_id))),
        _line("entity_type_id", int(entity_type_id)),
        _line("host_name", json.dumps(host_name)),
    ]
    if method in _PLUGIN_METHODS and dns_name:
        sys_lines.append(_line("dns_name", json.dumps(dns_name)))
    if ip_address:
        sys_lines.append(_line("ip_address", json.dumps(ip_address)))
    sys_lines += [
        _line("platform_id", int(platform_id)),
        _line("port", int(port)),
        _line("functional_account_id", int(functional_account_id)),
        _line("auto_management_flag", "true"),
    ]
    if method not in _PLUGIN_METHODS:
        sys_lines.append(_line("remote_client_type", '"ssh"'))
        sys_lines.append(_line("ssh_key_enforcement_mode", int(ssh_key_enforcement_mode)))
    if application_host_id and int(application_host_id) > 0:
        sys_lines.append(_line("application_host_id", int(application_host_id)))
        sys_lines.append(_line("is_application_host", "false"))
    sys_lines.append(_line("description", '"Auto-onboarded by Infrastructure Management Dashboard"'))

    acct_lines = [
        _line("system_name", f"passwordsafe_managed_system_by_workgroup.{label}.managed_system_name"),
        _line("account_name", json.dumps(managed_account_name)),
        _line("password", "var.ps_account_password"),
    ]
    if emit_private_key:
        acct_lines.append(_line("private_key", "var.ps_account_private_key"))
    acct_lines += [
        _line("dss_auto_management_flag", "true" if dss_auto_management else "false"),
        _line("auto_management_flag", "true"),
        _line("api_enabled", "true"),
    ]
    if use_own_credentials:
        # "Change Password Using Own Credentials". The DB custom plugins expose two change
        # actions and Password Safe picks between them from THIS flag: with it the account
        # rotates itself (Postgres ALTER USER on self / MySQL ALTER USER CURRENT_USER() /
        # SQL Server ALTER LOGIN … OLD_PASSWORD), which needs no privilege on the target;
        # without it Password Safe calls the via-functional-account action, which needs a
        # privileged DB login (CREATEROLE / CREATE USER / ALTER ANY LOGIN) that a
        # dashboard-provisioned server does not have. Omitted rather than emitted false so
        # existing managed accounts keep whatever they were onboarded with.
        acct_lines.append(_line("use_own_credentials", "true"))

    sys_block = "\n".join(sys_lines)
    acct_block = "\n".join(acct_lines)
    return header + f"""
resource "passwordsafe_managed_system_by_workgroup" {json.dumps(label)} {{
{sys_block}
}}

resource "passwordsafe_managed_account" {json.dumps(label)} {{
{acct_block}
}}

output "managed_system_id" {{
  value = passwordsafe_managed_system_by_workgroup.{label}.managed_system_id
}}

output "managed_account_id" {{
  value = passwordsafe_managed_account.{label}.id
}}
"""


# ── Terraform plumbing ────────────────────────────────────────────────────────

def _run_tf(args: list, work_dir: str, env: dict, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run one terraform subcommand in ``work_dir``.

    ``init`` is serialized on the shared plugin cache via ``terraform.plugin_cache_lock``:
    the tempdir is per-call but TF_PLUGIN_CACHE_DIR is the single cache baked into the
    image, and parallel inits race to place the same provider binary (ETXTBSY). Same
    reasoning as terraform_pra_service._run_tf — see the longer note there."""
    def _go() -> subprocess.CompletedProcess:
        return subprocess.run(
            [_TERRAFORM] + args, cwd=work_dir, capture_output=True, text=True,
            timeout=timeout, env=env)

    if args and args[0] == "init":
        from .terraform import plugin_cache_lock
        with plugin_cache_lock():
            return _go()
    return _go()


def _scrub_state(tf_state_json: Optional[str]) -> Optional[str]:
    """Redact secret attribute values (password / private_key / passphrase / token)
    from state before it is stashed in the job. Destroy is by id, so values aren't
    needed. Fails CLOSED — drop the state rather than stash a plaintext secret."""
    if not tf_state_json:
        return None
    try:
        state = json.loads(tf_state_json)
        for res in state.get("resources", []):
            for inst in res.get("instances", []):
                attrs = inst.get("attributes") or {}
                for k in ("password", "private_key", "passphrase", "token"):
                    if attrs.get(k):
                        attrs[k] = _REDACTED
        return json.dumps(state)
    except Exception as exc:  # noqa: BLE001
        logger.error("PS: failed to scrub Terraform state — dropping it: %s", exc)
        return None


def _apply_hcl_sync(hcl: str, tf_vars: dict) -> dict:
    env = _tf_env(tf_vars)
    with tempfile.TemporaryDirectory(prefix="ps_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(hcl)
        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise PSResourceError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")
        apply = _run_tf(["apply", "-auto-approve"], work_dir, env, timeout=180)
        if apply.returncode != 0:
            raise PSResourceError(
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
        return {
            "managed_system_id": str(outputs.get("managed_system_id") or "") or None,
            "managed_account_id": str(outputs.get("managed_account_id") or "") or None,
            "tf_state_json": _scrub_state(tf_state_json),
        }


def _destroy_sync(tf_state_json: str) -> None:
    """Off-board: restore stored state + provider-only config and destroy (the
    managed account, then the managed system)."""
    try:
        json.loads(tf_state_json)
    except json.JSONDecodeError as e:
        raise PSResourceError(f"tf_state_json is not valid JSON: {e}") from e
    env = _tf_env()
    with tempfile.TemporaryDirectory(prefix="ps_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(_provider_header())
        Path(work_dir, "terraform.tfstate").write_text(tf_state_json)
        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise PSResourceError(
                f"terraform init (destroy) failed: {init.stderr.strip() or init.stdout.strip()}")
        destroy = _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, env, timeout=180)
        if destroy.returncode != 0:
            raise PSResourceError(
                f"terraform destroy failed: {destroy.stderr.strip() or destroy.stdout.strip()}")


# ── Public async API ──────────────────────────────────────────────────────────

async def register_managed_system(*, name: str, host_name: str, private_key: str = "",
                                   functional_account_id: int, platform_id: int,
                                   workgroup_id: str, ip_address: str = "", port: int = 22,
                                   entity_type_id: int = 1, managed_account_name: str = "adminuser",
                                   ssh_key_enforcement_mode: int = 2,
                                   application_host_id: int = 0, method: str = "ssh",
                                   dns_name: str = "", account_suffix: str = "",
                                   initial_password: str = "",
                                   use_own_credentials: bool = False) -> dict:
    """Onboard a VM as a Password Safe managed system + managed account.
    Returns ``{managed_system_id, managed_account_id, tf_state_json,
    initial_password_seeded}``.

    ``method="ssm"`` uses the AWS Systems Manager custom plugin: ``dns_name`` must be
    ``{instance-id}:{region}``, the account name becomes ``{managed_account_name};{suffix}``
    (suffix ``local`` for IAM-user mode or an AssumeRole ARN for EC2 mode), no private key
    is pushed, and ip defaults to a ``127.0.0.1`` placeholder.

    ``method="azurevm"`` uses the Azure VM SSH Rotation custom plugin: ``dns_name`` must be
    ``tenantId/subscriptionId/resourceGroup/vmName`` (four slash-separated parts, the field
    the plugin parses), the account name is the plain Linux user (no suffix), no private key
    is pushed, and ip defaults to a ``127.0.0.1`` placeholder.

    ``method="gcpvm"`` uses the GCP VM SSH Rotation custom plugin: ``dns_name`` must be
    ``projectId/zone/instanceName`` (three slash-separated parts, the field the plugin parses),
    the account name is the plain Linux user (no suffix), no private key is pushed, and ip
    defaults to a ``127.0.0.1`` placeholder.

    ``method="dbssm"`` uses the cloud-DB "{engine} SSM Custom Plugin": ``dns_name`` must be
    ``{instanceArn};{region};{dbEndpoint};{dbName};{publicKeyPath};local`` (six ``;``-separated
    parts), ``port`` is the real DB port, ``managed_account_name`` is the dedicated DB user,
    and the account is password-managed (no SSH DSS key).

    ``method="dbazure"`` uses the cloud-DB "{engine} Azure Run Command Plugin": ``dns_name``
    must be ``vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;sslTRUE|sslFALSE``
    (eight ``;``-separated parts — the jump VM identity plus the DB host/name and the broker
    cert path/SSL flag), ``port`` is the real DB port, ``managed_account_name`` is the dedicated
    DB user the functional-account DB login rotates, and the account is password-managed.

    ``method="pravault"`` uses the "PRA Vault Username Password" plugin: ``host_name`` must be
    the PRA appliance URL and ``managed_account_name`` the exact PRA Vault account name; the
    account is password-managed.

    ``method="k8ssa"`` uses the "Kubernetes Service Account Token" plugin: ``dns_name`` must
    be a cluster address (``eks;<region>;<cluster>``, ``aks;<subscriptionId>;<resourceGroup>;
    <cluster>``, ``gke;<projectId>;<location>;<cluster>`` or ``k8s;<apiServerUrl>``) plus
    optional trailing ``;key=value`` options, at most 249 characters;
    ``managed_account_name`` is ``<namespace>/<serviceaccount>``. The account is
    password-managed — the credential IS the bearer token — but a bearer token cannot be
    seeded (see ``initial_password``), so the first rotation is what populates it.

    ``method="ssh"`` (default) keeps the traditional key-managed flow and requires
    ``private_key``.

    ``initial_password`` seeds the managed account with a credential the caller already
    holds, instead of the throwaway placeholder. Only meaningful for a password-managed
    method, and only up to ``_MAX_SEED_PASSWORD_LEN`` — the create API rejects anything
    longer with a 400 that fails the whole apply, so an over-long value is DROPPED for a
    placeholder rather than passed through. The returned ``initial_password_seeded`` says
    which happened: on False, Password Safe holds a placeholder that authenticates to
    nothing until the account is rotated, and it is the caller's job to make that rotation
    happen. A k8s ServiceAccount token is always over the cap."""
    method = (method or "ssh").lower()
    # The provider requires a password even for a key-managed account; supply a strong
    # placeholder it never uses (the real credential is the SSH key, managed by Password Safe).
    # An over-long seed is dropped rather than passed through: the create API rejects it with
    # a 400 that fails the whole apply, so sending it would cost the managed system too.
    seeded = bool(initial_password) and len(initial_password) <= _MAX_SEED_PASSWORD_LEN
    if initial_password and not seeded:
        # Logs the overage, never the length: a length is derived from the credential and
        # narrows a guess at it, and "over the cap" is the whole actionable content. The
        # cap itself is not interpolated either — passing a value whose IDENTIFIER matches
        # /password/ into a log sink trips CodeQL's name-based sensitive-data heuristic,
        # and the number is already in the constant, the docstring and the design note.
        logger.info(
            "PS: not seeding the %s managed account for %r — the credential is longer than "
            "the create API's maximum; the first rotation will populate it", method, name)
    tf_vars = {"ps_account_password":
               initial_password if seeded else secrets.token_urlsafe(24)}
    if method == "ssm":
        if not dns_name or ":" not in dns_name:
            raise PSResourceError(
                "SSM onboarding requires a dns_name of the form '{instance-id}:{region}'")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=_ssm_account_name(managed_account_name, account_suffix),
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="ssm", dns_name=dns_name, emit_private_key=False)
    elif method == "azurevm":
        if not dns_name or dns_name.count("/") != 3:
            raise PSResourceError(
                "Azure VM SSH Rotation onboarding requires a dns_name of the form "
                "'tenantId/subscriptionId/resourceGroup/vmName'")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="azurevm", dns_name=dns_name, emit_private_key=False)
    elif method == "gcpvm":
        if not dns_name or dns_name.count("/") != 2:
            raise PSResourceError(
                "GCP VM SSH Rotation onboarding requires a dns_name of the form "
                "'projectId/zone/instanceName'")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="gcpvm", dns_name=dns_name, emit_private_key=False)
    elif method == "dbssm":
        # Cloud-DB via the "{engine} SSM Custom Plugin": Password Safe reaches the
        # private RDS instance by running the DB client on a jump host over SSM.
        # dns_name encodes everything the plugin parses, ip is a placeholder, the
        # real DB port applies, and the account is PASSWORD-managed (no SSH key).
        if not dns_name or dns_name.count(";") != 5:
            raise PSResourceError(
                "DB SSM onboarding requires a dns_name of the form "
                "'{instanceArn};{region};{dbEndpoint};{dbName};{publicKeyPath};local'")
        _check_address_length(dns_name, "dbssm")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="dbssm", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False, use_own_credentials=use_own_credentials)
    elif method == "dbazure":
        # Cloud-DB via the "{engine} Azure Run Command Plugin": Password Safe reaches
        # the private Azure DB by running the DB client on a jump VM over Azure VM Run
        # Command. dns_name is eight ``;``-separated fields the plugin parses, ip is a
        # placeholder, the real DB port applies, and the account is PASSWORD-managed
        # (a dedicated managed user the functional-account DB login rotates).
        if not dns_name or dns_name.count(";") != 7:
            raise PSResourceError(
                "DB Azure Run Command onboarding requires a dns_name of the form "
                "'vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;"
                "sslTRUE|sslFALSE'")
        _check_address_length(dns_name, "dbazure")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="dbazure", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False, use_own_credentials=use_own_credentials)
    elif method == "dbgcp":
        # Cloud-DB via the "GCP Cloud SQL {engine}" plugins. Unlike its two DB siblings
        # there is no jump host: the plugin reaches a private-IP instance through the
        # Cloud SQL Data API, so the address carries no host, no cert path and no key
        # material — five positional fields plus optional key=value options. ip is a
        # placeholder, the real DB port applies, and the account is PASSWORD-managed
        # (a dedicated managed user the functional account's IAM identity rotates).
        _validate_dbgcp_dns_name(dns_name)
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="dbgcp", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False, use_own_credentials=use_own_credentials)
    elif method == "pravault":
        # "PRA Vault Username Password" plugin: Password Safe PATCHes the rotated
        # password into a PRA Vault username_password account via the PRA Config API.
        # The managed system's network address (host_name) is the PRA appliance URL;
        # the managed account name is the exact PRA Vault account name. Password-managed.
        if not host_name:
            raise PSResourceError(
                "PRA Vault onboarding requires host_name set to the PRA appliance URL")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1",
            port=port or 443,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="pravault", dns_name="", emit_private_key=False,
            dss_auto_management=False)
    elif method == "k8ssa":
        # "Kubernetes Service Account Token" plugin: dns_name carries the cluster
        # address plus trailing ;key=value options, and the managed account name is
        # "<namespace>/<serviceaccount>". host_name stays a human label and ip_address
        # the 127.0.0.1 placeholder — the plugin iterates every host Password Safe
        # supplies and skips the ones that do not parse as a cluster address, so the
        # two non-addresses cost nothing. Port is irrelevant (the API server port is
        # part of the endpoint URL). Password-managed: the credential is the bearer
        # token itself, so no private key and no DSS auto-management.
        _validate_k8ssa_dns_name(dns_name)
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1",
            port=port or 443,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="k8ssa", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False)
    else:
        if not private_key:
            raise PSResourceError(
                "no SSH private key available for the managed account — Password Safe "
                "manages the account by key; check the VM keypair secret")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address, port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id, method="ssh", emit_private_key=True)
        tf_vars["ps_account_private_key"] = private_key
    out = await asyncio.to_thread(_apply_hcl_sync, hcl, tf_vars)
    out["initial_password_seeded"] = seeded
    return out


async def deregister(tf_state_json: str) -> None:
    """Off-board a managed system + account previously registered (best-effort)."""
    await asyncio.to_thread(_destroy_sync, tf_state_json)
