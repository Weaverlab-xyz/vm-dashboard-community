"""
Cloud database infrastructure — the engine/cloud-agnostic service seam (community).

Provisions **private** managed databases (Postgres / MySQL / SQL Server) reached
only through a BeyondTrust PRA tunnel, and records each in the ``cloud_databases``
inventory table. Shaped like the other cloud services; drives Terraform via a
per-job deploy dir (``terraform/deployments/{job_id}``).

Phase 1 implements **postgres + aws** end-to-end on the dashboard side
(record + Terraform variables + apply/destroy plumbing); other engines/clouds
raise ``NotImplementedError``. The PRA tunnel (Phase 2) is brokered with the
``beyondtrust/sra`` Terraform provider (``terraform_pra_service``) — **never
``btapi``** — so MongoDB is not offered in community until the provider ships a
resource. Credentials are stored encrypted in the DB via ``config_service``
(community has no Password Safe dependency).

``provision`` does the synchronous record-keeping and returns; the actual
``terraform apply`` runs in :func:`run_provision_apply` (scheduled as a
background task by the API). The real apply needs cloud creds — dev mocks it.
"""
from __future__ import annotations

import asyncio
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
from ..database import CloudDatabase, Job
from . import config_service, job_service, terraform, terraform_provider_env

logger = logging.getLogger(__name__)

# Community supports the three engines the beyondtrust/sra provider can tunnel
# (no MongoDB resource yet). Phase 1 wires postgres/aws; the rest fan out later.
VALID_ENGINES = {"postgres", "mysql", "sqlserver"}
VALID_CLOUDS = {"aws", "azure", "gcp"}
_IMPLEMENTED = {
    ("postgres", "aws"), ("postgres", "gcp"), ("postgres", "azure"),
    ("mysql", "aws"),
}
_PROVIDER = {
    ("postgres", "aws"): "rds",
    ("postgres", "gcp"): "cloudsql",
    ("postgres", "azure"): "flexibleserver",
    ("mysql", "aws"): "rds",
}

# terraform/<dir> module per (engine, cloud) — relative to repo root (parents[2]).
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATE_DIRS = {
    ("postgres", "aws"): os.path.join(_REPO_ROOT, "terraform", "db_postgres"),
    ("postgres", "gcp"): os.path.join(_REPO_ROOT, "terraform", "db_gcp_postgres"),
    ("postgres", "azure"): os.path.join(_REPO_ROOT, "terraform", "db_azure_postgres"),
    ("mysql", "aws"): os.path.join(_REPO_ROOT, "terraform", "db_mysql"),
}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")

_DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "sqlserver": 1433}


class CloudDatabaseError(Exception):
    pass


def terraform_available() -> bool:
    return shutil.which(settings.terraform_executable) is not None


def template_dir(engine: str, cloud: str) -> str:
    return _TEMPLATE_DIRS[(engine, cloud)]


def _deploy_dir(job_id: str) -> str:
    return os.path.join(_DEPLOYMENTS_DIR, job_id)


