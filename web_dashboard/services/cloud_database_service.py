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


async def run_provision_apply(
    db: Session, *, db_id: str, job_id: str, engine: str, tf_variables: dict,
) -> None:
    """Background task: drive ``terraform apply`` for the engine module and fill
    the live fields on the ``CloudDatabase`` row. Marks the job + row failed on
    error. Mocked in dev."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        logger.warning("clouddb apply: row %s vanished", db_id)
        return
    job_service.set_running(db, job_id)
    try:
        outputs = await terraform.apply(
            _deploy_dir(job_id), tf_variables, template_dir=template_dir(engine),
        )
        row.instance_id = str(outputs.get("instance_id") or "")
        row.private_host = str(outputs.get("private_host") or "")
        if outputs.get("port"):
            row.port = int(outputs["port"])
        row.status = "available"
        db.commit()
        job_service.set_completed(db, job_id)
        logger.info("clouddb apply complete db_id=%s host=%s", db_id, row.private_host)
    except Exception as exc:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("clouddb apply failed db_id=%s: %s", db_id, exc)


def decommission(db: Session, db_id: str) -> dict:
    """Tear down a managed database: flip status, ``terraform destroy`` the job
    dir, mark decommissioned. (PRA tunnel removal is Phase 2.)"""
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
    if deploy_job and os.path.isdir(_deploy_dir(deploy_job.id)):
        try:
            asyncio.run(terraform.destroy(_deploy_dir(deploy_job.id)))
        except Exception as exc:
            logger.warning("clouddb destroy for %s failed (non-fatal in P1): %s", db_id, exc)

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
    # Phase 1 returns host/port/status; the PRA jump a user opens is Phase 2.
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
