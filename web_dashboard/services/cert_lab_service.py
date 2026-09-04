"""
Certificate-authority provisioning for the Password Safe Certificate plugin's lab.

Builds the **CA** — today a Google Cloud CAS pool with a root CA and an enrollment
service account — and tears it down again. The mTLS endpoint and the CI runner are
ordinary VMs deployed through the normal cloud pages, so they already have auto-delete
timers, ref-counted NAT and Password Safe VM onboarding; nothing here re-implements any
of that.

**Why a CA gets its own row and its own timer.** It is the part with standing cost that
nothing else reclaims. A CAS pool on the DevOps tier bills ~$20/month whether or not it
ever issues a certificate, and an AWS Private CA ~$400/month — which is why a forgotten
private CA is the expensive mistake this feature exists to prevent. VMs stop costing when
they are destroyed and are already swept; a pool is invisible on every page the dashboard
had before this one.

Same contract as the database and cluster paths: every provision records the Terraform
state, and every destroy is fed by that recorded state rather than by hand-typed ids, so
the lifecycle is closed. See docs/infrastructure-as-code.md.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..database import CertLab
from . import expiry_policy, job_service, terraform, terraform_provider_env

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Declared exactly this way on purpose: tests/test_terraform_modules_shipped.py regexes
# `os.path.join(_REPO_ROOT, "terraform", ...)` out of web_dashboard/services/*.py to prove
# every module a service can reach is COPYed into the image and re-included in
# .dockerignore. Build the path any other way and the guard silently stops seeing it —
# and a missing COPY fails only in the PUBLISHED image, at deploy time.
_TEMPLATE_DIRS = {
    "gcp": os.path.join(_REPO_ROOT, "terraform", "cert_ca", "gcp_cas"),
}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")

PROVISION_JOB_TYPE = "certca_provision"
DECOMMISSION_JOB_TYPE = "certca_decommission"

# The inventory kind. Mirrors "database" and "k8s": a first-class reapable resource with
# a row of its own, not a Job row like a VM.
INVENTORY_KIND = "certlab"


class CertLabError(Exception):
    """Raised when certificate-authority provisioning cannot proceed."""


def _cfg(key: str, default: str = "") -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val not in (None, ""):
            return str(val)
    except Exception:
        pass
    from ..config import settings
    val = getattr(settings, key, None)
    return default if val in (None, "") else str(val)


def template_dir(cloud: str) -> str:
    path = _TEMPLATE_DIRS.get((cloud or "").lower())
    if not path:
        raise CertLabError(
            f"no certificate-authority module for cloud {cloud!r} — GCP CAS is the only "
            f"one built. AWS Private CA is a near-copy of it, deliberately not shipped: "
            f"at ~$400/month standing it should be created for a demonstration and "
            f"destroyed immediately after.")
    return path


def _deploy_dir(job_id: str) -> str:
    return os.path.join(_DEPLOYMENTS_DIR, job_id)


def list_labs(db: Session, workgroup: Optional[str] = None) -> list:
    q = db.query(CertLab)
    if workgroup:
        q = q.filter(CertLab.workgroup == workgroup)
    return q.order_by(CertLab.created_at.desc()).all()


def get_lab(db: Session, lab_id: str) -> Optional[CertLab]:
    return db.query(CertLab).filter(CertLab.id == lab_id).first()


def _tf_variables(row: CertLab) -> dict:
    """The -var set. ``terraform destroy`` evaluates the module config too, so it needs
    the identical set — a required variable left unset fails the destroy with "No value
    for required variable", which is the worst possible time to find out."""
    return {"project": row.project or "",
            "location": row.location or "",
            "pool_id": row.pool_id or "",
            "tier": _cfg("cert_gcp_cas_tier", "DEVOPS"),
            "ca_id": f"{row.pool_id}-root",
            "ca_common_name": f"{row.name} Root CA",
            "labels": {"managed-by": "vm-dashboard", "purpose": "certificate-lab"}}