def _db_name_from(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_").lower() or "appdb"
    if not slug[0].isalpha():
        slug = "db_" + slug
    return slug[:63]


def _build_tf_variables(
    *, engine: str, cloud: str, region: str, db_id: str, db_name: str,
    master_username: str, master_password: str, opts: dict,
) -> dict:
    """The Terraform -var set for the engine module. Phase 1: postgres/aws.

    The module itself hardcodes ``publicly_accessible = false`` — the private-only
    guarantee lives in the .tf, not in a toggle-able variable.
    """
    if (engine, cloud) == ("postgres", "aws"):
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": opts.get("instance_class", "db.t3.micro"),
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name", ""),
            "vpc_security_group_ids": opts.get("vpc_security_group_ids", []),
            # Attach the force_ssl=0 parameter group the sandbox pre-created, so the
            # PRA protocol tunnel's cleartext jumpoint→RDS connection isn't rejected.
            # Empty config → "" → module falls back to the RDS default group.
            "parameter_group_name": _cfg("aws_db_parameter_group_name"),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("mysql", "aws"):
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": opts.get("instance_class", "db.t3.micro"),
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name", ""),
            "vpc_security_group_ids": opts.get("vpc_security_group_ids", []),
            # MySQL's cleartext knob is require_secure_transport=0 (not
            # rds.force_ssl) — its own mysql8.0-family group the sandbox
            # pre-creates. Empty config → "" → module falls back to RDS default.
            "parameter_group_name": _cfg("aws_db_mysql_parameter_group_name"),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("postgres", "gcp"):
        # The private_network the instance gets its private IP on; the sandbox
        # configures private-services-access on it. ssl_mode defaults inside the
        # module to ALLOW_UNENCRYPTED_AND_ENCRYPTED so the PRA tunnel's cleartext
        # jumpoint→DB connection is accepted (mirrors AWS's force_ssl=0).
        return {
            "project": _cfg("gcp_project") or _cfg("gcp_project_id"),
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "tier": opts.get("tier", "db-f1-micro"),
            "disk_size": opts.get("disk_size", 20),
            "private_network": opts.get("private_network") or _cfg("gcp_db_network") or _cfg("gcp_network"),
            "labels": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("postgres", "azure"):
        # VNet-integrated private Flexible Server. The delegated subnet + private
        # DNS zone are sandbox-created; the module references them. require_secure_
        # transport=OFF (set in the module) is the force_ssl=0 analog for the tunnel.
        return {
            "resource_group_name": opts.get("resource_group_name") or _cfg("azure_resource_group"),
            "location": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "administrator_login": master_username,
            "administrator_password": master_password,
            "sku_name": opts.get("sku_name", "B_Standard_B1ms"),
            "storage_mb": opts.get("storage_mb", 32768),
            "db_name": db_name,
            "delegated_subnet_id": opts.get("delegated_subnet_id") or _cfg("azure_db_subnet_id"),
            "private_dns_zone_id": opts.get("private_dns_zone_id") or _cfg("azure_db_private_dns_zone_id"),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    raise NotImplementedError(f"{engine}/{cloud} Terraform variables not implemented")


def provision(
    db: Session, *, engine: str, cloud: str, region: str, name: str,
    created_by: str, master_username: str = "dbadmin",
    vault_account_group_id: Optional[int] = None,
    jump_group: Optional[str] = None, jumpoint_name: Optional[str] = None,
    pra_credential_ref: Optional[str] = None, **opts,
) -> dict:
    """Record a new managed database: validate, mint the admin credential, write
    the ``CloudDatabase`` row + a provisioning ``Job``, and return the Terraform
    variables the apply will use. Does **not** run Terraform — the API schedules
    :func:`run_provision_apply`. Returns ``{ok, db_id, job_id, tf_variables}``.
    """
    if engine not in VALID_ENGINES:
        raise CloudDatabaseError(f"unknown engine {engine!r} (expected one of {sorted(VALID_ENGINES)})")
    if cloud not in VALID_CLOUDS:
        raise CloudDatabaseError(f"unknown cloud {cloud!r} (expected one of {sorted(VALID_CLOUDS)})")
    if not region:
        raise CloudDatabaseError("region is required")
    if (engine, cloud) not in _IMPLEMENTED:
        raise NotImplementedError(
            f"{engine} on {cloud} is not available yet"
        )

    row = CloudDatabase(
        engine=engine,
        provider=_PROVIDER.get((engine, cloud)),
        cloud=cloud,
        region=region,
        port=_DEFAULT_PORTS.get(engine),
        status="provisioning",
        created_by=created_by,
        created_at=datetime.utcnow(),
        jump_group=(jump_group or "").strip() or None,
        jumpoint_name=(jumpoint_name or "").strip() or None,
        pra_credential_ref=(pra_credential_ref or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Mint the admin master credential and stash it via the encrypted config
    # store — never returned in plaintext after this point.
    master_password = secrets.token_urlsafe(24)
    config_service.set(f"clouddb/{row.id}/admin", master_password)
    row.credentials_ref = f"config://clouddb/{row.id}/admin"
    db.commit()

    job_meta = {"db_id": row.id, "engine": engine, "cloud": cloud, "name": name}
    if vault_account_group_id:
        # Carried via job metadata (not tf_variables — those map 1:1 to the
        # cloud module's declared variables) for _broker_tunnel to pick up.
        job_meta["vault_account_group_id"] = int(vault_account_group_id)
    job = job_service.create_job(
        db, job_type="clouddb_provision", created_by=created_by,
        metadata=job_meta,
    )

    tf_variables = _build_tf_variables(
        engine=engine, cloud=cloud, region=region, db_id=row.id,
        db_name=_db_name_from(name), master_username=master_username,
        master_password=master_password, opts=opts,
    )
    logger.info("clouddb provisioned record db_id=%s engine=%s cloud=%s job_id=%s",
                row.id, engine, cloud, job.id)
    return {"ok": True, "db_id": row.id, "job_id": job.id, "tf_variables": tf_variables}


def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val:
        return val
    return getattr(settings, key, "") or ""


# Provider credentials for the terraform subprocess moved to the shared
# services/terraform_provider_env module (reused by k8s_service); call sites use
# terraform_provider_env.provider_env(cloud).


def _pra_configured() -> bool:
    """True when a PRA/SRA appliance + Jumpoint + Jump Group are configured —
    the prerequisites for brokering a tunnel. When false, a DB is still
    provisioned/recorded; it just isn't reachable until PRA is set up."""
    return all(_cfg(k) for k in ("bt_api_host", "bt_jumpoint_name", "bt_jump_group_name"))


def _pscli_configured() -> bool:
    """True when the Password Safe OAuth client (shared by ps-cli and the
    ps_api_service REST calls) is configured — the gate for staging the DB
    admin credential as a functional account + Secrets Safe secret."""
    return all(_cfg(k) for k in ("pscli_api_url", "pscli_client_id", "pscli_client_secret"))


async def _resolve_ecs_deploy_key() -> str:
    """BeyondTrust Jumpoint Docker deploy key — same resolution as the EC2
    deploy flow (api/aws.py:_resolve_aws_ecs_deploy_key): direct config field
    first, then the legacy Password-Safe title. Empty when neither is set."""
    direct = _cfg("aws_ecs_docker_deploy_key")
    if direct:
        return direct
    title = _cfg("bt_ps_deploy_key_title")
    if title:
        from . import btapi_service
        try:
            return await btapi_service.get_ps_secret(title)
        except Exception as exc:
            logger.warning("clouddb: deploy key fetch from Password Safe failed: %s", exc)
    return ""


async def _ensure_jumpoint_node(region: str) -> None:
    """Make sure the ECS-hosted Jumpoint (the PRA gateway) has at least one
    live node before brokering a tunnel. The tunnel jump item can be created
    with zero nodes online, but it shows 'Unavailable' in PRA until a node
    registers — so check the Jumpoint cluster for a running task and start one
    (the same launch the EC2 deploy flow does) only when there is none.
    Non-fatal throughout, like the EC2 path."""
    from . import aws_service
    cluster = _cfg("bt_ecs_cluster")
    family = _cfg("bt_ecs_task_family")
    try:
        tasks = await aws_service.list_ecs_tasks(region, cluster)
    except Exception as exc:
        logger.warning("clouddb: could not list Jumpoint ECS tasks (%s) — skipping node check", exc)
        return
    live = [t for t in tasks
            if t.get("lastStatus") in ("PROVISIONING", "PENDING", "RUNNING")
            and f"task-definition/{family}:" in (t.get("taskDefinitionArn") or "")]
    if live:
        logger.info("clouddb: Jumpoint node already up (%d live task(s) in cluster %s)",
                    len(live), cluster)
        return

    launch_type = (_cfg("bt_ecs_launch_type") or "EC2").upper()
    subnet_id = _cfg("bt_ecs_jumpoint_subnet_id")
    sg_ids = [s.strip() for s in _cfg("bt_ecs_jumpoint_security_group_id").split(",") if s.strip()]
    deploy_key = await _resolve_ecs_deploy_key()
    # EC2/host-networking takes its network from the container instance (the
    # sandbox provisions it), so only the deploy key is required here; the
    # legacy FARGATE path still needs a task subnet + SG.
    missing_net = (launch_type != "EC2") and not (subnet_id and sg_ids)
    if not deploy_key or missing_net:
        need = "aws_ecs_docker_deploy_key"
        if missing_net:
            need += ", bt_ecs_jumpoint_subnet_id, bt_ecs_jumpoint_security_group_id"
        logger.warning(
            "clouddb: no live Jumpoint node and cannot auto-start one — set %s. "
            "The tunnel will show Unavailable in PRA until a Jumpoint node is online.",
            need)
        return
    try:
        arn = await aws_service.run_ecs_jumpoint_task(
            region=region,
            cluster=cluster,
            task_family=family,
            subnet_id=subnet_id,
            security_group_ids=sg_ids,
            deploy_key=deploy_key,
            cpu=_cfg("bt_ecs_cpu"),
            memory=_cfg("bt_ecs_memory"),
            execution_role_arn=_cfg("bt_ecs_execution_role_arn"),
            image=_cfg("bt_ecs_image"),
            launch_type=launch_type,
        )
        logger.info("clouddb: started Jumpoint ECS node %s (launch_type=%s) — "
                    "registers with PRA in ~1-2 min", arn.split("/")[-1], launch_type)
    except Exception as exc:
        logger.warning("clouddb: Jumpoint ECS node launch failed (non-fatal): %s", exc)


async def _broker_tunnel(db: Session, *, row: CloudDatabase, job_id: str,
                         engine: str, tf_variables: dict) -> None:
    """Phase 2: provision a PRA protocol-tunnel jump to the private DB via the
    beyondtrust/sra provider, record ``jump_item_id`` on the row, and stash the
    tunnel's Terraform state in the provisioning job's metadata for teardown.
    Non-fatal: a failure leaves the DB up with no tunnel (retryable)."""
    from . import terraform_pra_service as pra
    # The shared Jumpoint host was ensured at the start of run_provision_apply
    # (so its ~2-min boot overlaps the RDS apply); ensure again here — idempotent
    # and cheap when the host is already up — so the task is running before we
    # broker the tunnel.
    from . import jumpoint_host_service
    try:
        await jumpoint_host_service.ensure_jumpoint_host(row.cloud, _cfg(row.cloud + "_region") or row.region)
    except Exception as exc:
        logger.warning("clouddb: ensure jumpoint host (broker) failed (non-fatal): %s", exc)
    try:
        jump_name = tf_variables.get("identifier") or f"clouddb-{row.id[:8]}"
        job = db.query(Job).filter(Job.id == job_id).first()
        vault_group_id = ((job.metadata_dict or {}).get("vault_account_group_id")
                          if job is not None else None)
        # Per-DB PRA overrides win over the configured defaults.
        cred_ref = row.pra_credential_ref
        client_secret = config_service.resolve_reference(cred_ref) if cred_ref else ""
        # The admin credential's variable name differs per cloud (aws/gcp use
        # master_username/master_password; azure's Flexible Server module uses
        # administrator_login/administrator_password). Normalize so the Vault
        # account is minted for every cloud — otherwise on Azure both resolve
        # empty, want_vault is False, and the tunnel comes up with no credential
        # to inject (no warning, since the vault account is never attempted).
        admin_username = (tf_variables.get("master_username")
                          or tf_variables.get("administrator_login") or "")
        admin_password = (tf_variables.get("master_password")
                          or tf_variables.get("administrator_password") or "")
        tun = await pra.provision_db_tunnel(
            engine=engine,
            name=jump_name,
            hostname=row.private_host,
            jump_group_name=row.jump_group or _cfg("bt_jump_group_name"),
            jumpoint_name=row.jumpoint_name or _cfg("bt_jumpoint_name"),
            client_secret=client_secret,
            username=admin_username,
            database=tf_variables.get("db_name", ""),
            tag="clouddb",
            # Vault account for credential injection at tunnel launch; rides in
            # the same workspace/state so decommission destroys it too. The
            # account group makes it visible to users via group policies.
            admin_password=admin_password,
            vault_account_name=f"{jump_name}-admin",
            vault_account_group_id=vault_group_id,
        )
        row.jump_item_id = tun.get("tunnel_jump_id") or None
        db.commit()
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is not None:
            meta = job.metadata_dict or {}
            meta["tunnel_tf_state"] = tun.get("tf_state_json")   # scrubbed of secrets
            meta["vault_account_id"] = tun.get("vault_account_id")
            job.metadata_dict = meta
            db.commit()
        logger.info("clouddb tunnel brokered db_id=%s jump_item_id=%s vault_account_id=%s",
                    row.id, row.jump_item_id, tun.get("vault_account_id"))
    except Exception as exc:
        logger.warning("clouddb tunnel brokering failed db_id=%s (DB is up, no tunnel): %s",
                       row.id, exc)


async def _store_ps_credentials(db: Session, *, row: CloudDatabase, job_id: str,
                                tf_variables: dict) -> None:
    """Stage the admin credential in BeyondTrust Password Safe:

    1. A FUNCTIONAL ACCOUNT — the privileged account a future Ansible playbook
       will hand to Password Safe when it onboards this DB as a managed system
       and creates a managed account on it.
    2. A Secrets Safe TEXT SECRET holding the connection document — what that
       playbook (and humans) read for the actual account-creation step.

    Both best-effort, independent of each other and of PRA; ids/refs land in
    the provisioning job's metadata so decommission can retire them."""
    if not _pscli_configured():
        logger.info("clouddb: Password Safe (pscli_*) not configured — skipping "
                    "functional-account + Secrets Safe staging for db_id=%s", row.id)
        return
    name = f"{tf_variables.get('identifier') or f'clouddb-{row.id[:8]}'}-admin"
    stash: dict = {}
    # Per-cloud credential key normalization (see _broker_tunnel): aws/gcp use
    # master_*, azure's Flexible Server module uses administrator_*.
    admin_username = (tf_variables.get("master_username")
                      or tf_variables.get("administrator_login") or "dbadmin")
    admin_password = (tf_variables.get("master_password")
                      or tf_variables.get("administrator_password") or "")

    try:
        from . import ps_api_service
        fa_id = await ps_api_service.create_functional_account(
            engine=row.engine,
            account_name=admin_username,
            display_name=name,
            password=admin_password,
            description=(
                f"Admin credential for dashboard-provisioned database "
                f"{tf_variables.get('identifier', '')} (db_id={row.id}); used as the "
                f"functional account when the DB is onboarded as a PS managed system."
            ),
        )
        stash["ps_functional_account_id"] = fa_id
        stash["ps_functional_account_name"] = name
        logger.info("clouddb: Password Safe functional account %r created (id=%s) db_id=%s",
                    name, fa_id, row.id)
    except Exception as exc:
        logger.warning("clouddb: functional-account creation failed db_id=%s (non-fatal): %s",
                       row.id, exc)

    try:
        from . import secrets_backend_service
        secret_doc = json.dumps({
            "engine": row.engine,
            "host": row.private_host,
            "port": row.port,
            "database": tf_variables.get("db_name", ""),
            "username": admin_username,
            "password": admin_password,
        })
        ref = await asyncio.to_thread(
            secrets_backend_service.write_bt_secrets_safe, name, secret_doc)
        stash["bt_secret_ref"] = ref
        logger.info("clouddb: Secrets Safe secret stored at %r db_id=%s", ref, row.id)
    except Exception as exc:
        logger.warning("clouddb: Secrets Safe write failed db_id=%s (non-fatal): %s",
                       row.id, exc)

    if stash:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is not None:
            meta = job.metadata_dict or {}
            meta.update(stash)
            job.metadata_dict = meta
            db.commit()


# Generic terraform line → (pct, message) milestones for the DB job's progress bar
# (engine-agnostic phrases, since the resource type varies by cloud).
_DB_MILESTONES = [
    ("plan:",                20, "Planning…"),
    ("creating...",          40, "Creating the database…"),
    ("still creating",       55, "Creating the database (this can take several minutes)…"),
    ("creation complete",    85, "Database created; brokering access…"),
    ("destroying...",        40, "Destroying the database…"),
    ("still destroying",     60, "Destroying the database…"),
    ("destruction complete", 90, "Cleaning up…"),
]


def _job_stream(job_id: str, start_pct: int, start_msg: str):
    """Build an async ``on_line`` callback for ``terraform.apply``/``destroy`` that
    streams each line to the job's Live Output + advances a coarse progress bar.
    The per-line ``broadcast_progress`` also heartbeats the job row, which the
    startup reconcile uses to distinguish a live job from a dead one."""
    from ..api.websocket import broadcast_progress
    state = {"pct": start_pct, "msg": start_msg}

    async def on_line(line: str) -> None:
        low = line.lower()
        for needle, pct, msg in _DB_MILESTONES:
            if needle in low:
                state["pct"], state["msg"] = max(state["pct"], pct), msg
                break
        await broadcast_progress(job_id, state["pct"], state["msg"], log_line=line)

    return on_line


async def run_provision_apply(
    db: Session, *, db_id: str, job_id: str, engine: str, tf_variables: dict,
) -> None:
    """Background task: drive ``terraform apply`` for the engine module, fill the
    live fields on the ``CloudDatabase`` row, then broker the PRA tunnel so the
    private DB is reachable (Phase 2). Marks the job + row failed on apply error.
    Mocked in dev."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        logger.warning("clouddb apply: row %s vanished", db_id)
        return
    job_service.set_running(db, job_id)
    try:
        # Kick the shared Jumpoint host EARLY (only when PRA is configured) so its
        # ~2-min boot overlaps the 5-10-min RDS apply instead of stacking after it.
        if _pra_configured():
            try:
                from . import jumpoint_host_service
                await jumpoint_host_service.ensure_jumpoint_host(row.cloud, _cfg(row.cloud + "_region") or row.region)
            except Exception as exc:
                logger.warning("clouddb: ensure jumpoint host (pre-apply) failed (non-fatal): %s", exc)

        outputs = await terraform.apply(
            _deploy_dir(job_id), tf_variables, template_dir=template_dir(engine, row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 5, "Provisioning the database…"),
        )
        row.instance_id = str(outputs.get("instance_id") or "")
        row.private_host = str(outputs.get("private_host") or "")
        if outputs.get("port"):
            row.port = int(outputs["port"])
        row.status = "available"
        db.commit()

        # Phase 2: broker the PRA tunnel (only when PRA is configured + we have a host).
        if row.private_host and _pra_configured():
            await _broker_tunnel(db, row=row, job_id=job_id, engine=engine, tf_variables=tf_variables)

        # Stage the credential in Password Safe / Secrets Safe — independent of
        # PRA (gated only on pscli_* config). Non-fatal.
        try:
            await _store_ps_credentials(db, row=row, job_id=job_id, tf_variables=tf_variables)
        except Exception as exc:
            logger.warning("clouddb credential staging failed db_id=%s (non-fatal): %s",
                           db_id, exc)

        job_service.set_completed(db, job_id)
        logger.info("clouddb apply complete db_id=%s host=%s tunnel=%s",
                    db_id, row.private_host, row.jump_item_id)
    except Exception as exc:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("clouddb apply failed db_id=%s: %s", db_id, exc)


def start_decommission(db: Session, db_id: str, created_by: str = "") -> dict:
    """Synchronously record the intent to decommission and schedule the work:
    flip the row to ``decommissioning`` and create a ``clouddb_decommission``
    Job. The actual teardown (PRA tunnel+vault, Password Safe, RDS
    ``terraform destroy``) runs in :func:`run_decommission` as a background task
    — it's minutes long and must not block the HTTP request (doing so timed out
    the browser mid-destroy and silently orphaned the Vault account). Returns
    ``{ok, db_id, job_id}``; mirrors :func:`provision`."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"cloud database {db_id} not found")

    # Already in flight — return the existing job rather than starting a second.
    if row.status == "decommissioning":
        existing = (db.query(Job)
                      .filter(Job.job_type == "clouddb_decommission")
                      .order_by(Job.created_at.desc()).all())
        job = next((j for j in existing if (j.metadata_dict or {}).get("db_id") == db_id), None)
        if job:
            return {"ok": True, "db_id": db_id, "job_id": job.id}

    row.status = "decommissioning"
    db.commit()
    job = job_service.create_job(
        db, job_type="clouddb_decommission", created_by=created_by or row.created_by or "system",
        metadata={"db_id": db_id, "engine": row.engine, "cloud": row.cloud},
    )
    return {"ok": True, "db_id": db_id, "job_id": job.id}


async def run_decommission(db: Session, *, db_id: str, job_id: str) -> None:
    """Background teardown for a managed database. Removes the PRA tunnel + its
    Vault account, the staged Password Safe artifacts, and the RDS instance.
    Each step's failure is ACCUMULATED (not swallowed): any real teardown error
    leaves the row ``failed`` and the job ``failed`` with the details, so an
    orphaned Vault account / tunnel / instance is visible rather than hidden
    behind a false ``decommissioned``. Steps that simply didn't apply (PRA/PS
    never configured) are skips, not failures."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        job_service.set_failed(db, job_id, f"cloud database {db_id} not found")
        return
    job_service.set_running(db, job_id)
    errors: list[str] = []
    warnings: list[str] = []

    jobs = (db.query(Job)
              .filter(Job.job_type == "clouddb_provision")
              .order_by(Job.created_at.desc()).all())
    deploy_job = next((j for j in jobs if (j.metadata_dict or {}).get("db_id") == db_id), None)
    meta = (deploy_job.metadata_dict or {}) if deploy_job else {}

    # 1. PRA tunnel + Vault account (the vault account rides in the tunnel's
    #    Terraform state and is destroyed with it).
    job_service.update_progress(db, job_id, 10, "Removing PRA tunnel + Vault account…")
    tun_state = meta.get("tunnel_tf_state")
    if tun_state:
        try:
            from . import terraform_pra_service as pra
            await pra.remove_db_tunnel(tun_state)
            logger.info("clouddb tunnel + vault removed db_id=%s", db_id)
        except Exception as exc:
            errors.append(f"PRA tunnel/Vault removal: {exc}")
            logger.warning("clouddb tunnel removal for %s failed: %s", db_id, exc)

    # 2. Password Safe functional account.
    fa_id = meta.get("ps_functional_account_id")
    if fa_id:
        job_service.update_progress(db, job_id, 35, "Removing Password Safe functional account…")
        try:
            from . import ps_api_service
            await ps_api_service.delete_functional_account(int(fa_id))
            logger.info("clouddb functional account %s deleted db_id=%s", fa_id, db_id)
        except Exception as exc:
            errors.append(f"Password Safe functional account: {exc}")
            logger.warning("clouddb functional-account delete for %s failed: %s", db_id, exc)

    # 3. Secrets Safe secret.
    secret_ref = meta.get("bt_secret_ref")
    if secret_ref:
        job_service.update_progress(db, job_id, 45, "Removing Secrets Safe secret…")
        try:
            from . import secrets_backend_service
            await asyncio.to_thread(secrets_backend_service.delete_bt_secrets_safe, secret_ref)
            logger.info("clouddb Secrets Safe secret %r deleted db_id=%s", secret_ref, db_id)
        except Exception as exc:
            errors.append(f"Secrets Safe secret: {exc}")
            logger.warning("clouddb secrets-safe delete for %s failed: %s", db_id, exc)

    # 4. The RDS instance itself (the long step).
    job_service.update_progress(db, job_id, 60, "Destroying the database instance…")
    if deploy_job:
        try:
            # terraform destroy still evaluates the module config, so it needs the
            # same -var set apply used (without it: "No value for required
            # variable"). The values don't change what's destroyed — resources
            # come from state — but every declared var must be set and provider
            # vars (e.g. the google provider's project/region) must be right.
            # Reconstruct from the row + config; the minted admin password is still
            # in the config store (deleted only after a clean decommission, below).
            destroy_vars = _build_tf_variables(
                engine=row.engine, cloud=row.cloud, region=row.region, db_id=db_id,
                db_name=_db_name_from(meta.get("name") or "appdb"),
                master_username="dbadmin",
                master_password=config_service.get(f"clouddb/{db_id}/admin") or "unused-on-destroy",
                opts={},
            )
            # State lives in the active storage backend, so destroy recovers even
            # if the deploy dir was lost to a container recreate — pass template_dir
            # so terraform.destroy rebuilds the module from it + the remote state.
            await terraform.destroy(
                _deploy_dir(deploy_job.id), variables=destroy_vars,
                env=terraform_provider_env.provider_env(row.cloud),
                template_dir=template_dir(row.engine, row.cloud),
                on_line=_job_stream(job_id, 60, "Destroying the database…"),
            )
            logger.info("clouddb instance destroyed db_id=%s cloud=%s", db_id, row.cloud)
        except Exception as exc:
            errors.append(f"DB destroy: {exc}")
            logger.warning("clouddb destroy for %s failed: %s", db_id, exc)
    else:
        errors.append("no provisioning job recorded for this database — the instance "
                      "may need manual termination in the cloud console")

    if errors:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, "; ".join(errors + warnings))
        logger.error("clouddb decommission db_id=%s ended with errors: %s", db_id, errors)
        return

    row.status = "decommissioned"
    db.commit()
    # Retire the minted admin credential from the encrypted config store too.
    config_service.delete(f"clouddb/{db_id}/admin")

    # Terminate the shared Jumpoint host if nothing is left using it (best-effort;
    # the row is no longer active, so it's excluded from the count).
    job_service.update_progress(db, job_id, 90, "Reclaiming idle Jumpoint host…")
    try:
        from . import jumpoint_host_service
        await jumpoint_host_service.teardown_jumpoint_host_if_idle(db, row.cloud, _cfg(row.cloud + "_region") or row.region)
    except Exception as exc:
        warnings.append(f"Jumpoint host teardown: {exc}")
        logger.warning("clouddb: jumpoint host idle-teardown failed (non-fatal): %s", exc)

    job_service.set_completed(db, job_id, {"db_id": db_id, **({"warnings": warnings} if warnings else {})})
    logger.info("clouddb decommissioned db_id=%s", db_id)


def list_databases(db: Session) -> list[dict]:
    # Hide cleanly-decommissioned rows so old endpoints don't linger; keep
    # available/provisioning/decommissioning and `failed` (a failed decommission
    # is an orphan the operator still needs to see).
    rows = (db.query(CloudDatabase)
              .filter(CloudDatabase.status != "decommissioned")
              .order_by(CloudDatabase.created_at.desc()).all())
    return [_serialize(r) for r in rows]


def connection_info(db: Session, db_id: str) -> dict:
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"cloud database {db_id} not found")
    # jump_item_id is the PRA protocol-tunnel jump a user opens to reach the
    # private DB (populated once the tunnel is brokered; null if PRA is unset).
    return {
        "db_id": row.id, "engine": row.engine, "cloud": row.cloud,
        "status": row.status, "private_host": row.private_host, "port": row.port,
        "jump_item_id": row.jump_item_id,
    }


def _serialize(r: CloudDatabase) -> dict:
    return {
        "id": r.id, "engine": r.engine, "provider": r.provider, "cloud": r.cloud,
        "region": r.region, "instance_id": r.instance_id, "private_host": r.private_host,
        "port": r.port, "status": r.status, "jump_item_id": r.jump_item_id,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
