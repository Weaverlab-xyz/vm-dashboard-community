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
import re
import uuid
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
    "aws": os.path.join(_REPO_ROOT, "terraform", "cert_ca", "aws_pca"),
}

# The plugin's backend name per cloud: what lands in ``CertLab.backend`` and what
# ``cert_ps_service.build_address`` dispatches on. Both names come from
# ``ps_resource_service._CERT_BACKENDS``, which has known ``awspca`` all along — which is
# why the address half of the AWS path needed no change to reach it.
_BACKENDS = {"gcp": "gcpcas", "aws": "awspca"}

# Derived, never maintained beside the registry — the move `vdesktop_service`'s
# PROVISIONING_CLOUDS makes for the same reason. A cloud advertised on the build form
# without a module behind it is exactly the bug this feature's audit item named.
PROVISIONING_CLOUDS = tuple(sorted(_TEMPLATE_DIRS))
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
            f"no certificate-authority module for cloud {cloud!r} — "
            f"built: {', '.join(sorted(_TEMPLATE_DIRS))}.")
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


# ── Naming: why nothing here is derived from the CA's name alone ──────────────
#
# CAS never releases a resource id. A deleted pool's full name —
# projects/<p>/locations/<l>/caPools/<id> — stays reserved for good, and every later
# create with that id fails at the API:
#
#   Error code 3, message: Previously used CaPool ids may not be reused. A `CaPool` for
#   `projects/…/locations/us-central1/caPools/demo-pipeline-pool` has previously been
#   deleted
#
# In a feature whose whole point is that a CA gets DESTROYED, an id derived from the CA's
# name alone can therefore be built exactly once: the first rebuild of that name — and
# every rebuild after it — is refused, permanently, in that project and location. So the
# generated id keeps the name as its readable part and carries a random suffix. The row
# stores what was actually built, so the teardown still names the same pool.
#
# 63 is CAS's cap on a pool id AND on a certificate-authority id, and this module's CA id
# is `<pool>-root` — so a pool id is capped shorter than the pool's own limit, and the
# slug shorter again to leave room for its suffix.
_CAS_ID_MAX = 63
_POOL_ID_MAX = _CAS_ID_MAX - len("-root")
_POOL_SLUG_MAX = _POOL_ID_MAX - len("-pool-") - 6
_POOL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % _POOL_ID_MAX)


def _slug(value: str) -> str:
    """A CAS-legal fragment of a free-text name. Letters, digits, hyphens and underscores
    are all a pool id may contain, and the name on the build form is free text — so
    "Demo Pipeline" has to become `demo-pipeline` here rather than a 400 from the API
    part-way through the apply."""
    out = re.sub(r"[^a-z0-9_-]+", "-", (value or "").lower())
    return out[:_POOL_SLUG_MAX].strip("-_") or "ca"


def _pool_id_for(name: str) -> str:
    """A pool id that is readable and single-use. See the note above — the suffix is what
    makes rebuilding a CA of the same name possible at all."""
    return f"{_slug(name)}-pool-{uuid.uuid4().hex[:6]}"


def _enroll_identity_id(row) -> str:
    """The id of the enrollment identity the plugin authenticates as — a GCP service
    account id, an AWS IAM user name.

    Both modules default it to the constant ``certauth-plugin``, and both namespaces are
    wider than one lab: a service account id is unique per PROJECT and an IAM user name
    per ACCOUNT. The constant therefore means the SECOND CA built in the same project
    collides at create with ``alreadyExists`` — and so does the retry after an apply that
    got as far as creating the identity and then failed, which is exactly what a reused
    pool id does to it.

    Derived from the row id rather than from the pool id, for two reasons: a GCP service
    account id is capped at 30 characters, which most pool ids overflow, and the AWS side
    has no pool to derive from at all. It must be reproducible, because ``_tf_variables``
    feeds the destroy as well as the apply.
    """
    token = str(getattr(row, "id", "") or "").replace("-", "")[:8]
    return f"certauth-{token}" if len(token) >= 6 else "certauth-plugin"