def provision(db: Session, *, name: str, project: str, created_by: str,
              cloud: str = "gcp", location: str = "", pool_id: str = "",
              workgroup: Optional[str] = None) -> dict:
    """Record the CA and enqueue its build. Returns ``{lab_id, job_id}``."""
    cloud = (cloud or "gcp").lower()
    template_dir(cloud)                       # fail here, not in the worker
    name = (name or "").strip()
    if not name:
        raise CertLabError("a certificate authority needs a name")
    if not project:
        raise CertLabError("a GCP project id is required — CAS pools are project-scoped")

    location = location or _cfg("cert_gcp_cas_location", "us-central1")
    # The pool id is what ends up in `pool=` on every managed-system address built
    # against this CA, so it is deterministic rather than random: an operator reading an
    # address back should recognise the pool it names.
    pool_id = (pool_id or f"{name}-pool").strip().lower()

    row = CertLab(name=name, cloud=cloud, backend="gcpcas", project=project,
                  location=location, pool_id=pool_id, status="provisioning",
                  workgroup=workgroup, created_by=created_by,
                  # NULL would mean "never" and never "inherit the default", so the timer
                  # is stamped here, in the provision's own transaction. Extending or
                  # pinning it afterwards is the existing /api/expiry/set path.
                  expires_at=expiry_policy.default_expiry_for_kind(INVENTORY_KIND))
    db.add(row)
    db.flush()

    job = job_service.create_job(
        db, PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"lab_id": row.id, "name": name, "cloud": cloud,
                  "project": project, "location": location, "pool_id": pool_id})
    row.deploy_job_id = job.id
    db.commit()
    logger.info("cert-lab: queued %s CA %r (pool %s) as job %s", cloud, name, pool_id, job.id)
    return {"lab_id": row.id, "job_id": job.id}


async def run_provision_apply(db: Session, *, lab_id: str, job_id: str) -> None:
    """Worker entry point for ``certca_provision``."""
    from ..api.websocket import broadcast_progress
    row = get_lab(db, lab_id)
    if not row:
        logger.warning("cert-lab: row %s vanished before apply", lab_id)
        return
    job_service.set_running(db, job_id)
    try:
        await broadcast_progress(job_id, 10, "Creating the CA pool and root CA…")
        outputs = await terraform.apply(
            _deploy_dir(job_id), _tf_variables(row),
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 10, "Creating the CA pool and root CA…"))
        row.pool_id = str(outputs.get("pool_id") or row.pool_id or "")
        row.location = str(outputs.get("location") or row.location or "")
        # Public by definition — a CA certificate is not a secret — and the mTLS endpoint
        # playbook needs it as an extra_var, so it is stored rather than re-fetched.
        row.ca_chain_pem = str(outputs.get("ca_chain_pem") or "")
        row.enroll_account = str(outputs.get("service_account_email") or "")
        row.status = "available"
        row.error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={
            "lab_id": row.id, "pool_id": row.pool_id,
            "enroll_account": row.enroll_account})
    except Exception as exc:
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("cert-lab: provision failed for %s: %s", lab_id, exc)
        job_service.set_failed(db, job_id, str(exc))


# Coarse progress milestones, matched against lowercased terraform output.
_CA_MILESTONES = (
    ("google_privateca_ca_pool", 30, "Creating the CA pool\u2026"),
    ("google_privateca_certificate_authority", 50, "Creating the root CA\u2026"),
    ("google_service_account", 70, "Creating the enrollment service account\u2026"),
    ("destroying", 40, "Destroying the CA\u2026"),
    ("destruction complete", 85, "Destroyed\u2026"),
)


def _job_stream(job_id: str, start_pct: int, start_msg: str):
    """``on_line`` callback streaming terraform output to the job's Live Output and
    advancing a coarse progress bar. The per-line broadcast also heartbeats the job row,
    which the startup reconcile uses to tell a live job from a dead one."""
    from ..api.websocket import broadcast_progress
    state = {"pct": start_pct, "msg": start_msg}

    async def on_line(line: str) -> None:
        job_service.cancel_check(job_id, state)
        low = line.lower()
        for needle, pct, msg in _CA_MILESTONES:
            if needle in low:
                state["pct"], state["msg"] = max(state["pct"], pct), msg
                break
        await broadcast_progress(job_id, state["pct"], state["msg"], log_line=line)

    return on_line


