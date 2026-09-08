"""
SPIRE Lab API — preview (gated by the ``spire_lab_enabled`` flag).

  GET    /api/spire-lab                  — list dashboard-built SPIRE trust domains
  POST   /api/spire-lab                  — build one on an existing VM
  GET    /api/spire-lab/options          — hosts, defaults, and what config is missing
  GET    /api/spire-lab/{id}             — one lab
  GET    /api/spire-lab/{id}/bundle      — the trust bundle PEM
  GET    /api/spire-lab/{id}/onboarding  — everything §5 asks an operator to paste
  POST   /api/spire-lab/{id}/acl         — re-apply the cloud ACL from current config
  DELETE /api/spire-lab/{id}             — close the ACL (the teardown)

``/onboarding`` is the point of this router. The plugin's Password Safe objects are
**deliberately not written by the dashboard yet** — whether BeyondInsight populates
plugin *attributes* for an action is unresolved (docs/runbooks/spire-lab-standup.md §5),
and an attribute writer built before that is answered would be betting on the answer. So
this route hands over exactly the values §5 needs, already resolved, and nothing else.

``/acl`` exists because corporate egress rotates. A rule pinned to one of two addresses
fails on about half the connections, and the symptom — a gRPC timeout on *Verify
Functional Account* — reads as a credential problem. Re-applying is a one-call fix that
should not require rebuilding a trust domain.

The bundle PEM is deliberately NOT admin-only, for the same reason the Certificate Lab's
chain is not: a trust bundle is a public document by construction — it is what every
consumer of the trust domain has to trust — and the whole lab depends on pasting it into
the managed system.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import config_service, spire_lab_service
from ..services.spire_lab_service import SpireLabError
from .auth import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/spire-lab", tags=["spire-lab"])


def _require_enabled() -> None:
    if not config_service.get_bool("spire_lab_enabled", settings.spire_lab_enabled):
        raise HTTPException(status_code=403, detail="the SPIRE Lab is disabled")


def _row_or_404(db: Session, lab_id: str):
    row = spire_lab_service.get_lab(db, lab_id)
    if not row:
        raise HTTPException(status_code=404, detail="SPIRE lab not found")
    return row


def _visible(row, user: User) -> bool:
    """Creator-scoped for non-admins, exactly like the Certificate Lab and functions."""
    return bool(getattr(user, "is_admin", False)) or row.created_by == user.username


def _visible_or_404(db: Session, lab_id: str, user: User):
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="SPIRE lab not found")
    return row


def _shape(row) -> dict:
    return {
        "id": row.id, "name": row.name, "trust_domain": row.trust_domain,
        "cloud": row.cloud, "region": row.region or "",
        "vm_name": row.vm_name or "",
        # Both, because they answer different questions: the private address is what the
        # Resource Broker dials, the public one is what the Ansible runner used.
        "private_ip": row.private_ip or "", "public_ip": row.public_ip or "",
        "bind_port": row.bind_port,
        "status": row.status, "error_message": row.error_message,
        "source_cidrs": [c for c in (row.source_cidrs or "").split(",") if c],
        "firewall_name": row.firewall_name or "",
        "stages_done": [s for s in (row.stages_done or "").split(",") if s],
        "stages": [{"key": s["key"], "asset": s["asset"]}
                   for s in spire_lab_service.STAGES],
        "stage_job_ids": spire_lab_service.stage_jobs(row),
        "entries_seeded": row.entries_seeded,
        "discovery_expected": row.discovery_expected,
        "admin_spiffe_id": row.admin_spiffe_id or "",
        "admin_secret_folder": row.admin_secret_folder or "",
        "ps_safe": row.ps_safe or "",
        # The PEM itself is on /bundle, not here: a list of five labs would otherwise
        # carry five multi-kilobyte certificates nothing on the page renders.
        "has_bundle": bool(row.trust_bundle_pem),
        # The REAL notAfter SPIRE granted, which is shorter than what was asked for.
        # Once it lapses every plugin action fails PERMISSION_DENIED and reads exactly
        # like an admin_ids problem, so it is surfaced rather than left in a job log.
        "admin_svid_expires_at": (row.admin_svid_expires_at.isoformat()
                                  if row.admin_svid_expires_at else None),
        "ps_system_id": row.ps_system_id, "ps_account_id": row.ps_account_id,
        "deploy_job_id": row.deploy_job_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


class BuildRequest(BaseModel):
    name: str
    trust_domain: str
    cloud: str = "azure"
    # A VM NAME or IP. Re-derived server-side against this dashboard's own deploy rows —
    # a request that could name an arbitrary address would be a request to run four
    # privileged playbooks against a host of the caller's choosing.
    host: str
    admin_spiffe_id: str = ""


# ── read ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_labs(db: Session = Depends(get_db),
              user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    rows = spire_lab_service.list_labs(db)
    return [_shape(r) for r in rows if r.status != "deleted" and _visible(r, user)]


@router.get("/options")
async def build_options(db: Session = Depends(get_db),
                        user: User = Depends(require_permission("cloud_function", "read"))):
    """What the build form needs, plus an honest list of what is not configured yet.

    ``missing`` is the point of this route, as it is on the Certificate Lab: every item
    on it produces a failure that surfaces inside a playbook or inside the plugin hours
    later rather than at the click.

    ``hosts`` comes from the completed cloud-deploy rows — the same source of truth
    ``/api/config-mgmt/cloud-targets`` reads, rather than the cloud tabs' cache, which is
    empty on a fresh restart and after every deploy.
    """
    _require_enabled()
    from ..database import Job

    hosts: dict = {c: [] for c in spire_lab_service.PROVISIONING_CLOUDS}
    types = {spire_lab_service.host_backend(c).deploy_job_type: c
             for c in spire_lab_service.PROVISIONING_CLOUDS}
    jobs = (db.query(Job)
            .filter(Job.job_type.in_(tuple(types)), Job.status == "completed")
            .order_by(Job.created_at.desc()).all())
    seen = set()
    for job in jobs:
        meta = job.metadata_dict or {}
        if meta.get("destroyed"):
            continue
        cloud = types[job.job_type]
        name = meta.get("vm_name") or meta.get("instance_name") or ""
        ip = meta.get("public_ip") or meta.get("private_ip") or ""
        if not name or not ip or (cloud, name) in seen:
            continue
        seen.add((cloud, name))
        hosts[cloud].append({"name": name, "ip": ip,
                             "private_ip": meta.get("private_ip") or "",
                             "public_ip": meta.get("public_ip") or "",
                             "region": meta.get("location") or meta.get("region") or ""})

    cidrs = spire_lab_service.source_cidrs()
    missing = []
    if not cidrs:
        missing.append(
            "spire_lab_source_cidrs — no cloud ACL change will be made, so the "
            "Resource Broker can reach tcp/8081 only from inside the network. This is "
            "not a failure if your broker is in-subnet; it is the first thing to check "
            "if Verify Functional Account times out.")
    if not config_service.get_bool("password_safe_enabled", True):
        missing.append(
            "password_safe_enabled — the administrative credential is written into "
            "Secrets Safe by the playbook, using the dashboard's own pscli_* OAuth "
            "client. With BeyondTrust off there is nowhere for it to go.")
    elif not (config_service.get("pscli_api_url")
              and config_service.get("pscli_client_id")):
        missing.append(
            "pscli_api_url / pscli_client_id — the identity playbook's "
            "beyondtrust.secrets_safe module reads these from the runner's environment; "
            "unset means it cannot store the credential it just minted.")
    if not config_service.get_bool("ansible_enabled", True):
        missing.append("ansible_enabled — the lab IS four Ansible runs")

    # Which playbooks are actually staged. The dashboard runs assets by filename from the
    # storage backend and never from examples/ in the repo, so an un-uploaded playbook is
    # a mid-provision failure rather than a missing option.
    #
    # A failure to LIST is reported, not swallowed. Silence here would be the exact thing
    # this check exists to prevent: the run fetches each playbook by filename at the
    # moment it needs it, so an asset nobody staged is a job that dies three stages in.
    #
    # "no backend" is settled BEFORE the try so it keeps its own precise wording — it is
    # a configuration state, not a failure, and it is the common case on a fresh install.
    from ..services import storage_service
    backend_name = ((config_service.get("spire_lab_asset_backend") or "").strip()
                    or storage_service.active_backend() or "")
    if not backend_name:
        missing.append(
            "no storage backend is configured, so the four spire-*.yml playbooks have "
            "nowhere to live. A run fetches assets by filename from storage — set a "
            "backend on the Storage page, then upload them from "
            "examples/playbooks/spire/.")
    else:
        try:
            staged = [a.get("name") for a in
                      (await storage_service.list_assets_in(backend_name) or [])]
            absent = [a for a in spire_lab_service.STAGE_ASSETS if a not in staged]
            if absent:
                missing.append(
                    f"these playbooks are not on the {backend_name!r} storage backend: "
                    f"{', '.join(absent)} — upload them from examples/playbooks/spire/ "
                    f"on the Config Management page. A run fetches assets by filename "
                    f"from storage; the repo copy is a sample, not a source.")
        except Exception as exc:  # noqa: BLE001 — a backend that cannot list still reports
            # Log the real error server-side; return a generic reason. A storage-backend
            # error carries provider response bodies and bucket detail, and this endpoint
            # is reachable by any cloud_function reader — CodeQL py/stack-trace-exposure.
            # Same rule as api/config_mgmt's managed-account lookup.
            logger.warning("spire-lab: could not list assets on %r: %s",
                           backend_name, exc)
            missing.append(
                f"could not read the {backend_name!r} storage backend to check whether "
                f"the four spire-*.yml playbooks are staged — check the server logs. A "
                f"run fetches them by filename from storage, so verify on the Config "
                f"Management page before building.")

    return {"clouds": list(spire_lab_service.PROVISIONING_CLOUDS),
            "hosts": hosts,
            "bind_port": spire_lab_service.BIND_PORT,
            "source_cidrs": cidrs,
            "entries_seeded": spire_lab_service.ENTRIES_SEEDED,
            "discovery_expected": spire_lab_service.DISCOVERY_EXPECTED,
            "spire_version": config_service.get("spire_lab_version")
            or settings.spire_lab_version,
            "ca_ttl": config_service.get("spire_lab_ca_ttl") or settings.spire_lab_ca_ttl,
            "ps_safe": config_service.get("spire_lab_ps_safe") or settings.spire_lab_ps_safe,
            "asset_backend": backend_name,
            "missing": missing}


@router.get("/{lab_id}")
def get_lab(lab_id: str, db: Session = Depends(get_db),
            user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    return _shape(_visible_or_404(db, lab_id, user))


@router.get("/{lab_id}/bundle")
def get_bundle(lab_id: str, db: Session = Depends(get_db),
               user: User = Depends(require_permission("cloud_function", "read"))):
    """The trust bundle PEM — paste it into the managed system's ``SpiffeTrustBundlePem``.

    A SPIRE server presents only its leaf certificate, so there is nothing in the
    handshake for the plugin to chain against and fingerprint pinning cannot substitute:
    without this value every connect fails "SPIRE Server certificate could not be
    validated". It is also embedded in the PKCS#12 by ``-certfile``, so the credential
    carries its own copy — which is why the bundle needs no new Password Safe field.
    """
    _require_enabled()
    row = _visible_or_404(db, lab_id, user)
    if not row.trust_bundle_pem:
        raise HTTPException(
            status_code=409,
            detail=f"{row.name} has no trust bundle recorded yet — it is {row.status}")
    return {"id": row.id, "trust_domain": row.trust_domain,
            "trust_bundle_pem": row.trust_bundle_pem}


@router.get("/{lab_id}/onboarding")
def get_onboarding(lab_id: str, db: Session = Depends(get_db),
                   user: User = Depends(require_permission("cloud_function", "read"))):
    """Every value §5 of the standup runbook asks an operator to paste, resolved.

    Deliberately a read that returns STRINGS to copy, not an action that writes. Two of
    the four secret refs name the credential; this route names them and never reads them,
    because the functional account's DSS-key field is the protected place built for them.
    """
    _require_enabled()
    row = _visible_or_404(db, lab_id, user)
    refs = spire_lab_service.secret_refs(row)
    return {
        "id": row.id,
        "trust_domain": row.trust_domain,
        # The managed system's address and port. One trust domain = one managed system.
        "host": row.private_ip or row.public_ip or "",
        "port": row.bind_port,
        # The functional account's NAME is a SPIFFE ID, not a username. An account on the
        # wrong platform onboards green and then fails every action, because the managed
        # system inherits its platform from the account.
        "functional_account_name": row.admin_spiffe_id or "",
        "platform": "SPIFFE SVID",
        "safe": row.ps_safe or "",
        "dss_key_secret": refs.get("pfx", ""),
        "dss_passphrase_secret": refs.get("passphrase", ""),
        "trust_bundle_secret": refs.get("bundle", ""),
        "expiry_secret": refs.get("expires", ""),
        "has_bundle": bool(row.trust_bundle_pem),
        "admin_svid_expires_at": (row.admin_svid_expires_at.isoformat()
                                  if row.admin_svid_expires_at else None),
        "attribute": {"name": "SpiffeTrustDomain", "value": row.trust_domain},
        "discovery_expected": row.discovery_expected,
        "entries_seeded": row.entries_seeded,
        "runbook": "runbooks/spire-lab-standup",
    }


# ── write ─────────────────────────────────────────────────────────────────────

@router.post("")
def build_lab(req: BuildRequest, db: Session = Depends(get_db),
              user: User = Depends(require_permission("cloud_function", "write"))):
    _require_enabled()
    try:
        return spire_lab_service.provision(
            db, name=req.name, trust_domain=req.trust_domain, cloud=req.cloud,
            host=req.host, admin_spiffe_id=req.admin_spiffe_id,
            created_by=user.username)
    except SpireLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{lab_id}/acl")
async def reapply_acl(lab_id: str, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("cloud_function", "write"))):
    """Converge the cloud ACL on the CURRENT ``spire_lab_source_cidrs``.

    Inline rather than a job: it is one rule write, and the operator reaching for it is
    usually mid-diagnosis of a timeout — a queued job that reports back later is the
    wrong shape for that. Fail-closed still holds: an empty CIDR set CLOSES the port, so
    this doubles as the "shut it now" button.
    """
    _require_enabled()
    row = _visible_or_404(db, lab_id, user)
    import json
    from datetime import datetime
    # Resolved OUTSIDE the try, so the generic handler below can name the ACL without
    # risking an unbound local: `require_backend` raises only SpireLabError, which is
    # a 400 either way.
    try:
        backend = spire_lab_service.require_backend(row.cloud)
    except SpireLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        cidrs = spire_lab_service.source_cidrs()
        res = await backend.apply_ingress(
            json.loads(row.vm_resource_id or "{}"), [row.bind_port], cidrs)
        row.source_cidrs = ",".join(cidrs)
        row.firewall_name = res.get("name") or row.firewall_name
        row.updated_at = datetime.utcnow()
        db.commit()
        return {"id": row.id, "opened": bool(res.get("opened")),
                "source_cidrs": cidrs, "firewall_name": row.firewall_name,
                "acl": backend.acl_label,
                # The HOST firewall is a second gate and this did not touch it. The two
                # fail identically, so saying so is the difference between one more check
                # and an afternoon.
                "note": ("The host firewall is a separate gate. Re-run "
                         "spire-open-ports.yml from Config Management if you changed the "
                         "source set and the host runs firewalld or ufw.")}
    except SpireLabError as exc:
        # Our own message, authored here — safe to return verbatim.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        # Log the real error server-side; return a generic reason. A cloud SDK error
        # carries request ids, subscription/project identifiers and response bodies, and
        # this endpoint is reachable by any cloud_function writer — CodeQL
        # py/stack-trace-exposure. The job path keeps the detail: a provision or teardown
        # records the provider's own text on the row, which is where to look.
        logger.warning("spire-lab: ACL re-apply failed for %s: %s", row.id, exc)
        raise HTTPException(
            status_code=502,
            detail=(f"the {backend.acl_label} could not be updated — check the server "
                    f"logs. Reachability is two gates; the host firewall is the other."))


@router.delete("/{lab_id}")
def destroy_lab(lab_id: str, db: Session = Depends(get_db),
                user: User = Depends(require_permission("cloud_function", "write"))):
    """Close tcp/8081 — the teardown. The same path the auto-delete timer runs, so there
    is exactly one and it is exercised both ways.

    **The VM is left alone.** It is an ordinary VM with its own timer and its own
    Destroy, and destroying somebody's host because a lab expired is a bigger surprise
    than leaving behind a server nothing can reach.
    """
    _require_enabled()
    row = _visible_or_404(db, lab_id, user)
    try:
        return spire_lab_service.start_decommission(
            db, lab_id=row.id, created_by=user.username)
    except SpireLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