def _tf_variables(row: CertLab) -> dict:
    """The -var set for this row's cloud. ``terraform destroy`` evaluates the module
    config too, so it needs the identical set — a required variable left unset fails the
    destroy with "No value for required variable", which is the worst possible time to
    find out.

    Split per cloud rather than unioned, because terraform treats an UNDECLARED -var as a
    hard error before it touches anything: a union would fail on whichever module did not
    declare the other's variables, and it would fail the destroy as readily as the apply.
    The two modules share nothing but the subject's common name.
    """
    if (row.cloud or "").lower() == "aws":
        # No project (a Private CA is account-scoped) and no pool (there is no such thing
        # — the CA's own ARN is what an address names). `region` carries what `location`
        # carries on the GCP side, which is why the row needs no extra column for it.
        return {"region": row.location or "",
                "ca_common_name": f"{row.name} Root CA",
                # Per row, not the module's constant default: an IAM user name is unique
                # per ACCOUNT, so two labs would collide on it. See _enroll_identity_id.
                "iam_user_name": _enroll_identity_id(row),
                "tags": {"managed-by": "vm-dashboard", "purpose": "certificate-lab"}}
    return {"project": row.project or "",
            "location": row.location or "",
            "pool_id": row.pool_id or "",
            "tier": _cfg("cert_gcp_cas_tier", "DEVOPS"),
            "ca_id": f"{row.pool_id}-root",
            "ca_common_name": f"{row.name} Root CA",
            # Per row, for the same reason — a service account id is unique per PROJECT.
            "service_account_id": _enroll_identity_id(row),
            "labels": {"managed-by": "vm-dashboard", "purpose": "certificate-lab"}}


def _read_outputs(row: CertLab, outputs: dict) -> None:
    """Copy an apply's outputs onto the row, per cloud.

    The two modules deliberately do not emit the same names. Making the AWS module output
    a ``service_account_email`` holding an IAM access key id would put a wrong word in
    the one field an operator reads back when a rotation fails, so the mapping lives here
    instead.
    """
    # Public by definition — a CA certificate is not a secret — and the mTLS endpoint
    # playbook needs it as an extra_var, so it is stored rather than re-fetched.
    row.ca_chain_pem = str(outputs.get("ca_chain_pem") or "")
    if (row.cloud or "").lower() == "aws":
        row.ca_arn = str(outputs.get("ca_arn") or row.ca_arn or "")
        row.location = str(outputs.get("region") or row.location or "")
        row.enroll_account = str(outputs.get("enroll_access_key_id") or "")
        return
    row.pool_id = str(outputs.get("pool_id") or row.pool_id or "")
    row.location = str(outputs.get("location") or row.location or "")
    row.enroll_account = str(outputs.get("service_account_email") or "")


async def _wire_up_functional_account(row: CertLab, outputs: dict) -> str:
    """Mint the CA's functional account from the apply's outputs, onto the row.

    Returns "" on success, or the failure explained — which the caller stores on the row
    while leaving the status ``available``. **A CA that exists must not be rolled back
    because BeyondInsight was unreachable**: the pool is real and billing either way, and
    a rebuild would burn a fresh pool id, since CAS never hands a deleted one back. So
    this reports instead, and `Wire up Password Safe` retries it against the state's own
    copy of the outputs.

    Reporting is not silence: the row renders the error under an available CA, which is
    already how a failed identity registration surfaces here.
    """
    from . import cert_ps_service
    try:
        fa = await cert_ps_service.ensure_functional_account(row, outputs)
    except Exception as exc:                                        # noqa: BLE001
        # str(exc) only. The composed password is never in a CertPSError, and nothing
        # from `outputs` may reach error_message, which the page renders verbatim.
        logger.error("cert-lab: functional account failed for %s: %s", row.id, exc)
        row.error_message = str(exc)[:2000]
        return str(exc)
    # Both modes record the NAME, so Add identity reads the account this CA was built
    # against rather than whatever the global key says now. Only a minted account
    # carries an id, and that is what teardown keys its delete on — reference mode
    # returns None there, which is what stops it deleting an operator's own account.
    row.ps_functional_account = fa.get("account_name") or ""
    row.ps_functional_account_id = fa.get("id")
    return ""


