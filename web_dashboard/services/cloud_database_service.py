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
from . import config_service, job_service, terraform

logger = logging.getLogger(__name__)

# Community supports the three engines the beyondtrust/sra provider can tunnel
# (no MongoDB resource yet). Phase 1 wires postgres/aws; the rest fan out later.
VALID_ENGINES = {"postgres", "mysql", "sqlserver"}
VALID_CLOUDS = {"aws", "azure", "gcp"}
_IMPLEMENTED = {("postgres", "aws")}
_PROVIDER = {("postgres", "aws"): "rds"}

# terraform/<dir> module per engine (relative to repo root → parents[2] of this file).
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATE_DIRS = {"postgres": os.path.join(_REPO_ROOT, "terraform", "db_postgres")}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")

_DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "sqlserver": 1433}


class CloudDatabaseError(Exception):
    pass


def terraform_available() -> bool:
    return shutil.which(settings.terraform_executable) is not None


def template_dir(engine: str) -> str:
    return _TEMPLATE_DIRS[engine]


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
    if (engine, cloud) != ("postgres", "aws"):
        raise NotImplementedError(f"{engine}/{cloud} Terraform variables not implemented")
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
        "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
    }


def provision(
    db: Session, *, engine: str, cloud: str, region: str, name: str,
    created_by: str, master_username: str = "dbadmin", **opts,
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
            f"{engine}/{cloud} is not wired yet — Phase 1 implements postgres/aws"
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

    job = job_service.create_job(
        db, job_type="clouddb_provision", created_by=created_by,
        metadata={"db_id": row.id, "engine": engine, "cloud": cloud, "name": name},
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


def _aws_env() -> Optional[dict]:
    """Provider credentials for the terraform subprocess, mirroring the packer
    flow's env injection: the wizard-stored (encrypted) keys win; when unset,
    return None so terraform falls back to whatever the container environment
    provides (env vars / shared config). Phase 1 is aws-only — provision()
    already rejects other clouds before any apply runs."""
    key_id = _cfg("aws_access_key_id")
    secret = _cfg("aws_secret_access_key")
    if key_id and secret:
        return {"AWS_ACCESS_KEY_ID": key_id, "AWS_SECRET_ACCESS_KEY": secret}
    return None


def _pra_configured() -> bool:
    """True when a PRA/SRA appliance + Jumpoint + Jump Group are configured —
    the prerequisites for brokering a tunnel. When false, a DB is still
    provisioned/recorded; it just isn't reachable until PRA is set up."""
    return all(_cfg(k) for k in ("bt_api_host", "bt_jumpoint_name", "bt_jump_group_name"))


async def _broker_tunnel(db: Session, *, row: CloudDatabase, job_id: str,
                         engine: str, tf_variables: dict) -> None:
    """Phase 2: provision a PRA protocol-tunnel jump to the private DB via the
    beyondtrust/sra provider, record ``jump_item_id`` on the row, and stash the
    tunnel's Terraform state in the provisioning job's metadata for teardown.
    Non-fatal: a failure leaves the DB up with no tunnel (retryable)."""
    from . import terraform_pra_service as pra
    try:
        tun = await pra.provision_db_tunnel(
            engine=engine,
            name=tf_variables.get("identifier") or f"clouddb-{row.id[:8]}",
            hostname=row.private_host,
            jump_group_name=_cfg("bt_jump_group_name"),
            jumpoint_name=_cfg("bt_jumpoint_name"),
            username=tf_variables.get("master_username", ""),
            database=tf_variables.get("db_name", ""),
            tag="clouddb",
        )
        row.jump_item_id = tun.get("tunnel_jump_id") or None
        db.commit()
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is not None:
            meta = job.metadata_dict or {}
            meta["tunnel_tf_state"] = tun.get("tf_state_json")
            job.metadata_dict = meta
            db.commit()
        logger.info("clouddb tunnel brokered db_id=%s jump_item_id=%s", row.id, row.jump_item_id)
    except Exception as exc:
        logger.warning("clouddb tunnel brokering failed db_id=%s (DB is up, no tunnel): %s",
                       row.id, exc)


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
        outputs = await terraform.apply(
            _deploy_dir(job_id), tf_variables, template_dir=template_dir(engine),
            env=_aws_env(),
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

        job_service.set_completed(db, job_id)
        logger.info("clouddb apply complete db_id=%s host=%s tunnel=%s",
                    db_id, row.private_host, row.jump_item_id)
    except Exception as exc:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("clouddb apply failed db_id=%s: %s", db_id, exc)


def decommission(db: Session, db_id: str) -> dict:
    """Tear down a managed database: flip status, remove the PRA tunnel, then
    ``terraform destroy`` the DB, mark decommissioned."""
    import asyncio

    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"cloud database {db_id} not found")

    row.status = "decommissioning"
    db.commit()

    jobs = (db.query(Job)
              .filter(Job.job_type == "clouddb_provision")
              .order_by(Job.created_at.desc()).all())
    deploy_job = next((j for j in jobs if (j.metadata_dict or {}).get("db_id") == db_id), None)

    # Phase 2: remove the PRA tunnel first (best-effort), so we don't orphan a
    # jump item pointing at a host we're about to destroy.
    if deploy_job:
        tun_state = (deploy_job.metadata_dict or {}).get("tunnel_tf_state")
        if tun_state:
            try:
                from . import terraform_pra_service as pra
                asyncio.run(pra.remove_db_tunnel(tun_state))
                logger.info("clouddb tunnel removed db_id=%s", db_id)
            except Exception as exc:
                logger.warning("clouddb tunnel removal for %s failed (non-fatal): %s", db_id, exc)

    if deploy_job and os.path.isdir(_deploy_dir(deploy_job.id)):
        try:
            asyncio.run(terraform.destroy(_deploy_dir(deploy_job.id), env=_aws_env()))
        except Exception as exc:
            logger.warning("clouddb destroy for %s failed (non-fatal): %s", db_id, exc)

    row.status = "decommissioned"
    db.commit()
    logger.info("clouddb decommissioned db_id=%s", db_id)
    return {"ok": True, "db_id": db_id, "status": row.status}


def list_databases(db: Session) -> list[dict]:
    rows = db.query(CloudDatabase).order_by(CloudDatabase.created_at.desc()).all()
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
