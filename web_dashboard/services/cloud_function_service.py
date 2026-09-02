"""
Cloud Functions — the cloud-agnostic service seam (preview).

Deploys a dashboard-authored handler (``web_dashboard/functions/``) as an AWS
Lambda / Azure Linux Function App / GCP Cloud Run function, and records each in the
``cloud_functions`` inventory table. Shaped like ``cloud_database_service``: module
-level dispatch dicts rather than classes, a per-job Terraform deploy dir, secrets
stripped from job metadata and re-injected at apply time.

What a function gives that nothing else in the dashboard does is a **stable inbound
HTTPS endpoint** with a millisecond response, optionally sitting inside the VPC/VNet
next to a private resource. Existing execution paths (one-shot ECS/ACI/Cloud Run
containers) reach private resources fine but are dashboard-initiated and slow to
start, so they cannot serve Entitle's outbound Give/Revoke Access POST — which is
what Phase 2 needs.

``deploy`` does the synchronous record-keeping (mint the secret, build + upload the
package, write the row and the Job) and returns; the actual ``terraform apply`` runs
in :func:`run_deploy_apply`, scheduled as a background task by the API.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..database import CloudFunction, Job
from . import (cloud_function_package, config_service, job_service, region_catalog,
               terraform, terraform_provider_env)
from .region_config import resolve_region

logger = logging.getLogger(__name__)

VALID_CLOUDS = ("aws", "azure", "gcp")
VALID_NETWORK_MODES = ("public", "vpc")

# The workload catalog is the FILESYSTEM (web_dashboard/functions/fnworkloads/) —
# dropping in a module makes it deployable, which is the point of the feature being
# modular. This table therefore records only the EXCEPTIONS: workloads that need a
# cloud SDK which only one runtime ships. Vendoring the others would break the
# stdlib-only rule, and anything with a compiled extension is unsafe to vendor on
# this multi-arch image (see cloud_function_package._BINARY_SUFFIXES).
_CLOUD_RESTRICTED = {
    # Uses boto3 for ssm:SendCommand; only the Lambda runtime ships an AWS SDK.
    "local_account_broker": ("aws",),
    # Serves the Password Safe GCP Cloud SQL plugin's "cloud-run" channel. Not a
    # missing SDK — a missing CALLER: the contract, the OIDC front door and the
    # instance connection name are all GCP concepts, and the same code on Lambda
    # would be a credential-changing endpoint nothing ever calls.
    "ps_dbops": ("gcp",),
}

# Workloads that may not be deployed with an open front door or without a VPC.
# ``ps_dbops`` receives a live database credential in the request body and opens a
# connection to a private-IP instance, so "public + shared secret" (the GCP default,
# below) and "no VPC attachment" are both deploys that must fail at the click rather
# than produce a working, wrong service.
_REQUIRES_AUTHENTICATED_FRONT_DOOR = ("ps_dbops",)
_REQUIRES_VPC = ("ps_dbops",)

_PROVIDER = {"aws": "lambda", "azure": "function_app", "gcp": "cloudrun_function"}
# One runtime, three spellings. Keeping them in a dict beats three string literals
# scattered through the module.
_RUNTIME = {"aws": "python3.12", "azure": "3.12", "gcp": "python312"}

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATE_DIRS = {
    "aws": os.path.join(_REPO_ROOT, "terraform", "cloud_function", "aws_lambda"),
    "azure": os.path.join(_REPO_ROOT, "terraform", "cloud_function", "azure_function_app"),
    "gcp": os.path.join(_REPO_ROOT, "terraform", "cloud_function", "gcp_cloudrun"),
}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")

# cloud → the module's declared variable names, parsed once. See _module_variables.
_MODULE_VARIABLE_CACHE: dict = {}

# Front-door defaults. Deliberately the OPEN option on AWS/GCP: Entitle authenticates
# with a plain header, so requiring SigV4 or an OIDC token would make the integration
# undeployable without a proxy. The shared bearer secret is the gate in that case,
# which is exactly the layered model — an operator who fronts the function with a
# gateway can tighten this per function.
_DEFAULT_AUTH_MODE = {"aws": "NONE", "azure": "function_key", "gcp": "none"}

# Stripped before tf_variables are persisted to job metadata, and re-injected in
# BOTH run_deploy_apply and run_decommission. package_sas_url is the easy one to
# miss: it is a credential embedded in a URL, so unstripped it lands in
# jobs.extra_data AND streams into the job's Live Output line by line.
_SECRET_TF_KEYS = ("shared_secret", "package_sas_url", "storage_account_access_key")

# Environment keys whose VALUE would be a credential. Every workload that needs one
# takes it BY REFERENCE (``secret_environment`` below), on all three clouds, so a
# plaintext one in `environment` is always an operator mistake — and an expensive
# one, because `environment` is deliberately not in _SECRET_TF_KEYS: it lands in
# jobs.extra_data, streams into the job's Live Output as terraform renders the plan,
# and stays readable on the function's own console page for the life of the deploy.
#
# A superset of fnruntime.logs' redaction substrings (pinned by
# tests/test_cloud_function_service.py), so what the runtime refuses to LOG is at
# least what the deploy path refuses to STORE. It also folds in the credential names
# that module matches EXACTLY rather than by substring — `api_key` and friends —
# because an environment variable is always prefixed (FN_PORTAINER_API_KEY), so an
# exact match would never fire here.
#
# Deliberately NOT included, for the same reason logs.py leaves out "key": "auth",
# which would refuse the legitimate FN_AUTH_HEADER / FN_AUTH_PREFIX settings and
# teach people to work around the guard.
_SECRET_ENV_SUBSTRINGS = (
    "password", "passwd", "pwd", "secret", "token", "credential", "authorization",
    "api_key", "apikey", "private_key", "connection_string", "cookie", "sas_url",
)

# Two things that carry a credential's NAME rather than its value, and are therefore
# fine in cleartext: the id variable fnruntime.secretref reads on AWS, and the app
# setting the Azure platform resolves before the worker starts.
_REFERENCE_SUFFIX = "_SECRET_ID"
_AZURE_KV_PREFIX = "@Microsoft.KeyVault("

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MEMORY_MB = 256
# Matches the gcp_cloudrun module's own default, so passing it explicitly changes
# nothing for an existing function while giving a caller a way to raise it.
_DEFAULT_MAX_INSTANCES = 3

# Function names: cloud-safe intersection (Lambda allows 64 chars incl. '-'/'_';
# an Azure Function App name becomes a DNS label, so lowercase alphanumeric + '-'
# only, <= 60). Normalising to the strictest rule keeps one name valid everywhere.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,59}$")

# Environment variable names, checked before they reach a module: all three clouds
# reject an invalid one, but two of them do it at apply time with a message that
# does not name the offending key.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CloudFunctionError(Exception):
    pass


def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val:
        return val
    return getattr(settings, key, "") or ""


def terraform_available() -> bool:
    return shutil.which(settings.terraform_executable) is not None


def template_dir(cloud: str) -> str:
    return _TEMPLATE_DIRS[cloud]


def _module_variables(cloud: str) -> frozenset:
    """Variable names the cloud's module declares, read from its ``.tf`` files.

    Cached because it is read on every apply and destroy and the files ship in the
    image. An unreadable module returns an empty set, which disables the filtering
    below rather than emptying the var set.
    """
    cached = _MODULE_VARIABLE_CACHE.get(cloud)
    if cached is None:
        names = set()
        try:
            directory = _TEMPLATE_DIRS[cloud]
            for entry in sorted(os.listdir(directory)):
                if entry.endswith(".tf"):
                    with open(os.path.join(directory, entry), encoding="utf-8") as fh:
                        names.update(re.findall(r'^variable\s+"([^"]+)"', fh.read(),
                                                re.M))
        except OSError as exc:
            logger.warning("cloudfn: cannot read the %s module's variables: %s",
                           cloud, exc)
        cached = _MODULE_VARIABLE_CACHE[cloud] = frozenset(names)
    return cached


def _drop_undeclared(cloud: str, variables: dict) -> dict:
    """Remove ``-var``s the module has no ``variable`` block for.

    Terraform treats an undeclared -var as a hard error, not a warning, and it fails
    the run before touching anything — so on DESTROY the effect is a function that
    can never be torn down. The persisted var set is a snapshot of whatever the
    service sent at deploy time, and it outlives any later change to the module, so
    replaying it has to tolerate a variable the module no longer takes.
    """
    declared = _module_variables(cloud)
    if not declared:
        return variables
    stale = sorted(set(variables) - declared)
    if stale:
        logger.info("cloudfn: dropping %s -var(s) the %s module does not declare: %s",
                    len(stale), cloud, ", ".join(stale))
    return {k: v for k, v in variables.items() if k in declared}


def _deploy_dir(job_id: str) -> str:
    return os.path.join(_DEPLOYMENTS_DIR, job_id)


def normalize_name(raw: str) -> str:
    """A function name valid on all three clouds (see _NAME_RE)."""
    slug = re.sub(r"[^a-z0-9-]", "-", (raw or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if slug and not slug[0].isalpha():
        slug = "fn-" + slug
    return slug[:60]


def _check_target(workload: str, cloud: str) -> None:
    if cloud not in VALID_CLOUDS:
        raise CloudFunctionError(
            f"unknown cloud {cloud!r} (expected one of {', '.join(VALID_CLOUDS)})")
    if workload not in available_workloads():
        raise CloudFunctionError(
            f"unknown workload {workload!r} "
            f"(available: {', '.join(available_workloads())})")
    if cloud not in clouds_for(workload):
        raise NotImplementedError(
            f"the {workload!r} workload is not available on {cloud} "
            f"(it needs a cloud SDK that only {'/'.join(clouds_for(workload))} "
            "ships in its runtime)")


def _check_front_door(workload: str, *, auth_mode: str, network_mode: str) -> None:
    """Refuse a deploy whose front door or placement makes the workload unsafe.

    Separate from :func:`_check_target` because these are not facts about the
    workload's availability — they are facts about THIS deploy's options, and the
    message has to name the option to change.
    """
    if workload in _REQUIRES_AUTHENTICATED_FRONT_DOOR and (auth_mode or "").lower() in (
            "none", "no", "public"):
        raise CloudFunctionError(
            f"the {workload!r} workload may not be deployed with auth_mode={auth_mode!r} "
            "— it takes a live database credential in the request body, so the platform "
            "front door (roles/run.invoker) is the authentication, not an extra. Deploy "
            "it with auth_mode='run_invoker'")
    if workload in _REQUIRES_VPC and network_mode != "vpc":
        raise CloudFunctionError(
            f"the {workload!r} workload needs network_mode='vpc' — it connects to a "
            "private-IP database, and a public deploy would succeed and then fail every "
            "request")


def available_workloads() -> tuple:
    """The catalog, from the filesystem — the source of truth is the module tree."""
    return cloud_function_package.available_workloads()


def clouds_for(workload: str) -> tuple:
    """Clouds a workload can run on. Universal unless _CLOUD_RESTRICTED says
    otherwise, so adding a stdlib-only workload needs no table edit."""
    return _CLOUD_RESTRICTED.get(workload, VALID_CLOUDS)


def workload_catalog() -> list:
    """``[{name, description, clouds}]`` for the UI picker.

    Workload modules are stdlib-only and import their cloud SDKs lazily, so the
    dashboard can import them purely to read their docstrings. A workload that
    somehow fails to import still appears, just without its description — the
    catalog must never be the thing that breaks the page.
    """
    catalog = []
    for name in available_workloads():
        description = ""
        try:
            import importlib
            from .. import functions  # noqa: F401  (puts fnworkloads on sys.path)
            module = importlib.import_module(f"fnworkloads.{name}")
            description = str(getattr(module, "DESCRIPTION", "") or "")
        except Exception as exc:
            logger.debug("cloudfn: workload %s did not import for catalog: %s", name, exc)
        catalog.append({
            "name": name,
            "description": description,
            "clouds": list(clouds_for(name)),
            # So the picker can say what a workload needs BEFORE you deploy it,
            # rather than the operator discovering it from a function that 500s.
            "required_env": list(required_env(name)),
            # And so the page offers "Register in Entitle" on exactly the workloads
            # that serve the contract, instead of a hand-maintained list in its JS.
            "entitle_adapter": is_entitle_adapter(name),
        })
    return catalog


def _workload_module(workload: str):
    """The workload module, or ``None`` if it will not import.

    Neither the catalog nor a deploy may fail because of this — a workload we cannot
    read simply declares nothing.
    """
    try:
        import importlib
        from .. import functions  # noqa: F401  (puts fnworkloads on sys.path)
        return importlib.import_module(f"fnworkloads.{workload}")
    except Exception as exc:
        logger.debug("cloudfn: workload %s did not import: %s", workload, exc)
        return None


def is_entitle_adapter(workload: str) -> bool:
    """Whether ``workload`` serves the Entitle Remote Adapter contract.

    From the module's own ``ENTITLE_ADAPTER`` flag. Registering a workload that does
    not serve it produces a live integration in the tenant that can never resolve an
    asset — visible only in Entitle, which is a bad place to discover it.
    """
    return bool(getattr(_workload_module(workload), "ENTITLE_ADAPTER", False))


def required_env(workload: str) -> tuple:
    """Settings ``workload`` cannot run without, from its own ``REQUIRED_ENV``.

    ``"A|B"`` means either satisfies the requirement (``FN_DB_NAME|FN_DB_NAMES``).
    Empty for a workload that needs no configuration, which is most of them.
    """
    module = _workload_module(workload)
    return tuple(str(name) for name in getattr(module, "REQUIRED_ENV", ()) or ())


def _check_required_env(workload: str, environment: Optional[dict]) -> None:
    """Refuse a deploy that could only produce a function which fails on every call.

    The adapter workloads read their target out of the environment, so one deployed
    without it is not degraded — it is inert, and it costs a real build to find out.
    The automated pairing path (cloud_db_adapter_service) always passes these, so
    this only ever fires on a hand-deploy that omitted them.
    """
    present = {str(k) for k, v in (environment or {}).items() if str(v or "").strip()}
    missing = [req for req in required_env(workload)
               if not any(alt in present for alt in req.split("|"))]
    if not missing:
        return
    hint = ("To have this done for you, use the 'Function (DB grant)' action on the "
            "database's row on the Databases page (or provision a MySQL / SQL Server "
            "database with 'Register in Entitle' checked). That pairing deploys and "
            "configures its own adapter, in the database's own region. It cannot finish "
            "one started by hand: it names its function after the database."
            if workload == "db_grant" else
            "Deploy echo_diag instead if you only want to test the endpoint.")
    raise CloudFunctionError(
        f"the {workload!r} workload needs "
        + ", ".join(req.replace("|", " or ") for req in missing)
        + " in `environment` — without them the function deploys and then fails on "
        f"every request. {hint}")


# ── Terraform variables (PURE — the main unit-test target) ───────────────────

def _resolved_network(cloud: str, region: str, *, network_mode: str,
                      subnet_ids: Optional[list], subnet_id: str,
                      vpc_connector: str, security_group_ids: Optional[list],
                      vpc_network: str = "", vpc_subnetwork: str = "") -> dict:
    """Per-cloud network ids for ``network_mode``, defaulting from region config.

    Returns ``{}`` for public mode. Raises when vpc mode is asked for without the
    ids that cloud needs — before the row is written, so a misconfigured request
    never leaves a half-created record behind.
    """
    if network_mode == "public":
        return {}
    if network_mode not in VALID_NETWORK_MODES:
        raise CloudFunctionError(
            f"unknown network_mode {network_mode!r} "
            f"(expected one of {', '.join(VALID_NETWORK_MODES)})")

    # Region-correctness lives entirely in these `functions_*` fields, and the shape of
    # the fix is worth stating because the obvious version is wrong. A flat
    # aws_functions_subnet_ids / gcp_functions_subnetwork answers TWO questions at once:
    # "which subnet do FUNCTIONS use" (purpose) and "in the default region" (place).
    # Beating it with a per-region GENERIC subnet — the first attempt here — fixes place
    # by discarding purpose, and only for non-default regions, since a region entry is
    # empty for the default one. The result was a feature that picked a different-purpose
    # subnet depending on whether the region happened to be the default.
    #
    # So the per-region field is purpose-specific too, and resolve_region falls it back
    # to exactly the flat key it replaces. Every tier below is therefore symmetric: the
    # default region resolves to the flat keys as it always did, and any other region
    # resolves to its own value where one is set and the same flat key where it is not.
    #
    # ValueError only: _spec rejects oci/local, which is expected. Anything else is a bug
    # and should not be swallowed into a wrong-region deploy.
    regional: dict = {}
    try:
        regional = resolve_region(cloud, region) or {}
    except ValueError:
        regional = {}

    if cloud == "aws":
        # NB: the last resort is default_subnet_id, NOT db_subnet_group_name — that is
        # an RDS subnet-GROUP NAME, and handing it to Terraform as a subnet id passes
        # validation here and then dies at apply on InvalidSubnetID.NotFound.
        subnets = ([s for s in (subnet_ids or []) if s]
                   or _csv(regional.get("functions_subnet_ids", ""))
                   or _csv(_cfg("aws_functions_subnet_ids"))
                   or _csv(regional.get("default_subnet_id", "")))
        groups = ([g for g in (security_group_ids or []) if g]
                  or _csv(regional.get("functions_security_group_ids", ""))
                  or _csv(_cfg("aws_functions_security_group_ids"))
                  or _csv(regional.get("db_security_group_id", "")))
        if not subnets:
            raise CloudFunctionError(
                "network_mode=vpc needs subnet ids on AWS — pass subnet_ids or set "
                "aws_functions_subnet_ids in Settings → Cloud Functions")
        if not groups:
            raise CloudFunctionError(
                "network_mode=vpc needs at least one security group on AWS, or the "
                "function's ENIs get the default SG and the DB path silently fails")
        return {"subnet_ids": subnets, "security_group_ids": groups}

    if cloud == "azure":
        # VNet integration requires a SAME-REGION subnet delegated to
        # Microsoft.Web/serverFarms — a different subnet from the database one, which is
        # delegated to the DB service. Hence its own per-region field rather than reusing
        # db_subnet_id.
        chosen = ((subnet_id or "").strip() or regional.get("functions_subnet_id")
                  or _cfg("azure_functions_subnet_id"))
        if not chosen:
            raise CloudFunctionError(
                "network_mode=vpc needs a subnet on Azure — set azure_functions_subnet_id. "
                "It must be DELEGATED TO Microsoft.Web/serverFarms, and is a different "
                "subnet from the database one (delegated to the DB service)")
        return {"subnet_id": chosen}

    # GCP: Direct VPC egress is the default — no connector to pre-provision and
    # nothing billed when no function exists. The module creates and destroys the
    # attachment with the function. Fall back through the keys the sandbox emits,
    # then the ones the Cloud Run runners already use, before the legacy connector.
    #
    # Deliberately a BARE subnet name, not a regional self-link: direct egress is
    # region-locked, and the sandbox gives every region an identically-named subnet,
    # so one bare name resolves correctly wherever the function lands. That convention
    # is a convenience, not a guarantee — a region configured by hand need not follow it
    # — so a region that names its own functions subnet wins, and the tier order below
    # is otherwise exactly what it always was.
    # The bare _cfg tiers are unreachable while resolve_region works — it already falls
    # each functions_* field back to exactly that key — and are kept deliberately, as on
    # AWS and Azure above, so the `except ValueError` branch (a cloud with no region
    # dimension) still honours the purpose-specific setting instead of skipping to the
    # runner's subnet.
    net = ((vpc_network or "").strip() or regional.get("functions_network")
           or _cfg("gcp_functions_network") or _cfg("gcp_run_network")
           or regional.get("network") or _cfg("gcp_network"))
    subnet = ((vpc_subnetwork or "").strip() or regional.get("functions_subnetwork")
              or _cfg("gcp_functions_subnetwork") or _cfg("gcp_run_subnetwork")
              or regional.get("jumpoint_subnetwork")
              or _cfg("gcp_jumpoint_subnetwork"))
    if net or subnet:
        return {"vpc_network": net, "vpc_subnetwork": subnet}

    chosen = (vpc_connector or "").strip() or _cfg("gcp_functions_vpc_connector")
    if not chosen:
        raise CloudFunctionError(
            "network_mode=vpc needs a VPC on GCP — set gcp_functions_network and "
            "gcp_functions_subnetwork for Direct VPC egress (no connector, nothing "
            "billed while idle; the subnet must be in the function's region). A "
            "pre-existing Serverless VPC Access connector still works via "
            "gcp_functions_vpc_connector, but costs ~$26/mo whether invoked or not")
    return {"vpc_connector": chosen}


def _csv(raw) -> list:
    if isinstance(raw, (list, tuple)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _reject_plaintext_secrets(environment: Optional[dict]) -> None:
    """Refuse a credential VALUE in the plaintext ``environment`` map.

    Deliberately a hard error rather than a warning: the damage is done the moment
    the deploy is recorded (see _SECRET_ENV_SUBSTRINGS), so there is no useful state
    between "accepted" and "leaked", and rotating whatever was pasted is a worse
    afternoon than reading this message.
    """
    for key, value in (environment or {}).items():
        name = str(key)
        if not any(s in name.lower() for s in _SECRET_ENV_SUBSTRINGS):
            continue
        if name.upper().endswith(_REFERENCE_SUFFIX):
            continue                                    # an id, not a value
        text = str(value or "")
        if not text or text.startswith(_AZURE_KV_PREFIX):
            continue                                    # unset, or a Key Vault reference
        raise CloudFunctionError(
            f"{name} looks like a credential, and `environment` is stored and logged "
            "in cleartext. Pass it as secret_environment instead — a Secrets Manager "
            "ARN on AWS, a Secret Manager secret id on GCP, a Key Vault secret name "
            "on Azure — so only the reference is ever recorded.")


def _secret_environment(cloud: str, refs: Optional[dict], *, vault: str = "") -> tuple:
    """``(extra_environment, extra_tf_variables)`` for ``{ENV_NAME: <reference>}``.

    One deploy field, three mechanisms, because only two of the clouds resolve a
    secret for you:

      * **GCP** — ``secret_environment_variables``; Terraform passes the secret id
        and the platform injects the value.
      * **Azure** — an ``@Microsoft.KeyVault(...)`` app setting, resolved by the
        platform using the app's system-assigned identity.
      * **AWS** — nothing platform-side, so the id goes in the function's
        environment and the ARN goes on its role; ``fnruntime.secretref`` reads it
        at cold start. ``<NAME>_SECRET_ID`` is the name that module derives, which
        is what lets one entry here work on all three clouds with no per-workload
        mapping table.

    In every case Terraform sees a reference and never a value.
    """
    cleaned = {}
    for key, value in (refs or {}).items():
        name, ref = str(key).strip(), str(value).strip()
        if not name or not ref:
            continue
        if not _ENV_NAME_RE.match(name):
            raise CloudFunctionError(
                f"{name!r} is not a valid environment variable name")
        cleaned[name] = ref
    if not cleaned:
        return {}, {}

    if cloud == "gcp":
        return {}, {"secret_environment": cleaned}

    if cloud == "aws":
        for name, ref in cleaned.items():
            if not ref.startswith("arn:"):
                raise CloudFunctionError(
                    f"secret_environment[{name}] must be a full Secrets Manager ARN "
                    "on AWS — the role's policy names the ARN, and AWS appends a "
                    f"random suffix to every one, so {ref!r} cannot be turned into "
                    "one by concatenation")
        return ({f"{name}{_REFERENCE_SUFFIX}": ref for name, ref in cleaned.items()},
                {"readable_secret_arns": list(cleaned.values())})

    out = {}
    for name, ref in cleaned.items():
        if ref.startswith(_AZURE_KV_PREFIX):
            out[name] = ref                             # already a full reference
            continue
        if not vault:
            raise CloudFunctionError(
                "no Azure Key Vault is configured — an Azure function resolves its "
                "credentials through a Key Vault reference, so there is nowhere for "
                f"{name} to come from. Set the vault URL in Settings → Secrets → "
                "Azure Key Vault (secrets_azure_kv_url)")
        out[name] = (f"@Microsoft.KeyVault(SecretUri="
                     f"https://{vault}.vault.azure.net/secrets/{ref}/)")
    return out, {}


def azure_bearer_key(fn_id: str) -> str:
    """Key Vault secret name for a function's bearer secret. Derived from the id so
    :func:`_reinject_secrets` can rebuild the reference at apply and destroy time
    without writing to the vault again."""
    return f"cloudfn-{fn_id}-bearer"


def _azure_key_vault() -> tuple:
    """``(name, url, resource_group)`` of the vault Azure functions resolve from.

    The name and the URL are each derivable from the other, and operators have
    historically set one or the other, so accept either. The resource group is only
    needed for the module's ``data "azurerm_key_vault"`` lookup, which is what lets
    it detect RBAC-vs-access-policy and grant the right one.
    """
    url = (_cfg("secrets_azure_kv_url") or _cfg("azure_key_vault_url")
           or "").strip().rstrip("/")
    name = (_cfg("azure_key_vault_name") or _cfg("azure_keyvault_name") or "").strip()
    if not name and url:
        name = url.split("://", 1)[-1].split("/", 1)[0].split(".", 1)[0]
    if not url and name:
        url = f"https://{name}.vault.azure.net"
    return name, url, (_cfg("azure_key_vault_resource_group")
                       or _cfg("azure_resource_group") or "").strip()


def azure_bearer_reference(fn_id: str) -> dict:
    """The module variables that point an Azure function at its bearer secret.

    Pure — no vault write. :func:`_stage_azure_bearer` does that once at deploy;
    everything afterwards (apply, destroy) only needs to name the same secret again.
    """
    name, url, resource_group = _azure_key_vault()
    if not name:
        return {}
    return {
        "shared_secret_kv_uri":
            f"@Microsoft.KeyVault(SecretUri={url}/secrets/{azure_bearer_key(fn_id)}/)",
        "key_vault_name": name,
        "key_vault_resource_group": resource_group,
    }


def _stage_azure_bearer(fn_id: str, secret: str) -> dict:
    """Write the bearer secret to Key Vault and return the reference variables.

    Azure ends up the strictest of the three: the value reaches neither an app
    setting nor Terraform state, because the platform resolves the reference itself
    and the module is handed only the URI. AWS and GCP still pass the value to the
    resource that stores it, so it lands in state there.

    Refuses rather than falling back to a literal app setting — a deploy that
    quietly downgrades to a readable credential is the failure this is here to stop.
    """
    refs = azure_bearer_reference(fn_id)
    if not refs:
        raise CloudFunctionError(
            "an Azure function keeps its bearer secret in Key Vault, and no vault is "
            "configured — set the vault URL in Settings → Secrets → Azure Key Vault "
            "(secrets_azure_kv_url)")
    from . import secrets_backend_service
    secrets_backend_service.write_azure_kv(azure_bearer_key(fn_id), secret)
    return refs


def _build_tf_variables(*, cloud: str, region: str, name: str, workload: str,
                        package: dict, network: dict, opts: dict) -> dict:
    """The ``-var`` set for the cloud's module. Pure: no config reads, no I/O —
    everything it needs is already resolved by the caller, which is what makes it
    unit-testable without a database or a cloud."""
    # Only what all three modules declare. Terraform fails an apply outright on a
    # -var the root module has no variable block for, so a key belongs here only
    # once every cloud has somewhere to put it — memory_mb does not, because on
    # Azure memory comes from the App Service plan SKU, not from the app.
    common = {
        "name": name,
        "workload": workload,
        "shared_secret": opts["shared_secret"],
        "timeout_seconds": int(opts.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
        "environment": dict(opts.get("environment") or {}),
    }
    memory_mb = int(opts.get("memory_mb") or _DEFAULT_MEMORY_MB)

    if cloud == "aws":
        return {
            **common,
            "memory_mb": memory_mb,
            "region": region,
            "runtime": _RUNTIME["aws"],
            "auth_mode": opts.get("auth_mode") or _DEFAULT_AUTH_MODE["aws"],
            "package_bucket": package["bucket"],
            "package_key": package["key"],
            "package_sha256_b64": package["sha256_b64"],
            "subnet_ids": network.get("subnet_ids", []),
            "security_group_ids": network.get("security_group_ids", []),
            # AWS is the only cloud with no platform-resolved env-var secret for
            # functions, so a credential-using workload reads it itself (via
            # fnruntime.secretref, from the <NAME>_SECRET_ID env var deploy() sets)
            # and needs the grant. Named ARNs only — never a wildcard.
            "readable_secret_arns": _csv(opts.get("readable_secret_arns")),
        }

    if cloud == "gcp":
        return {
            **common,
            "memory_mb": memory_mb,
            "region": region,
            "project": opts["project"],
            "runtime": _RUNTIME["gcp"],
            "auth_mode": opts.get("auth_mode") or _DEFAULT_AUTH_MODE["gcp"],
            "package_bucket": package["bucket"],
            "package_object": package["key"],
            "service_account_email": opts.get("service_account_email", ""),
            "vpc_connector": network.get("vpc_connector", ""),
            "vpc_network": network.get("vpc_network", ""),
            "vpc_subnetwork": network.get("vpc_subnetwork", ""),
            # Secret Manager ids, injected as env vars by the platform, so no value
            # reaches Terraform state or the function's describe output.
            # db_admin_secret is the original db_grant-specific spelling; the module
            # merges the two.
            "secret_environment": dict(opts.get("secret_environment") or {}),
            "db_admin_secret": opts.get("db_admin_secret", ""),
            # Scaling and the front door. All four were module variables with no way
            # to reach them: ingress_settings was DECLARED but never passed, so every
            # GCP function was ALLOW_ALL whatever the module said, and there was no
            # variable at all for the named-invoker bindings a Password Safe Resource
            # Broker needs. Defaults keep every existing function byte-identical.
            "ingress_settings": opts.get("ingress_settings") or "ALLOW_ALL",
            "min_instances": int(opts.get("min_instances") or 0),
            "concurrency": int(opts.get("concurrency") or 0),
            "max_instances": int(opts.get("max_instances") or _DEFAULT_MAX_INSTANCES),
            "invoker_members": _csv(opts.get("invoker_members")),
        }

    # Azure: no bucket/key vars — run-from-package takes a single SAS URL, and the
    # object key still carries the content hash so terraform sees a real diff. No
    # memory_mb either: sku_name sizes the plan, and the app gets whatever the plan
    # has. timeout_seconds DOES apply — the module turns it into the host.json
    # functionTimeout override.
    bearer = dict(opts.get("azure_bearer") or {})
    return {
        **common,
        "location": region,
        "resource_group_name": opts["resource_group_name"],
        "python_version": _RUNTIME["azure"],
        "sku_name": opts.get("sku_name") or "B1",
        "package_sas_url": package["sas_url"],
        "storage_account_name": package["storage_account"],
        "storage_account_access_key": package["storage_key"],
        "subnet_id": network.get("subnet_id", ""),
        # A reference, not a value — so unlike the other two clouds the bearer
        # secret never reaches Terraform at all. The module's precondition wants
        # exactly one of the two, hence blanking the literal.
        **bearer,
        "shared_secret": "" if bearer else common["shared_secret"],
    }


def strip_secrets(tf_variables: dict) -> dict:
    """A copy safe to persist to ``jobs.extra_data``."""
    return {k: v for k, v in (tf_variables or {}).items() if k not in _SECRET_TF_KEYS}


def _serialize(row: CloudFunction) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "workload": row.workload,
        "cloud": row.cloud,
        "region": row.region,
        "provider": row.provider,
        "runtime": row.runtime,
        "status": row.status,
        "resource_id": row.resource_id,
        "invoke_url": row.invoke_url,
        "package_sha256": row.package_sha256,
        "auth_mode": row.auth_mode,
        "network_mode": row.network_mode,
        "network": json.loads(row.network_ref) if row.network_ref else {},
        "provenance": json.loads(row.provenance) if row.provenance else {},
        "deploy_job_id": row.deploy_job_id,
        "entitle_integration_id": row.entitle_integration_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_functions(db: Session) -> list:
    rows = (db.query(CloudFunction)
            .filter(CloudFunction.status != "deleted")
            .order_by(CloudFunction.created_at.desc()).all())
    return [_serialize(row) for row in rows]


def find_by_names(db: Session, names, *, workload: str = "") -> dict:
    """``{name: CloudFunction}`` for the live functions among ``names``.

    The lookup deploy() does NOT do: it inserts unconditionally, with no existence
    check on the name, and every deploy gets a fresh (empty) Terraform directory. So a
    second deploy of a deterministically-named function leaves a duplicate row wedged in
    ``deploying`` and an "already exists" apply failure — callers that own such a name
    have to ask first. ``name`` is indexed and this is one query for the whole page.

    ``deleted`` rows are excluded: that name is free again.
    """
    wanted = sorted({str(n) for n in (names or []) if n})
    if not wanted:
        return {}
    query = (db.query(CloudFunction)
             .filter(CloudFunction.name.in_(wanted),
                     CloudFunction.status != "deleted")
             # Oldest first, so the newest wins the collapse below. A name CAN be
             # duplicated today — nothing has stopped it until now — and reporting the
             # latest attempt is what makes the caller's "already exists" message match
             # what the operator sees on the Functions page.
             .order_by(CloudFunction.created_at.asc()))
    if workload:
        query = query.filter(CloudFunction.workload == workload)
    return {row.name: row for row in query.all()}


def get_function(db: Session, fn_id: str) -> Optional[CloudFunction]:
    return db.query(CloudFunction).filter(CloudFunction.id == fn_id).first()


def invoke_info(db: Session, fn_id: str) -> dict:
    """Everything needed to call the function — including the bearer secret, which
    is why the API gates this on an admin/write permission rather than read."""
    row = get_function(db, fn_id)
    if not row:
        raise CloudFunctionError(f"unknown function {fn_id!r}")
    return {
        "id": row.id,
        "name": row.name,
        "invoke_url": row.invoke_url or "",
        "auth_mode": row.auth_mode or "",
        "shared_secret": config_service.get(f"cloudfn/{row.id}/bearer") or "",
        "invoke_key": config_service.get(f"cloudfn/{row.id}/invoke-key") or "",
        "header": "Authorization",
        "scheme": "Bearer",
    }


# ── Package upload ────────────────────────────────────────────────────────────
#
# One transport for all three clouds: upload to an object store in the SAME cloud
# as the function and let Terraform reference it by bucket + key + content hash.
# GCP forces this (cloudfunctions2 has no inline source option), so matching it on
# AWS and Azure keeps the transport out of the per-cloud dispatch entirely.

# WEBSITE_RUN_FROM_PACKAGE re-reads the blob on every cold start and after every
# restart, so the SAS has to outlive the deploy by a long way — a short-lived token
# produces an app that works today and 403s itself weeks later.
_AZURE_SAS_DAYS = 365


def package_location(cloud: str, fn_id: str, sha256_hex: str) -> dict:
    """Where this artifact will live — resolvable with no I/O, so ``deploy`` can
    record it synchronously and the upload can happen in the background job."""
    key = cloud_function_package.object_key(fn_id, sha256_hex)
    if cloud == "aws":
        bucket = _cfg("function_package_s3_bucket")
        if not bucket:
            raise CloudFunctionError(
                "function_package_s3_bucket is not configured — set it in "
                "Settings → Cloud Functions before deploying to AWS")
        return {"bucket": bucket, "key": key, "uri": f"s3://{bucket}/{key}"}
    if cloud == "gcp":
        bucket = _cfg("function_package_gcs_bucket")
        if not bucket:
            raise CloudFunctionError(
                "function_package_gcs_bucket is not configured — set it in "
                "Settings → Cloud Functions before deploying to GCP")
        return {"bucket": bucket, "key": key, "uri": f"gs://{bucket}/{key}"}
    account = _cfg("storage_azure_account")
    if not account:
        raise CloudFunctionError(
            "storage_azure_account is not configured — Azure Functions need a "
            "storage account for both the Functions host and the "
            "run-from-package blob")
    container = _cfg("function_package_azure_container") or "function-packages"
    return {"bucket": container, "key": key, "storage_account": account,
            "uri": f"https://{account}.blob.core.windows.net/{container}/{key}"}


async def _upload_package(*, cloud: str, fn_id: str, workload: str,
                          sha256_hex: str) -> dict:
    """Rebuild the package and upload it to the object store for ``cloud``.

    Rebuilding rather than carrying the bytes through the job queue is free
    precisely because ``build`` is deterministic — the same inputs give the same
    archive, so the hash recorded at deploy time still matches. Blocking SDK calls
    go through ``asyncio.to_thread``, per the repo's convention.
    """
    import asyncio
    blob, rebuilt_hex, sha256_b64 = cloud_function_package.build(
        cloud=cloud, workload=workload)
    if rebuilt_hex != sha256_hex:
        # Only reachable if the handler source changed between the API call and the
        # job running. Fail loudly: silently deploying different code than the row
        # records would make the package hash a lie.
        raise CloudFunctionError(
            f"package hash changed between staging and deploy "
            f"({sha256_hex[:12]} → {rebuilt_hex[:12]}) — the handler source moved "
            "under a running job; retry the deploy")

    location = package_location(cloud, fn_id, sha256_hex)
    key = location["key"]

    if cloud == "aws":
        import boto3
        from . import aws_service
        client = boto3.client("s3", **aws_service._aws_kwargs(_cfg("aws_region")))
        await asyncio.to_thread(client.put_object, Bucket=location["bucket"],
                                Key=key, Body=blob)
        return {**location, "sha256_b64": sha256_b64}

    if cloud == "gcp":
        from google.cloud import storage
        from . import gcp_service
        client = storage.Client(credentials=gcp_service._gcp_creds(),
                                project=_cfg("gcp_project") or _cfg("gcp_project_id"))
        blob_ref = client.bucket(location["bucket"]).blob(key)
        await asyncio.to_thread(blob_ref.upload_from_string, blob,
                                content_type="application/zip")
        return {**location, "sha256_b64": sha256_b64}

    account = location["storage_account"]
    account_key = await _azure_storage_key(account)
    await asyncio.to_thread(_azure_upload_sync, account, account_key,
                            location["bucket"], key, blob)
    return {**location, "sha256_b64": sha256_b64,
            "storage_key": account_key,
            "sas_url": _azure_sas_url(account, account_key, location["bucket"], key)}


async def _azure_storage_key(account: str) -> str:
    """The storage account key, over ARM.

    There is no settings field for it — the repo's convention (see
    ``azure_service._run_aci_jumpoint_sync``'s caller) is to resolve it from the
    subscription credentials at the point of use, so the key is never stored.
    """
    import asyncio
    from . import azure_service
    cred, sub_id = await azure_service._ensure_creds()
    resource_group = _cfg("azure_resource_group")
    if not resource_group:
        raise CloudFunctionError(
            "azure_resource_group is not configured — it is needed to look up the "
            "storage account key for the run-from-package blob")
    return await asyncio.to_thread(
        azure_service._get_storage_account_key_sync, cred, sub_id,
        resource_group, account)


def _azure_upload_sync(account: str, account_key: str, container: str,
                       key: str, blob: bytes) -> None:
    from azure.storage.blob import BlobServiceClient
    service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=account_key)
    try:
        service.create_container(container)
    except Exception:
        pass  # already exists — the only expected failure here
    service.get_blob_client(container, key).upload_blob(blob, overwrite=True)


def _azure_sas_url(account: str, account_key: str, container: str, key: str) -> str:
    """A read-only SAS URL for the package blob.

    Signing is offline — account key plus an HMAC — so this does NOT require the
    blob to exist. That is what keeps ``terraform destroy`` recoverable after the
    artifact has been cleaned up.
    """
    from datetime import timedelta
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    token = generate_blob_sas(
        account_name=account, container_name=container, blob_name=key,
        account_key=account_key, permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(days=_AZURE_SAS_DAYS))
    return f"https://{account}.blob.core.windows.net/{container}/{key}?{token}"


# ── Deploy ────────────────────────────────────────────────────────────────────

def _record_provenance(row: CloudFunction) -> dict:
    """Capture where the handler source came from, onto ``row``.

    A separate function on purpose, and not just for tidiness: it takes ONLY the row,
    so no secret — and no structure carrying one — is in scope at the log calls.
    ``deploy`` holds the freshly minted shared secret, and logging anywhere in that
    scope is exactly the shape CodeQL's clear-text-logging rule flags. Keeping the
    sink somewhere a secret cannot reach is a better answer than suppressing it.

    Never raises: provenance is metadata and must not be able to fail a deploy.
    """
    try:
        from . import build_provenance
        provenance = build_provenance.collect()
        row.provenance = json.dumps(provenance)
        logger.info("cloudfn: %s built from %s", row.id,
                    build_provenance.describe(provenance))
        return provenance
    except Exception as exc:
        logger.warning("cloudfn: could not record provenance for %s (non-fatal): %s",
                       row.id, type(exc).__name__)
        return {}


def deploy(db: Session, *, cloud: str, region: str, name: str, workload: str,
           created_by: str, network_mode: str = "public",
           subnet_ids: Optional[list] = None, subnet_id: str = "",
           vpc_connector: str = "", security_group_ids: Optional[list] = None,
           vpc_network: str = "", vpc_subnetwork: str = "",
           auth_mode: str = "", environment: Optional[dict] = None,
           secret_environment: Optional[dict] = None,
           timeout_seconds: Optional[int] = None, memory_mb: Optional[int] = None,
           **opts) -> dict:
    """Record and stage a function deploy: validate, mint the bearer secret, build
    and upload the package, write the ``CloudFunction`` row + a deploy ``Job``.
    Does **not** run Terraform — the API schedules :func:`run_deploy_apply`.
    Returns ``{ok, fn_id, job_id, tf_variables}`` (secrets already stripped)."""
    _check_target(workload, cloud)
    _check_front_door(workload,
                      auth_mode=auth_mode or _DEFAULT_AUTH_MODE[cloud],
                      network_mode=network_mode)
    if not region:
        raise CloudFunctionError("region is required")
    # Canonicalise + validate, as the cloud-database path does
    # (api/cloud_databases._resolve_db_region). This one string is what the row, the
    # Terraform vars ("region" on aws/gcp, "location" on azure) and
    # region_config.resolve_region below all key off, so an Azure region on an AWS
    # function — or "West US 2" where only "westus2" works — has to fail HERE rather
    # than 90 seconds into an apply. A FORMAT check, deliberately not a membership
    # test against the catalog: operators run regions we don't enumerate.
    try:
        region = region_catalog.resolve(cloud, region)
    except ValueError as exc:
        raise CloudFunctionError(str(exc)) from exc
    fn_name = normalize_name(name)
    if not _NAME_RE.match(fn_name):
        raise CloudFunctionError(
            f"invalid function name {name!r} — needs 3-60 chars, lowercase "
            "alphanumeric or '-', starting with a letter (the strictest of the "
            "three clouds' rules, so one name is valid everywhere)")

    # Resolve the network BEFORE writing anything, so a misconfigured vpc request
    # fails cleanly instead of leaving a half-created row behind.
    network = _resolved_network(cloud, region, network_mode=network_mode,
                                subnet_ids=subnet_ids, subnet_id=subnet_id,
                                vpc_connector=vpc_connector,
                                security_group_ids=security_group_ids,
                                vpc_network=vpc_network,
                                vpc_subnetwork=vpc_subnetwork)

    # Same reason, and the more important one: a credential pasted into `environment`
    # is leaked the instant the row and the Job are written, so it has to be refused
    # here rather than anywhere downstream.
    _reject_plaintext_secrets(environment)
    # Through _azure_key_vault(), not the name keys directly: the vault is
    # configured as a URL on the Secrets page (secrets_azure_kv_url), and reading
    # only azure_key_vault_name here made every Azure secret_environment deploy fail
    # on a vault that was demonstrably present — _stage_admin_secret had just
    # written the credential INTO it. The bearer path below already resolves it the
    # derived way; this is the same vault.
    secret_env, secret_vars = _secret_environment(
        cloud, secret_environment, vault=_azure_key_vault()[0])
    environment = {**(environment or {}), **secret_env}
    # Against the MERGED map, so a setting supplied as a secret reference counts as
    # supplied. Before the row is written, for the same reason the network is
    # resolved above: no half-created row.
    _check_required_env(workload, environment)
    # The union of what the caller granted explicitly and what secret_environment
    # implies, deduplicated: the same ARN twice is a policy statement that reads
    # like a mistake.
    readable_arns = _csv(opts.get("readable_secret_arns"))
    readable_arns += [arn for arn in secret_vars.get("readable_secret_arns", [])
                      if arn not in readable_arns]

    row = CloudFunction(
        name=fn_name, workload=workload, cloud=cloud, region=region,
        provider=_PROVIDER[cloud], runtime=_RUNTIME[cloud], status="deploying",
        auth_mode=auth_mode or _DEFAULT_AUTH_MODE[cloud],
        network_mode=network_mode,
        network_ref=json.dumps(network) if network else None,
        env_ref=json.dumps(environment or {}),
        created_by=created_by, created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    shared_secret = secrets.token_urlsafe(32)
    config_service.set(f"cloudfn/{row.id}/bearer", shared_secret)
    row.invoke_secret_ref = f"config://cloudfn/{row.id}/bearer"

    # Azure resolves the secret from Key Vault rather than from an app setting, so
    # it has to exist there before terraform runs. Done once, here, where the secret
    # is minted: apply and destroy only rebuild the reference.
    azure_bearer = _stage_azure_bearer(row.id, shared_secret) if cloud == "azure" else {}

    # Build now to pin the hash (and to fail fast on a broken workload), but let the
    # background job do the UPLOAD: it is network I/O, and on Azure it needs async
    # credentials. Determinism makes the rebuild there free and exact.
    _blob, sha256_hex, sha256_b64 = cloud_function_package.build(
        cloud=cloud, workload=workload)
    package = {**package_location(cloud, row.id, sha256_hex),
               "sha256_b64": sha256_b64,
               # Placeholders for the Azure-only secrets: both are stripped from the
               # persisted vars anyway and regenerated by _reinject_secrets at apply.
               "sas_url": "", "storage_key": ""}
    row.package_sha256 = sha256_hex
    row.package_uri = package["uri"]

    # Captured HERE, at the moment the package hash is pinned, so the two describe
    # the same source.
    provenance = _record_provenance(row)
    db.commit()

    # Also handed to the function itself, so a RUNNING function can report what it
    # is rather than you having to trust the row that claims to describe it.
    try:
        from . import build_provenance as _bp
        environment = {**(environment or {}), **_bp.env_for_function(provenance)}
    except Exception:
        pass

    tf_variables = _build_tf_variables(
        cloud=cloud, region=region, name=fn_name, workload=workload,
        package=package, network=network,
        opts={
            "shared_secret": shared_secret,
            "auth_mode": row.auth_mode,
            "timeout_seconds": timeout_seconds,
            "memory_mb": memory_mb,
            "environment": environment or {},
            "project": _cfg("gcp_project") or _cfg("gcp_project_id"),
            "resource_group_name": opts.get("resource_group_name")
            or _cfg("azure_resource_group"),
            "sku_name": opts.get("sku_name") or _cfg("azure_functions_plan_sku"),
            "service_account_email": opts.get("service_account_email")
            or _cfg("gcp_functions_service_account"),
            "ingress_settings": opts.get("ingress_settings", ""),
            "min_instances": opts.get("min_instances", 0),
            "concurrency": opts.get("concurrency", 0),
            "max_instances": opts.get("max_instances", 0),
            "invoker_members": opts.get("invoker_members", []),
            # Credential references. Both spellings reach the module: the generic
            # secret_environment, and the two workload-specific options the pairing
            # path passes. Without this they were dropped silently — the AWS role
            # got no secretsmanager:GetSecretValue and GCP injected nothing, so a
            # paired db_grant adapter deployed cleanly and then failed every grant
            # at cold start.
            "readable_secret_arns": readable_arns,
            "db_admin_secret": opts.get("db_admin_secret", ""),
            "secret_environment": secret_vars.get("secret_environment", {}),
            "azure_bearer": azure_bearer,
        })

    # Build the vars BEFORE create_job so the secret-stripped copy is embedded
    # atomically: the jobs worker polls for pending jobs, and a job created first
    # and populated second can be claimed in the gap and dispatched with no
    # tf_variables at all. Same ordering constraint as cloud_database_service.provision.
    safe_variables = strip_secrets(tf_variables)
    job = job_service.create_job(
        db, job_type="cloudfn_deploy", created_by=created_by,
        metadata={"fn_id": row.id, "cloud": cloud, "workload": workload,
                  "name": fn_name, "tf_variables": safe_variables})
    row.deploy_job_id = job.id
    db.commit()

    return {"ok": True, "fn_id": row.id, "job_id": job.id,
            "tf_variables": safe_variables}


def _package_variables(cloud: str, fn_id: str, sha256_hex: str, sha256_b64: str) -> dict:
    """The module variables naming the package, per cloud.

    Azure is absent on purpose: its module takes a single SAS URL, which is a
    credential and is therefore stripped from persisted vars and rebuilt by
    :func:`_reinject_secrets` from ``row.package_sha256``.
    """
    location = package_location(cloud, fn_id, sha256_hex)
    if cloud == "aws":
        return {"package_key": location["key"], "package_sha256_b64": sha256_b64}
    if cloud == "gcp":
        return {"package_object": location["key"]}
    return {}


def merged_environment(current: dict, changes: dict) -> dict:
    """Settings merged over the current ones, with ``None`` meaning *remove*.

    Removal needs an explicit spelling: a merge alone can only ever add keys, so
    without this there would be no way to stop serving a database once FN_DB_NAMES
    listed it, short of destroying the function.
    """
    return {key: value
            for key, value in {**(current or {}), **(changes or {})}.items()
            if value is not None}


def update_environment(db: Session, *, fn_id: str, environment: dict,
                       created_by: str = "") -> dict:
    """Change a deployed function's NON-SECRET settings and re-apply in place.

    Exists because the alternative is destroy-and-redeploy, and three things do not
    survive that: the endpoint URL, the bearer secret, and the Entitle integration
    registered against both. Adding a database to a ``db_grant`` adapter
    (``FN_DB_NAMES``) is the case that needs it — the adapter's whole point is being
    a stable endpoint something else already points at.

    Settings are MERGED over the current ones; a key set to ``None`` is removed. The
    package is rebuilt from the running image, so an update also brings the function
    up to the code that image ships — deterministically a no-op when it is the same
    image, and never a silent downgrade when it is not.
    """
    row = get_function(db, fn_id)
    if not row:
        raise CloudFunctionError(f"unknown function {fn_id!r}")
    if row.status not in ("available", "failed"):
        raise CloudFunctionError(
            f"function is {row.status} — wait for the current job to finish")
    if not row.deploy_job_id:
        raise CloudFunctionError(
            "this function has no deploy job recorded, so terraform has nothing to "
            "update in place; redeploy it instead")
    job = db.query(Job).filter(Job.id == row.deploy_job_id).first()
    persisted = ((job.metadata_dict or {}).get("tf_variables") if job else None) or {}
    if not persisted:
        raise CloudFunctionError(
            "the original deploy job's variables are gone, so the function cannot be "
            "updated in place; redeploy it instead")

    _reject_plaintext_secrets(environment)
    merged = merged_environment(json.loads(row.env_ref or "{}"), environment)

    # Rebuild to pin the hash, exactly as deploy does — the upload in the background
    # job re-derives it and refuses to proceed if the source moved underneath.
    _blob, sha256_hex, sha256_b64 = cloud_function_package.build(
        cloud=row.cloud, workload=row.workload)
    row.package_sha256 = sha256_hex
    row.package_uri = package_location(row.cloud, row.id, sha256_hex)["uri"]
    row.env_ref = json.dumps(merged)
    provenance = _record_provenance(row)
    row.updated_at = datetime.utcnow()
    db.commit()

    environment_vars = dict(merged)
    try:
        from . import build_provenance as _bp
        environment_vars.update(_bp.env_for_function(provenance))
    except Exception:
        pass

    tf_variables = {**persisted, "environment": environment_vars,
                    **_package_variables(row.cloud, row.id, sha256_hex, sha256_b64)}
    update_job = job_service.create_job(
        db, job_type="cloudfn_update", created_by=created_by,
        metadata={"fn_id": row.id, "cloud": row.cloud, "workload": row.workload,
                  "name": row.name, "tf_variables": strip_secrets(tf_variables)})
    db.commit()
    return {"ok": True, "fn_id": row.id, "job_id": update_job.id,
            "environment": merged,
            "tf_variables": strip_secrets(tf_variables)}


async def run_update_apply(db: Session, *, fn_id: str, job_id: str,
                           tf_variables: dict) -> None:
    """Background task: re-apply the function with changed settings.

    Applies in the ORIGINAL deploy job's directory, which is what makes this an
    update rather than a second function — the terraform state for this function
    lives under that job's key in the state backend.
    """
    row = get_function(db, fn_id)
    if not row:
        logger.warning("cloudfn update: row %s vanished", fn_id)
        return
    job_service.set_running(db, job_id)
    try:
        await _upload_package(cloud=row.cloud, fn_id=row.id,
                              workload=row.workload, sha256_hex=row.package_sha256)
        variables = await _reinject_secrets(row, tf_variables)
        outputs = await terraform.apply(
            _deploy_dir(row.deploy_job_id), variables,
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 5, "Updating the function…"),
        )
        # Re-read rather than assume: an update must not leave the row describing
        # something the cloud no longer matches.
        row.resource_id = str(outputs.get("resource_id") or row.resource_id or "")
        row.invoke_url = str(outputs.get("invoke_url") or row.invoke_url or "")
        row.status = "available"
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={
            "fn_id": row.id, "invoke_url": row.invoke_url})
    except Exception as exc:
        # Deliberately NOT marking the row failed: a refused apply leaves the
        # function serving its previous configuration, and flagging a working
        # endpoint as broken sends people to redeploy something that is fine.
        logger.error("cloudfn: update failed for %s: %s", fn_id, exc)
        job_service.set_failed(db, job_id, str(exc))


# Terraform line → (pct, message). GCP dominates this timeline: every deploy runs
# Cloud Build, so "still creating" is the normal state for a minute or two and the
# progress bar must not look stuck.
_FN_MILESTONES = [
    ("plan:", 15, "Planning…"),
    ("creating...", 35, "Creating the function…"),
    ("still creating", 55, "Building and deploying (GCP builds a container — 1-2 min)…"),
    ("creation complete", 85, "Function created; wiring the endpoint…"),
    ("destroying...", 40, "Destroying the function…"),
    ("still destroying", 60, "Destroying the function…"),
    ("destruction complete", 90, "Cleaning up…"),
]


def _job_stream(job_id: str, start_pct: int, start_msg: str):
    """``on_line`` callback streaming terraform output to the job's Live Output and
    advancing a coarse progress bar. The per-line broadcast also heartbeats the job
    row, which the startup reconcile uses to tell a live job from a dead one."""
    from ..api.websocket import broadcast_progress
    state = {"pct": start_pct, "msg": start_msg}

    async def on_line(line: str) -> None:
        job_service.cancel_check(job_id, state)
        low = line.lower()
        for needle, pct, msg in _FN_MILESTONES:
            if needle in low:
                state["pct"], state["msg"] = max(state["pct"], pct), msg
                break
        await broadcast_progress(job_id, state["pct"], state["msg"], log_line=line)

    return on_line


async def _reinject_secrets(row: CloudFunction, tf_variables: dict) -> dict:
    """Put every stripped secret back before calling terraform.

    Needed by apply AND destroy: ``terraform destroy`` evaluates the module config
    and errors on any unset required variable, so a destroy with the persisted
    (stripped) var set fails before it deletes anything.
    """
    variables = dict(tf_variables or {})
    # get_fresh, not get: on a first deploy this key was written by the APP process
    # moments ago and the runner claims the job within one 2s poll — inside the config
    # cache's 5s TTL, so a cached read returns "" for a row that is certainly there.
    # `shared_secret` is a required variable in the aws_lambda and gcp_cloudrun modules,
    # so an empty read fails the apply on "No value for required variable". Same defect
    # cloud_database_service.run_provision_apply had with the DB admin password.
    secret = config_service.get_fresh(f"cloudfn/{row.id}/bearer")
    if row.cloud == "azure":
        # Rebuild the Key Vault reference rather than the value. Also covers a
        # function deployed before the vault path existed: it has no persisted
        # reference, so it keeps using its literal app setting until redeployed.
        variables.update(azure_bearer_reference(row.id)
                         if variables.get("shared_secret_kv_uri") else {})
    if secret and not variables.get("shared_secret_kv_uri"):
        variables["shared_secret"] = secret
    if row.cloud == "azure" and row.package_sha256:
        account = _cfg("storage_azure_account")
        container = _cfg("function_package_azure_container") or "function-packages"
        if account:
            account_key = await _azure_storage_key(account)
            key = cloud_function_package.object_key(row.id, row.package_sha256)
            variables["storage_account_access_key"] = account_key
            variables["package_sas_url"] = _azure_sas_url(
                account, account_key, container, key)
    # Last: a function deployed by an older build persisted variables its module may
    # since have dropped, and every one of them would fail this run outright.
    return _drop_undeclared(row.cloud, variables)


async def run_deploy_apply(db: Session, *, fn_id: str, job_id: str,
                           tf_variables: dict) -> None:
    """Background task: upload the package, ``terraform apply`` the cloud's module,
    then fill the live fields on the row. Marks the job + row failed on error."""
    row = get_function(db, fn_id)
    if not row:
        logger.warning("cloudfn apply: row %s vanished", fn_id)
        return
    job_service.set_running(db, job_id)
    try:
        await _upload_package(cloud=row.cloud, fn_id=row.id,
                              workload=row.workload, sha256_hex=row.package_sha256)
        variables = await _reinject_secrets(row, tf_variables)
        outputs = await terraform.apply(
            _deploy_dir(job_id), variables, template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 5, "Deploying the function…"),
        )
        row.resource_id = str(outputs.get("resource_id") or "")
        row.invoke_url = str(outputs.get("invoke_url") or "")
        if outputs.get("network_mode"):
            row.network_mode = str(outputs["network_mode"])
        row.status = "available"
        row.updated_at = datetime.utcnow()
        db.commit()

        if row.cloud == "azure":
            # Non-fatal by design: azurerm exposes no data source for host keys, so
            # this is a separate ARM call, and failing it must not fail a function
            # that is already deployed and already protected by the bearer secret.
            try:
                await _store_azure_host_key(row, str(outputs.get("default_hostname") or ""))
            except Exception as exc:
                logger.warning("cloudfn: could not fetch the Azure host key for %s "
                               "(non-fatal; the bearer secret still applies): %s",
                               row.name, exc)

        job_service.set_completed(db, job_id, result={
            "fn_id": row.id, "invoke_url": row.invoke_url,
            "resource_id": row.resource_id})
    except Exception as exc:
        row.status = "failed"
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("cloudfn: deploy failed for %s: %s", fn_id, exc)
        job_service.set_failed(db, job_id, str(exc))


async def _store_azure_host_key(row: CloudFunction, hostname: str) -> None:
    """Fetch the Function App's default host key over ARM and stash it.

    This is the Azure half of the layered auth: the key is the front door, the
    bearer secret is checked independently inside the handler.
    """
    import asyncio

    import httpx
    from . import azure_service
    credential, subscription = await azure_service._ensure_creds()
    resource_group = _cfg("azure_resource_group")
    # get_token is a blocking call on the sync azure-identity credential.
    token = (await asyncio.to_thread(
        credential.get_token, "https://management.azure.com/.default")).token
    url = (f"https://management.azure.com/subscriptions/{subscription}"
           f"/resourceGroups/{resource_group}/providers/Microsoft.Web/sites/"
           f"{row.name}/host/default/listKeys?api-version=2022-03-01")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        key = (response.json().get("functionKeys") or {}).get("default", "")
    if key:
        config_service.set(f"cloudfn/{row.id}/invoke-key", key)
        row.invoke_key_ref = f"config://cloudfn/{row.id}/invoke-key"


# ── Decommission ──────────────────────────────────────────────────────────────

def start_decommission(db: Session, fn_id: str, created_by: str = "") -> dict:
    row = get_function(db, fn_id)
    if not row:
        raise CloudFunctionError(f"unknown function {fn_id!r}")
    if row.status == "deleting":
        raise CloudFunctionError("a decommission is already running for this function")
    job = job_service.create_job(
        db, job_type="cloudfn_decommission", created_by=created_by,
        metadata={"fn_id": row.id, "cloud": row.cloud, "name": row.name})
    row.status = "deleting"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "fn_id": row.id, "job_id": job.id}


async def _destroy_variables(db: Session, row: CloudFunction) -> dict:
    """Rebuild the full ``-var`` set for destroy from the deploy job's metadata."""
    persisted = {}
    if row.deploy_job_id:
        job = db.query(Job).filter(Job.id == row.deploy_job_id).first()
        if job:
            persisted = (job.metadata_dict or {}).get("tf_variables") or {}
    if not persisted:
        raise CloudFunctionError(
            "the original deploy job's variables are gone, so terraform destroy "
            "cannot be reconstructed — remove the cloud resource manually and "
            "delete the record")
    return await _reinject_secrets(row, persisted)