async def rewire_functional_account(db: Session, *, lab_id: str) -> dict:
    """Retry the functional account for a CA whose build could not create one.

    The enrollment credential is long gone from this process, but it IS in the state
    terraform wrote — so this reads the outputs back out of it rather than asking for a
    rebuild. That is the whole reason the retry can exist.

    Synchronous on purpose: it is one API call against Password Safe plus a state read,
    both of which the operator is sitting and watching.
    """
    from . import cert_ps_service
    row = get_lab(db, lab_id)
    if not row:
        raise CertLabError("that certificate authority no longer exists")
    if not row.deploy_job_id:
        raise CertLabError(
            "this CA has no terraform state recorded, so its enrollment credential "
            "cannot be recovered — only a rebuild can produce a new one")
    # A state read fails for its own reasons — a backend credential, a held lock, a
    # state that was never written. Those are not CertPSError, and letting them out raw
    # turns an operator-fixable problem into a 500.
    try:
        outputs = await terraform.read_state_outputs(row.deploy_job_id)
    except Exception as exc:                                        # noqa: BLE001
        raise CertLabError(
            f"could not read {row.name}'s terraform state, which is where its "
            f"enrollment credential still is: {exc}") from exc
    fa = await cert_ps_service.ensure_functional_account(row, outputs)
    row.ps_functional_account = fa.get("account_name") or ""
    row.ps_functional_account_id = fa.get("id")
    row.error_message = None
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"lab_id": row.id, "functional_account": row.ps_functional_account,
            "mode": fa.get("mode")}