def start_decommission(db: Session, *, lab_id: str, created_by: str) -> dict:
    """Enqueue teardown. The same entry point the auto-delete sweep calls, so a timer
    that runs out ends in exactly the destroy the button runs — no second code path."""
    row = get_lab(db, lab_id)
    if not row:
        raise CertLabError(f"certificate authority {lab_id} not found")
    if row.status == "decommissioning":
        raise CertLabError(f"{row.name} is already being destroyed")
    row.status = "decommissioning"
    # Clear the timer in the same transaction that starts the teardown: at-most-once,
    # and it stops the next sweep pass from enqueueing a second destroy for the same row.
    row.expires_at = None
    row.updated_at = datetime.utcnow()
    job = job_service.create_job(
        db, DECOMMISSION_JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"lab_id": row.id, "name": row.name, "cloud": row.cloud,
                  "pool_id": row.pool_id})
    db.commit()
    logger.info("cert-lab: queued teardown of %r (pool %s) as job %s",
                row.name, row.pool_id, job.id)
    return {"lab_id": row.id, "job_id": job.id}


async def run_decommission(db: Session, *, lab_id: str, job_id: str) -> None:
    """Worker entry point for ``certca_decommission``.

    Password Safe first, then the CA. A managed system whose CA has been destroyed still
    looks registered and fails every rotation, which is a worse state to leave behind than
    a CA pool with no managed system — that one only costs money, and the sweep would have
    caught it."""
    from ..api.websocket import broadcast_progress
    row = get_lab(db, lab_id)
    if not row:
        logger.warning("cert-lab: row %s vanished before teardown", lab_id)
        return
    job_service.set_running(db, job_id)
    try:
        if row.ps_tf_state:
            await broadcast_progress(job_id, 10, "Removing the Password Safe objects\u2026")
            try:
                from . import cert_ps_service
                await cert_ps_service.deregister(row.ps_tf_state)
                row.ps_tf_state = None
                row.ps_system_id = None
                row.ps_account_id = None
                db.commit()
            except Exception as exc:
                # Never fatal. The CA is the thing that costs money, and a Password Safe
                # object left behind is visible, deletable and free — so a tenant that is
                # unreachable right now must not strand a billing pool.
                logger.warning("cert-lab: Password Safe deregister failed for %s "
                               "(continuing to the CA): %s", lab_id, exc)
                await broadcast_progress(
                    job_id, 15,
                    f"Password Safe deregister failed, continuing to the CA: {exc}")

        await broadcast_progress(job_id, 25, "Destroying the CA pool\u2026")
        await terraform.destroy(
            _deploy_dir(row.deploy_job_id or job_id),
            variables=_tf_variables(row),
            # Rebuilds the module and re-inits the remote backend, so teardown still works
            # after a container recreate wiped the original deploy dir.
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 25, "Destroying the CA pool\u2026"))
        row.status = "deleted"
        row.error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={"lab_id": row.id,
                                                      "pool_id": row.pool_id})
    except Exception as exc:
        # Left "failed" with the timer still cleared, on purpose: a half-destroyed pool
        # needs a human, and re-arming the timer would have the sweep retry a destroy that
        # already failed once, on a loop, silently.
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("cert-lab: teardown failed for %s: %s", lab_id, exc)
        job_service.set_failed(db, job_id, str(exc))


# ── the certificate identity on this CA ───────────────────────────────────────

