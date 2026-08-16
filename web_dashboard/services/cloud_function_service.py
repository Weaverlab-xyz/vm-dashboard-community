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
from . import (cloud_function_package, config_service, job_service, terraform,
               terraform_provider_env)
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
}

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

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MEMORY_MB = 256

# Function names: cloud-safe intersection (Lambda allows 64 chars incl. '-'/'_';
# an Azure Function App name becomes a DNS label, so lowercase alphanumeric + '-'
# only, <= 60). Normalising to the strictest rule keeps one name valid everywhere.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,59}$")


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
        })
    return catalog


# ── Terraform variables (PURE — the main unit-test target) ───────────────────

def _resolved_network(cloud: str, region: str, *, network_mode: str,
                      subnet_ids: Optional[list], subnet_id: str,
                      vpc_connector: str, security_group_ids: Optional[list]) -> dict:
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

    regional = {}
    try:
        regional = resolve_region(cloud, region) or {}
    except Exception:
        regional = {}

    if cloud == "aws":
        subnets = [s for s in (subnet_ids or []) if s] or _csv(
            _cfg("aws_functions_subnet_ids")) or _csv(regional.get("db_subnet_group_name", ""))
        groups = [g for g in (security_group_ids or []) if g] or _csv(
            _cfg("aws_functions_security_group_ids")) or _csv(
                regional.get("db_security_group_id", ""))
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
        chosen = (subnet_id or "").strip() or _cfg("azure_functions_subnet_id")
        if not chosen:
            raise CloudFunctionError(
                "network_mode=vpc needs a subnet on Azure — set azure_functions_subnet_id. "
                "It must be DELEGATED TO Microsoft.Web/serverFarms, and is a different "
                "subnet from the database one (delegated to the DB service)")
        return {"subnet_id": chosen}

    chosen = (vpc_connector or "").strip() or _cfg("gcp_functions_vpc_connector")
    if not chosen:
        raise CloudFunctionError(
            "network_mode=vpc needs an existing Serverless VPC Access connector on GCP — "
            "set gcp_functions_vpc_connector (reference one; a per-function connector "
            "costs ~$26/mo whether invoked or not)")
    return {"vpc_connector": chosen}


def _csv(raw) -> list:
    if isinstance(raw, (list, tuple)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _build_tf_variables(*, cloud: str, region: str, name: str, workload: str,
                        package: dict, network: dict, opts: dict) -> dict:
    """The ``-var`` set for the cloud's module. Pure: no config reads, no I/O —
    everything it needs is already resolved by the caller, which is what makes it
    unit-testable without a database or a cloud."""
    common = {
        "name": name,
        "workload": workload,
        "shared_secret": opts["shared_secret"],
        "timeout_seconds": int(opts.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
        "memory_mb": int(opts.get("memory_mb") or _DEFAULT_MEMORY_MB),
        "environment": dict(opts.get("environment") or {}),
    }

    if cloud == "aws":
        return {
            **common,
            "region": region,
            "runtime": _RUNTIME["aws"],
            "auth_mode": opts.get("auth_mode") or _DEFAULT_AUTH_MODE["aws"],
            "package_bucket": package["bucket"],
            "package_key": package["key"],
            "package_sha256_b64": package["sha256_b64"],
            "subnet_ids": network.get("subnet_ids", []),
            "security_group_ids": network.get("security_group_ids", []),
            # AWS is the only cloud with no platform-resolved env-var secret for
            # functions, so a credential-using workload reads it itself and needs
            # the grant. Named ARNs only — never a wildcard.
            "readable_secret_arns": _csv(opts.get("readable_secret_arns")),
        }

    if cloud == "gcp":
        return {
            **common,
            "region": region,
            "project": opts["project"],
            "runtime": _RUNTIME["gcp"],
            "auth_mode": opts.get("auth_mode") or _DEFAULT_AUTH_MODE["gcp"],
            "package_bucket": package["bucket"],
            "package_object": package["key"],
            "service_account_email": opts.get("service_account_email", ""),
            "vpc_connector": network.get("vpc_connector", ""),
            # Injected as FN_DB_ADMIN_PASSWORD by the platform, so the value never
            # reaches Terraform state or the function's describe output.
            "db_admin_secret": opts.get("db_admin_secret", ""),
        }

    # Azure: no bucket/key vars — run-from-package takes a single SAS URL, and the
    # object key still carries the content hash so terraform sees a real diff.
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

def deploy(db: Session, *, cloud: str, region: str, name: str, workload: str,
           created_by: str, network_mode: str = "public",
           subnet_ids: Optional[list] = None, subnet_id: str = "",
           vpc_connector: str = "", security_group_ids: Optional[list] = None,
           auth_mode: str = "", environment: Optional[dict] = None,
           timeout_seconds: Optional[int] = None, memory_mb: Optional[int] = None,
           **opts) -> dict:
    """Record and stage a function deploy: validate, mint the bearer secret, build
    and upload the package, write the ``CloudFunction`` row + a deploy ``Job``.
    Does **not** run Terraform — the API schedules :func:`run_deploy_apply`.
    Returns ``{ok, fn_id, job_id, tf_variables}`` (secrets already stripped)."""
    _check_target(workload, cloud)
    if not region:
        raise CloudFunctionError("region is required")
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
                                security_group_ids=security_group_ids)

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
    db.commit()

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
    secret = config_service.get(f"cloudfn/{row.id}/bearer") or ""
    if secret:
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
    return variables


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
            job_service.update_progress(db, job_id, 30, "Registering adapter in Entitle…")
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
    job = job_service.create_job(
        db, job_type="cloudfn_entitle_register", created_by=created_by,
        metadata={"fn_id": row.id, "action": action, "name": row.name})
    return {"ok": True, "fn_id": row.id, "job_id": job.id, "action": action}


async def invoke(db: Session, *, fn_id: str, payload: Optional[dict] = None) -> dict:
    """Call the function from the dashboard with the right credentials attached.

    This is what makes Phase 1 demoable without a terminal, and it is the fastest
    way to tell "the function is broken" from "my curl was wrong".
    """
    row = get_function(db, fn_id)
    if not row:
        raise CloudFunctionError(f"unknown function {fn_id!r}")
    if not row.invoke_url:
        raise CloudFunctionError(
            f"{row.name} has no endpoint yet (status: {row.status})")

    import httpx
    url = row.invoke_url
    secret = config_service.get(f"cloudfn/{row.id}/bearer") or ""
    headers = {"content-type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    key = config_service.get(f"cloudfn/{row.id}/invoke-key") or ""
    if key:
        # Azure's front door takes the host key as a query param or x-functions-key.
        headers["x-functions-key"] = key

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=payload or {}, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = response.text[:4000]
    return {"status": response.status_code, "body": body,
            "elapsed_ms": int(response.elapsed.total_seconds() * 1000)}