def provision(db: Session, *, name: str, project: str, created_by: str,
              cloud: str = "gcp", location: str = "", pool_id: str = "",
              workgroup: Optional[str] = None) -> dict:
    """Record the CA and enqueue its build. Returns ``{lab_id, job_id}``."""
    cloud = (cloud or "gcp").lower()
    template_dir(cloud)                       # fail here, not in the worker
    name = (name or "").strip()
    if not name:
        raise CertLabError("a certificate authority needs a name")
    # NULL would mean "never" and never "inherit the default", so the timer is resolved
    # here, in the provision's own transaction. Extending or pinning it afterwards is the
    # existing /api/expiry/set path.
    expires_at = expiry_policy.default_expiry_for_kind(INVENTORY_KIND)

    if cloud == "aws":
        # An AWS Private CA bills ~$400/month standing, against ~$20/month for a GCP CAS
        # DevOps pool — the figures expiry_policy records — and it bills whether or not it
        # ever issues a certificate. On an instance where the reaper would stamp nothing,
        # building one means creating a resource this dashboard will never take down, so
        # it is refused rather than created and hoped about.
        #
        # GCP is deliberately not held to this: at a twentieth of the cost the same trade
        # does not hold, and tightening it would change behaviour somebody already has.
        #
        # A stamped timer is necessary and not sufficient — the reaper only DELETES when
        # `resource_expiry_enforce` is on and dry-run is off. That half is reported by
        # /api/cert-lab/options rather than refused here, because it is a setting an
        # operator may be part-way through arming, not a reason to have no timer at all.
        if expires_at is None:
            raise CertLabError(
                "an AWS Private CA bills ~$400/month standing, and this instance would "
                "stamp no expiry on it — so nothing here would ever take it down. Set "
                "resource_expiry_enabled with a non-zero resource_expiry_default_hours "
                "before building one. GCP CAs are unaffected.")
        # Regional and account-scoped: no project, and no pool at all — an awspca address
        # names the CA's own ARN, which does not exist until the apply returns it.
        location = location or _cfg("aws_region", "us-east-2")
        pool_id = ""
    elif cloud == "gcp":
        # Default to the project the dashboard is already authenticated against — the same
        # two keys, in the same order, that `terraform_provider_env.gcp_env` reads to build
        # GOOGLE_PROJECT, and the same request-then-config shape `api/desktops.py` uses for
        # its own project.
        #
        # This is worth more than saving a field of typing. The CAS module's provider block
        # sets `project = var.project` EXPLICITLY, which overrides GOOGLE_PROJECT — so this
        # value decides where the pool lands regardless of how the credential is scoped.
        # Defaulting it to the credential's own project is what stops a build landing in a
        # project where the service account has no CAS permission, which fails partway
        # through the apply rather than at the click.
        project = (project or "").strip() or _cfg("gcp_project") or _cfg("gcp_project_id")
        if not project:
            raise CertLabError(
                "a GCP project id is required — CAS pools are project-scoped, and neither "
                "the build form nor gcp_project/gcp_project_id in config supplies one")
        location = location or _cfg("cert_gcp_cas_location", "us-central1")
        # The pool id is what ends up in `pool=` on every managed-system address built
        # against this CA, so the CA's name is the readable part of it — an operator
        # reading an address back should recognise the pool it names. It cannot be ONLY
        # the name, though: CAS reserves a deleted pool's id permanently, so a
        # name-derived id builds once and every rebuild of that name is refused for good.
        # Hence the suffix (see _pool_id_for).
        pool_id = (pool_id or "").strip().lower()
        if not pool_id:
            pool_id = _pool_id_for(name)
        elif not _POOL_ID_RE.match(pool_id):
            # An explicit id is honoured as typed — mangling a value that goes on to
            # identify the pool in every address would be worse — so an unusable one is
            # refused here rather than failing the apply after the identity exists.
            raise CertLabError(
                f"{pool_id!r} cannot be a CAS pool id: CAS accepts letters, digits, "
                f"hyphens and underscores only, and this id also becomes the root CA's "
                f"id as {pool_id}-root, which caps it at {_POOL_ID_MAX} characters. "
                f"Leave it blank to get one derived from the name.")
    else:
        # `template_dir` above proved a module exists, so reaching here means somebody
        # added one and stopped. Loud, because the alternative is a row shaped like a CAS
        # pool on a cloud that has never heard of pools — which fails in the worker, after
        # the record exists.
        raise CertLabError(
            f"{cloud!r} has a module but no provisioning rules — add its branch to "
            f"cert_lab_service.provision alongside _tf_variables and _read_outputs")

    row = CertLab(name=name, cloud=cloud, backend=_BACKENDS[cloud], project=project or "",
                  location=location, pool_id=pool_id, status="provisioning",
                  workgroup=workgroup, created_by=created_by,
                  expires_at=expires_at)
    db.add(row)
    db.flush()

    job = job_service.create_job(
        db, PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"lab_id": row.id, "name": name, "cloud": cloud,
                  "project": project, "location": location, "pool_id": pool_id})
    row.deploy_job_id = job.id
    db.commit()
    logger.info("cert-lab: queued %s CA %r (%s) as job %s", cloud, name,
                pool_id or location, job.id)
    return {"lab_id": row.id, "job_id": job.id}