async def run_decommission(db: Session, *, fn_id: str, job_id: str) -> None:
    """Background task: ``terraform destroy`` the function and mark the row deleted."""
    row = get_function(db, fn_id)
    if not row:
        logger.warning("cloudfn destroy: row %s vanished", fn_id)
        return
    job_service.set_running(db, job_id)
    try:
        deploy_dir = _deploy_dir(row.deploy_job_id or job_id)
        await terraform.destroy(
            deploy_dir, variables=await _destroy_variables(db, row),
            # Passing template_dir lets destroy rebuild the module and re-init the
            # remote backend, so it still works after a container recreate wiped
            # the original deploy dir.
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 10, "Destroying the function…"),
        )
        row.status = "deleted"
        row.updated_at = datetime.utcnow()
        db.commit()
        # The bearer secret and host key are useless once the endpoint is gone.
        for suffix in ("bearer", "invoke-key"):
            try:
                config_service.delete(f"cloudfn/{row.id}/{suffix}")
            except Exception:
                pass
        job_service.set_completed(db, job_id, result={"fn_id": row.id})
    except Exception as exc:
        row.status = "failed"
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("cloudfn: decommission failed for %s: %s", fn_id, exc)
        job_service.set_failed(db, job_id, str(exc))


# ── Test invoke ───────────────────────────────────────────────────────────────