def address_for(row: CertLab, overrides: Optional[dict] = None) -> str:
    """The managed-system address for a certificate identity issued by THIS CA.

    Composed from the row rather than typed, so `project=`, `location=` and `pool=` can
    never drift from the pool that was actually built — a mismatch there is a CAS 404 at
    the first rotation and reads like a permissions problem."""
    from . import cert_ps_service
    if row.backend != "gcpcas":
        raise CertLabError(f"no address builder for backend {row.backend!r}")
    return cert_ps_service.build_address(
        "gcpcas",
        {"project": row.project or "", "location": row.location or "",
         "pool": row.pool_id or ""},
        overrides)


def start_ps_register(db: Session, *, lab_id: str, account_name: str, created_by: str,
                      action: str = "register", overrides: Optional[dict] = None) -> dict:
    """Enqueue onboarding one certificate identity against this CA."""
    row = get_lab(db, lab_id)
    if not row:
        raise CertLabError(f"certificate authority {lab_id} not found")
    if action == "register" and row.status != "available":
        raise CertLabError(
            f"{row.name} is {row.status}, not available — onboarding an identity against a "
            f"CA that is not built yet produces a managed system that fails every rotation")
    if action == "register":
        # Compose here, in the request, so a bad profile is a 400 the operator can fix
        # while the form is still open rather than a failed job ten seconds later.
        address = address_for(row, overrides)
    else:
        address = row.ps_address or ""
    job = job_service.create_job(
        db, "cert_ps_register", created_by, workgroup=row.workgroup,
        metadata={"lab_id": row.id, "account_name": account_name,
                  "action": action, "address": address})
    db.commit()
    return {"lab_id": row.id, "job_id": job.id, "address": address}


async def run_ps_register(db: Session, *, lab_id: str, job_id: str, account_name: str,
                          action: str = "register", address: str = "") -> None:
    """Worker entry point for ``cert_ps_register``.

    ``address`` is the profile ``start_ps_register`` already composed and validated,
    carried on the job. Recomposing it here would silently drop the per-identity
    overrides the form supplied -- the request holds them, the row does not -- so the
    identity would get a certificate with the CA's defaults instead of the one asked
    for."""
    from ..api.websocket import broadcast_progress
    from . import cert_ps_service
    row = get_lab(db, lab_id)
    if not row:
        logger.warning("cert-lab: row %s vanished before Password Safe registration", lab_id)
        return
    job_service.set_running(db, job_id)
    try:
        if action == "deregister":
            await broadcast_progress(job_id, 20, "Removing the Password Safe objects\u2026")
            if row.ps_tf_state:
                await cert_ps_service.deregister(row.ps_tf_state)
            row.ps_tf_state = row.ps_system_id = row.ps_account_id = None
            db.commit()
            job_service.set_completed(db, job_id, result={"lab_id": row.id})
            return

        # Fall back to recomposing only for a job queued before this argument existed.
        address = address or address_for(row)
        await broadcast_progress(job_id, 20, "Creating the Secrets Safe folder\u2026")
        reg = await cert_ps_service.register(
            system_name=row.name, account_name=account_name, address=address)
        row.ps_system_id = str(reg.get("managed_system_id") or "")
        row.ps_account_id = str(reg.get("managed_account_id") or "")
        row.ps_address = address
        row.ps_tf_state = reg.get("tf_state_json")
        row.updated_at = datetime.utcnow()
        db.commit()
        # No credential change is fired here, deliberately — unlike the k8s token path,
        # which rotates on register to prove the whole path at once. Issuance is meant to
        # be gated by an approval with a reason, and that record is the first thing the
        # demonstration shows. Firing a rotation from the dashboard would produce a
        # certificate nobody approved and quietly remove the point.
        await broadcast_progress(
            job_id, 95,
            "Registered. Run Change Password in BeyondInsight to issue the first "
            "certificate \u2014 Test password correctly fails until then, because no bundle "
            "exists yet.")
        job_service.set_completed(db, job_id, result={
            "lab_id": row.id, "account_name": account_name,
            "managed_system_id": row.ps_system_id,
            "managed_account_id": row.ps_account_id, "address": address})
    except Exception as exc:
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("cert-lab: Password Safe registration failed for %s: %s", lab_id, exc)
        job_service.set_failed(db, job_id, str(exc))