def _explain_apply_failure(row: CertLab, text: str) -> str:
    """Prefix an apply failure with what the operator has to do, for the two failures
    that read as nothing in particular and have a specific way out.

    Both are id-reuse failures, and both arrive as a wall of plan output with the cause
    on one line near the bottom — which is also the line that gets cut when the message
    is truncated onto the row.
    """
    low = (text or "").lower()
    if "may not be reused" in low:
        return (f"CAS has permanently reserved the pool id {row.pool_id!r} — a deleted "
                f"CaPool's name can never be used again in this project and location, so "
                f"this build cannot succeed as asked. Destroy this row to clear what the "
                f"attempt left behind, then build again leaving the pool id blank: a "
                f"generated id carries a unique suffix for exactly this reason."
                f"\n\n{text}")
    if ("alreadyexists" in low or "already exists" in low) and "certauth-" in low:
        # Read the id out of the provider's own text rather than re-deriving it: on a row
        # built before the id became per-lab the collision is with the old constant, and
        # naming a different account than the error does would send the operator nowhere.
        found = re.search(r"certauth-[a-z0-9_-]*", low)
        ident = found.group(0) if found else _enroll_identity_id(row)
        return (f"The enrollment identity {ident!r} already exists — an earlier attempt "
                f"created it and did not get to clean it up. Destroy this row (its "
                f"teardown removes what the last attempt left behind) and build again."
                f"\n\n{text}")
    return text


async def _rollback_failed_provision(row: CertLab, job_id: str) -> str:
    """Best-effort ``terraform destroy`` of a build that died part-way through. Returns a
    note to append to the failure message, and never raises.

    A failed apply is **not** a no-op in the cloud. This one is the observed case: the
    enrollment service account and its KEY were created, then the pool create was refused
    for a reused id — leaving a live credential behind, and an identity id that then
    collides with the retry. A CA pool bills from the moment it exists, so a partial build
    is exactly as expensive as a whole one.

    Deliberately non-fatal, like ``k8s_service._rollback_failed_provision``: the apply
    error is the thing the operator needs to read, so a rollback that fails must not
    replace it. The row stays ``failed`` either way, and Destroy re-runs the teardown.
    """
    from ..api.websocket import broadcast_progress

    # No job_service.cancel_check in this stream, unlike the apply's: cancelling is one of
    # the ways we get here, and re-checking would abort the rollback on its first line —
    # leaving behind precisely the orphan it exists to clean up.
    async def on_line(line: str) -> None:
        await broadcast_progress(job_id, 90, "Rolling back the failed build…",
                                 log_line=line)

    logger.warning("cert-lab: apply failed for %s — rolling back the partial build",
                   row.id)
    try:
        await broadcast_progress(job_id, 90, "Build failed — rolling back…")
        await terraform.destroy(
            _deploy_dir(job_id),
            variables=_tf_variables(row),
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=on_line)
    except Exception as exc:                                        # noqa: BLE001
        logger.error("cert-lab: rollback FAILED for %s: %s", row.id, exc)
        return ("\n\n[rollback] terraform destroy also failed — MANUAL CLEANUP REQUIRED. "
                "Whatever the apply created is still live: a CA pool bills from the "
                "moment it exists, and an enrollment key left behind is a live "
                f"credential. Destroy this row to retry the teardown. Cause: {exc}")
    logger.info("cert-lab: rollback complete for %s — partial build destroyed", row.id)
    return ("\n\n[rollback] The partial build was destroyed — no cloud resources should "
            "remain from this attempt.")