async def run_entitle_register(db: Session, *, fn_id: str, job_id: str,
                               action: str = "register") -> None:
    """Worker entry for a ``cloudfn_entitle_register`` job.

    Registers the function as an Entitle REST integration — the adapter contract it
    already serves — or removes that registration. Mirrors
    ``cloud_database_service.run_entitle_register``: failures are fatal here,
    because a post-hoc request should surface them rather than log and continue.
    """
    from . import entitle_registration_service as entitle

    row = get_function(db, fn_id)
    if not row:
        job_service.set_failed(db, job_id, f"function {fn_id} not found")
        return
    job_service.set_running(db, job_id)
    try:
        if action == "deregister":
            job_service.update_progress(db, job_id, 30, "Removing Entitle integration…")
            state = config_service.get(f"cloudfn/{row.id}/entitle-tfstate") or ""
            if state:
                await entitle.deregister(state)
            config_service.delete(f"cloudfn/{row.id}/entitle-tfstate")
            row.entitle_integration_id = None
        else:
            if not config_service.get_bool(
                    "entitle_registration_enabled",
                    getattr(settings, "entitle_registration_enabled", False)):
                raise CloudFunctionError(
                    "Entitle registration is disabled (set entitle_registration_enabled)")
            if not row.invoke_url:
                raise CloudFunctionError(
                    "function has no endpoint yet — wait for the deploy to finish")
            secret = config_service.get(f"cloudfn/{row.id}/bearer") or ""
            if not secret:
                raise CloudFunctionError(
                    "function has no shared secret; redeploy it before registering")
            job_service.update_progress(db, job_id, 20, "Checking the adapter's config…")
            await _refuse_unconfigured_adapter(db, row)
            job_service.update_progress(db, job_id, 40, "Registering adapter in Entitle…")
            result = await entitle.register_rest(
                name=f"{row.name} ({row.workload})",
                base_url=row.invoke_url,
                shared_secret=secret,
                # The FUNCTION is VPC-attached; the ENDPOINT is public, which is the
                # whole point — so no agent, regardless of network_mode.
                private=False,
                ephemeral=True,
            )
            row.entitle_integration_id = str(result.get("integration_id") or "")
            if not row.entitle_integration_id:
                raise CloudFunctionError("Entitle registration returned no integration id")
            # The tfstate is what deregister needs later; it is not a secret, but it
            # is bulky and per-function, so it lives beside the bearer rather than
            # on the row.
            config_service.set(f"cloudfn/{row.id}/entitle-tfstate",
                               str(result.get("tf_state_json") or ""))
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, {
            "fn_id": fn_id, "action": action,
            "entitle_integration_id": row.entitle_integration_id,
        })
        logger.info("cloudfn entitle %s complete fn_id=%s integration_id=%s",
                    action, fn_id, row.entitle_integration_id)
    except Exception as exc:
        logger.error("cloudfn entitle %s failed fn_id=%s: %s", action, fn_id, exc)
        job_service.set_failed(db, job_id, str(exc))