async def run_provision_apply(db: Session, *, lab_id: str, job_id: str) -> None:
    """Worker entry point for ``certca_provision``."""
    from ..api.websocket import broadcast_progress
    row = get_lab(db, lab_id)
    if not row:
        logger.warning("cert-lab: row %s vanished before apply", lab_id)
        return
    job_service.set_running(db, job_id)
    built = False
    try:
        await broadcast_progress(job_id, 10, "Creating the certificate authority…")
        outputs = await terraform.apply(
            _deploy_dir(job_id), _tf_variables(row),
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 10, "Creating the certificate authority…",
                                row.cloud))
        built = True
        _read_outputs(row, outputs)
        row.status = "available"
        row.error_message = None
        # The CA is real from here on, so nothing below may fail the build. The
        # functional account is the LAST thing that needs the apply's outputs, and it
        # needs them because the enrollment credential is in no other reachable place.
        # Worded for both modes: reference mode records the account rather than creating
        # one, and a progress line that claims otherwise is the kind of small lie that
        # sends somebody looking in BeyondInsight for an object nobody made.
        await broadcast_progress(job_id, 90,
                                 "Wiring up the Password Safe functional account…")
        fa_note = await _wire_up_functional_account(row, outputs)
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={
            "lab_id": row.id, "pool_id": row.pool_id,
            "enroll_account": row.enroll_account,
            # The account's NAME, never its credential.
            "functional_account": row.ps_functional_account or "",
            "functional_account_error": fa_note})
    except Exception as exc:
        # Tear down whatever the apply managed to create BEFORE the row goes failed: a
        # partial build leaves a billing pool, or a live enrollment key, or an identity id
        # that collides with the retry. `built` guards the case where the apply itself
        # succeeded and something after it did not — there is nothing to roll back then,
        # and destroying a CA that exists would be the opposite of the intent.
        note = "" if built else await _rollback_failed_provision(row, job_id)
        # Explained first, then the rollback note: error_message is truncated at 2000
        # characters and the actionable line has to survive that.
        message = f"{_explain_apply_failure(row, str(exc))}{note}"
        row.status = "failed"
        row.error_message = message[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("cert-lab: provision failed for %s: %s", lab_id, exc)
        job_service.set_failed(db, job_id, message)


# Coarse progress milestones, matched against lowercased terraform output. Per cloud,
# because they are resource names: an AWS apply matched against `google_*` needles would
# sit at the starting percentage for the whole build, which reads as a hung job.
_BUILD_MILESTONES = {
    "gcp": (
        ("google_privateca_ca_pool", 30, "Creating the CA pool\u2026"),
        ("google_privateca_certificate_authority", 50, "Creating the root CA\u2026"),
        ("google_service_account", 70, "Creating the enrollment service account\u2026"),
    ),
    "aws": (
        # Ordered as terraform reaches them, and the first two needles cannot swallow the
        # third: `aws_acmpca_certificate_authority_certificate` continues past the point
        # where `aws_acmpca_certificate_authority.` ends, so it never matches that one.
        ("aws_acmpca_certificate_authority.", 30, "Creating the private CA\u2026"),
        ("aws_acmpca_certificate.", 50, "Signing the root certificate\u2026"),
        ("aws_acmpca_certificate_authority_certificate", 60, "Activating the CA\u2026"),
        ("aws_iam_user", 70, "Creating the enrollment identity\u2026"),
    ),
}

# Terraform's own words rather than a provider's, so both clouds share them.
_TEARDOWN_MILESTONES = (
    ("destroying", 40, "Destroying the CA\u2026"),
    ("destruction complete", 85, "Destroyed\u2026"),
)


def _milestones(cloud: str) -> tuple:
    return _BUILD_MILESTONES.get((cloud or "").lower(), ()) + _TEARDOWN_MILESTONES


def _job_stream(job_id: str, start_pct: int, start_msg: str, cloud: str = ""):
    """``on_line`` callback streaming terraform output to the job's Live Output and
    advancing a coarse progress bar. The per-line broadcast also heartbeats the job row,
    which the startup reconcile uses to tell a live job from a dead one."""
    from ..api.websocket import broadcast_progress
    state = {"pct": start_pct, "msg": start_msg}

    async def on_line(line: str) -> None:
        job_service.cancel_check(job_id, state)
        low = line.lower()
        for needle, pct, msg in _milestones(cloud):
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
        deregistered = True
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
                deregistered = False
                # Never fatal. The CA is the thing that costs money, and a Password Safe
                # object left behind is visible, deletable and free — so a tenant that is
                # unreachable right now must not strand a billing pool.
                logger.warning("cert-lab: Password Safe deregister failed for %s "
                               "(continuing to the CA): %s", lab_id, exc)
                await broadcast_progress(
                    job_id, 15,
                    f"Password Safe deregister failed, continuing to the CA: {exc}")

        # Only ever the account this dashboard minted. A NULL id means an operator named
        # their own in reference mode, and deleting that would take out every other CA
        # pointed at it.
        if row.ps_functional_account_id:
            if not deregistered:
                # Its managed system still references it, so the delete would be refused
                # anyway \u2014 and saying why beats a 400 in the log.
                await broadcast_progress(
                    job_id, 18,
                    "Leaving the functional account: its managed system is still "
                    "registered, so it cannot be deleted yet.")
            else:
                await broadcast_progress(
                    job_id, 18,
                    "Deleting the Password Safe functional account\u2026")
                try:
                    from . import ps_api_service
                    await ps_api_service.delete_functional_account(
                        int(row.ps_functional_account_id))
                    # Cleared only on success, so a retried teardown reattempts exactly
                    # the step that failed and skips the one that did not.
                    row.ps_functional_account_id = None
                    row.ps_functional_account = None
                    db.commit()
                except Exception as exc:
                    logger.warning("cert-lab: functional account delete failed for %s "
                                   "(continuing to the CA): %s", lab_id, exc)
                    await broadcast_progress(
                        job_id, 20,
                        f"Functional account delete failed, continuing to the CA: {exc}")

        await broadcast_progress(job_id, 25, "Destroying the certificate authority\u2026")
        await terraform.destroy(
            _deploy_dir(row.deploy_job_id or job_id),
            variables=_tf_variables(row),
            # Rebuilds the module and re-inits the remote backend, so teardown still works
            # after a container recreate wiped the original deploy dir.
            template_dir=template_dir(row.cloud),
            env=terraform_provider_env.provider_env(row.cloud),
            on_line=_job_stream(job_id, 25, "Destroying the certificate authority\u2026",
                                row.cloud))
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

    Composed from the row rather than typed, so the keys that identify the CA — `project=`,
    `location=` and `pool=` on gcpcas, `arn=` and `region=` on awspca — can never drift
    from what was actually built. A mismatch there is a 404 at the first rotation and
    reads like a permissions problem."""
    from . import cert_ps_service
    backend = row.backend or ""
    if backend == "awspca":
        # `arn=` is the one option an awspca address cannot be built without, and it is
        # not known until the apply returns it — so a row still building has nothing to
        # compose from. Saying that beats composing an address the plugin refuses hours
        # later at the first rotation.
        if not row.ca_arn:
            raise CertLabError(
                f"{row.name} has no CA ARN yet — an awspca address is built from it, and "
                f"it is only known once the build finishes")
        return cert_ps_service.build_address(
            "awspca",
            {"arn": row.ca_arn, "region": row.location or ""},
            overrides)
    if backend != "gcpcas":
        raise CertLabError(f"no address builder for backend {backend!r}")
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
        # Same reason as the address below: fail at the click. Both sources are checked
        # because a CA built before the dashboard minted these has a NULL column and an
        # operator-configured account that works perfectly — refusing that would break a
        # lab that is running today.
        from . import config_service
        if not (row.ps_functional_account
                or config_service.get("cert_ps_functional_account")):
            raise CertLabError(
                f"{row.name} has no Password Safe functional account, so an identity "
                f"onboarded against it would fail every credential action. "
                f"Use 'Wire up Password Safe' on the CA first.")
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
        # This CA's OWN functional account, not the global config key. In create mode
        # it is the one minted from this CA's enrollment credential, and no other
        # account can rotate these certificates; falling through to config would onboard
        # against a different CA's identity and fail every credential action.
        reg = await cert_ps_service.register(
            system_name=row.name, account_name=account_name, address=address,
            functional_account=row.ps_functional_account or "")
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