def start_entitle_register(db: Session, fn_id: str, *, action: str = "register",
                           created_by: str = "") -> dict:
    row = get_function(db, fn_id)
    if not row:
        raise CloudFunctionError(f"unknown function {fn_id!r}")
    if action not in ("register", "deregister"):
        raise CloudFunctionError(f"unknown action {action!r}")
    # Deregister stays unconditional: a registration that should not have happened is
    # exactly the one that most needs removing.
    if action == "register" and not is_entitle_adapter(row.workload):
        raise CloudFunctionError(
            f"the {row.workload!r} workload does not serve the Entitle Remote Adapter "
            "contract, so registering it would create an integration that can never "
            "resolve an asset")
    job = job_service.create_job(
        db, job_type="cloudfn_entitle_register", created_by=created_by,
        metadata={"fn_id": row.id, "action": action, "name": row.name})
    return {"ok": True, "fn_id": row.id, "job_id": job.id, "action": action}


async def invoke(db: Session, *, fn_id: str, payload: Optional[dict] = None,
                 method: str = "POST", path: str = "/") -> dict:
    """Call the function from the dashboard with the right credentials attached.

    This is what makes Phase 1 demoable without a terminal, and it is the fastest
    way to tell "the function is broken" from "my curl was wrong".

    ``method``/``path`` exist because the adapter workloads route on both: every one
    of their operations lives under a sub-path (``/check_config``, ``/get_assets``,
    …) and two are GETs. Hard-coding ``POST /`` made those functions untestable from
    here — the request could only ever come back as the 404 the workload correctly
    returns for an unrouted path.
    """
    row = get_function(db, fn_id)
    if not row:
        raise CloudFunctionError(f"unknown function {fn_id!r}")
    if not row.invoke_url:
        raise CloudFunctionError(
            f"{row.name} has no endpoint yet (status: {row.status})")

    verb = (method or "POST").strip().upper()
    if verb not in ("GET", "POST"):
        raise CloudFunctionError(f"method must be GET or POST (got {method!r})")

    import httpx
    url = _invoke_url(row.invoke_url, path)
    secret = config_service.get(f"cloudfn/{row.id}/bearer") or ""
    headers = {"content-type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    key = config_service.get(f"cloudfn/{row.id}/invoke-key") or ""
    if key:
        # Azure's front door takes the host key as a query param or x-functions-key.
        headers["x-functions-key"] = key

    async with httpx.AsyncClient(timeout=60) as client:
        if verb == "GET":
            # No body on a GET: the adapters' two read routes take none, and sending
            # one makes the request look different from what Entitle actually sends.
            response = await client.get(url, headers=headers)
        else:
            response = await client.post(url, json=payload or {}, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = response.text[:4000]
    return {"status": response.status_code, "body": body, "url": url,
            "elapsed_ms": int(response.elapsed.total_seconds() * 1000)}


def _is_front_door_denial(status: int, body) -> bool:
    """Whether a 401/403 came from the CLOUD's front door rather than the workload.

    The two are different failures wearing the same status code, and only one of
    them is worth waiting on:

    * The **workload** denies with ``fnruntime.auth``'s ``{"error": "unauthorized"}``
      — a JSON body carrying ``error``. That means the shared secret is wrong, which
      no amount of waiting fixes.
    * The **platform** denies before the container ever runs, and its body is not
      ours: Cloud Run answers a bearer it cannot verify as an ID token with an empty
      401, a Lambda Function URL under AWS_IAM with ``{"Message": "Forbidden"}``,
      the Azure host with an empty 401.

    So the test is the body, not the status: ours always names ``error``.
    """
    if status not in (401, 403):
        return False
    return not (isinstance(body, dict) and "error" in body)


# How long the preflight keeps re-asking when the FRONT DOOR is the one refusing.
#
# The pairing path calls /check_config seconds after terraform creates the invoke
# permission, and an IAM change is not effective the moment the API returns it: GCP
# documents up to ~2 minutes for a Cloud Run binding to take hold. Live, a pairing
# was refused at +11s, +44s and +51s past the apply and served normally at +5m —
# so without this the GCP path is a coin flip on every pairing, and the job that
# loses reads as "the adapter is broken" when nothing is.
#
# Waiting costs nothing when the permission is genuinely missing: the same error is
# raised, two minutes later, and it names the binding to go and check.
_FRONT_DOOR_GRACE_SECONDS = 120
_FRONT_DOOR_POLL_SECONDS = 5


async def _refuse_unconfigured_adapter(
        db: Session, row: CloudFunction, *,
        grace_seconds: int = _FRONT_DOOR_GRACE_SECONDS) -> None:
    """Ask the adapter's own ``/check_config`` before publishing it to the tenant.

    Registration is outward-facing: it creates a live integration in Entitle, and an
    adapter with no target resolves no assets — so the integration looks healthy in
    the dashboard and is useless in Entitle, which is the worst place to find out.
    Nothing else catches it. REQUIRED_ENV is a deploy-time check and cannot see a
    setting that is present but wrong, an admin credential the function cannot read,
    or an environment edited after the deploy.

    The adapter answers for itself rather than this re-deriving the rules, which is
    what ``/check_config`` is for. Only an explicit refusal blocks: an unrecognisable
    body is not treated as a failure, so this can never become the reason a working
    pairing job stops working.

    A front-door 401/403 is retried for ``grace_seconds`` — see
    :func:`_is_front_door_denial` and ``_FRONT_DOOR_GRACE_SECONDS``. Everything else,
    including the workload's OWN 401, is answered on the first call.
    """
    import asyncio
    import time

    deadline = time.monotonic() + max(0, int(grace_seconds))
    waited = False
    while True:
        try:
            result = await invoke(db, fn_id=row.id, method="POST",
                                  path="/check_config", payload={})
        except CloudFunctionError:
            raise
        except Exception as exc:
            # Unreachable is disqualifying on its own: Entitle would be pointed at an
            # endpoint that does not answer.
            raise CloudFunctionError(
                f"could not reach {row.name} to check its configuration "
                f"({type(exc).__name__}) — Entitle would be pointed at a dead endpoint"
            ) from exc

        status = int(result.get("status") or 0)
        body = result.get("body")
        if not _is_front_door_denial(status, body):
            break
        if time.monotonic() >= deadline:
            raise CloudFunctionError(
                f"{row.name}'s own platform still refused the call (HTTP {status}) "
                f"{int(grace_seconds)}s after the deploy — the function's front door "
                "never opened, so /check_config could not run and Entitle would be "
                "pointed at an endpoint it cannot invoke either. Check the invoke "
                "permission: on GCP the allUsers → roles/run.invoker binding on the "
                "Cloud Run service, on AWS the Function URL's lambda permission, on "
                "Azure the host key.")
        if not waited:
            waited = True
            logger.info(
                "cloudfn: %s front door answered %s to /check_config; waiting up to "
                "%ss for the invoke permission to take effect",
                row.name, status, int(grace_seconds))
        await asyncio.sleep(_FRONT_DOOR_POLL_SECONDS)

    data = body.get("data") if isinstance(body, dict) else None
    if status != 200:
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("problem") or body.get("error") or "")
        # The workload's own 401 is the one 401 that reaches here, and it means one
        # specific thing: the secret the dashboard holds is not the one the function
        # verifies. Say so — "unauthorized" on its own sends you looking at Entitle.
        if status == 401:
            raise CloudFunctionError(
                f"{row.name} rejected the dashboard's own shared secret — the value in "
                f"cloudfn/{row.id}/bearer is not the FN_SHARED_SECRET the function "
                "verifies. Redeploy the function to mint a matching pair, then "
                "register it.")
        raise CloudFunctionError(
            f"{row.name} answered HTTP {status} to its own /check_config"
            + (f": {detail}" if detail else "")
            + " — fix that before registering it in Entitle")
    if isinstance(data, dict) and data.get("valid") is False:
        problems = [str(p) for p in (data.get("problems") or []) if p]
        raise CloudFunctionError(
            f"{row.name} reports it is not configured"
            + (": " + "; ".join(problems) if problems else "")
            + f". Set what it needs on the function ({', '.join(required_env(row.workload))})"
            " and register it once /check_config reports valid.")


def _invoke_url(base: str, path: str) -> str:
    """``base`` with ``path`` appended, without letting a path escape the function.

    Azure serves under ``/api/<name>`` and the base URL already carries that prefix,
    so this appends rather than replaces — the adapters' routes are relative to
    whatever the platform's own root is, which is exactly what the handler sees
    after ``adapters.from_azure`` strips the prefix.
    """
    suffix = (path or "/").strip()
    # Refuse anything that could re-point the request at another host or climb out
    # of the function's own path; this value reaches an outbound request.
    if "://" in suffix or suffix.startswith("//") or ".." in suffix:
        raise CloudFunctionError(f"invalid path {path!r}")
    suffix = suffix.lstrip("/")
    if not suffix:
        return base
    return base.rstrip("/") + "/" + suffix
