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
# ONE definition of the managed-account ref, imported rather than re-declared: its
# "pinned ids or a name" validator is the invariant, and a second copy would let the two
# drift — a drift that presents as a run checking out the wrong host's credential.
# api/cloud_databases.py already imports a helper from this module, so the direction is
# established, and api/config_mgmt imports nothing from here, so there is no cycle.
from .config_mgmt import (ManagedAccountRef, _can_use_secrets,
                          _validate_cloud_secret_stores)

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
        # WHICH credential this lab was built with — "managed" / "ssh-key-secret" /
        # "auto". The kind and the account NAME only: a name is not a credential (it is
        # also what becomes ansible_user), and the ref itself has no business on a page.
        "credential_kind": spire_lab_service.credential_kind(row),
        "credential_account": spire_lab_service.managed_account_name(row),
        "credential_login_user": row.login_user or "",
        # ── the Kubernetes half ──────────────────────────────────────────────
        # `k8s_status` NULL is "never attempted", which is every lab built before the
        # feature. Kept SEPARATE from `status` so a failed link never makes an otherwise
        # working trust domain read as broken.
        "k8s_status": row.k8s_status or "",
        "k8s_error_message": row.k8s_error_message,
        "k8s_vm_name": row.k8s_vm_name or "",
        "k8s_private_ip": row.k8s_private_ip or "",
        "k8s_stages_done": [x for x in (row.k8s_stages_done or "").split(",") if x],
        "k8s_stages": [{"key": x["key"], "asset": x["asset"], "host": x.get("host", "spire")}
                       for x in spire_lab_service.K8S_STAGES],
        "k8s_stage_job_ids": spire_lab_service.k8s_stage_jobs(row),
        # The three strings that have to agree in three places. Shown because when they
        # disagree Kubernetes rejects every token and says nothing useful about why.
        "k8s_audience": row.k8s_audience or "",
        "k8s_issuer_url": row.k8s_issuer_url or "",
        "k8s_workload_spiffe_id": row.k8s_workload_spiffe_id or "",
        "k8s_workload_role": row.k8s_workload_role or "",
        # What the RBAC subject actually is. Not the SPIFFE ID: Kubernetes requires a
        # prefix on any username claim other than email, so the binding names
        # "spiffe:spiffe://…". It looks wrong, it is correct, and it is the first thing
        # to check on a 403 — so the page shows the real string rather than implying it.
        "k8s_rbac_subject": (
            f"{spire_lab_service.K8S_USERNAME_PREFIX}{row.k8s_workload_spiffe_id}"
            if row.k8s_workload_spiffe_id else ""),
        # The k3s node's own, separate from credential_kind above: two VMs, two keys.
        # The KIND and the account NAME only — a name is not a credential.
        "k8s_credential_kind": spire_lab_service.credential_kind_for(row, "k8s"),
        "k8s_credential_account": (
            (spire_lab_service.managed_ref_for(row, "k8s") or {}).get("account_name") or ""),
        "k8s_credential_login_user": row.k8s_login_user or "",
        "k8s_workload_user": spire_lab_service.K8S_WORKLOAD_USER,
        "k8s_jwt_svid_ttl": spire_lab_service.K8S_JWT_SVID_TTL,
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
    # WHO the four playbook runs log in as. Field names match Config Management's
    # RunRequest on purpose: the two forms post the same shape, and the SPIRE page reuses
    # that page's own /secret-options and /managed-accounts endpoints to populate them.
    # Either/or — the service refuses both — and blank means "auto-derive this host's
    # keypair from its deploy job", which is what every lab built before this did.
    secret_ssh_key_source: str = ""
    managed_account: ManagedAccountRef | None = None
    # All four plays are `become: true`, so a non-root account needs a sudo password;
    # this sends the same account as managed_become rather than adding a second picker.
    managed_become_self: bool = False
    login_user: str = ""


class K8sLinkRequest(BaseModel):
    # A VM NAME or IP, re-derived server-side against this dashboard's own deploy rows —
    # same contract as BuildRequest.host, and the same reason.
    host: str
    # Blank takes the default. The audience is the security boundary rather than a label:
    # a relying party that accepts an audience it was not issued for accepts tokens minted
    # for somebody else's service.
    audience: str = ""
    workload_role: str = ""
    # THE K3S NODE'S OWN connection identity. The two VMs are deployed independently and do
    # not share an SSH key, so these are a separate set from the ones BuildRequest took for
    # the SPIRE host — and a blank set here means "auto-derive from THIS host's deploy job",
    # never "reuse the SPIRE host's". Same either/or rule and the same field names as the
    # build form, so the panel reuses Config Management's own pickers unchanged.
    secret_ssh_key_source: str = ""
    managed_account: ManagedAccountRef | None = None
    managed_become_self: bool = False
    login_user: str = ""


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
            "nowhere to live. A run fetches assets by filename from storage — set an "
            "active backend on the Storage page and upload them there, from "
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
                    f"on the Storage page. A run fetches assets by filename from storage; "
                    f"the repo copy is a sample, not a source.")
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
                f"run fetches them by filename from storage, so verify on the Storage "
                f"page before building.")

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
    # The same pre-flight refusals a Config-Management run gets, worded once in
    # services/ansible_run_gate. Here rather than in the service because two of them are
    # about the CALLER — a 403 is not a SpireLabError — and because the runner decision
    # is per-lab-cloud: an Azure lab dispatches to ACI, which injects a managed
    # credential inline, while an AWS or GCP lab dispatches to ECS / Cloud Run, where a
    # just-in-time credential needs the ephemeral-store opt-in.
    from ..services import (ansible_local_service, ansible_run_gate,
                            managed_accounts as _ma)
    from ..services import config_service as cs

    cloud = (req.cloud or "azure").lower()
    has_managed = req.managed_account is not None
    wants_secret = bool(has_managed or req.secret_ssh_key_source)
    # Resolved with ansible_local_service._cfg — the SAME reader _run_job uses — so this
    # predicts the runner the run will actually dispatch to.
    eff_runner = ansible_run_gate.effective_runner(cloud, cfg=ansible_local_service._cfg)
    refusal = ansible_run_gate.check_permission(
        wants_secret=wants_secret,
        can_use_secrets=_can_use_secrets(user),
        has_managed=has_managed,
        # Short-circuited: a build with no managed account costs no config read here.
        password_safe_enabled=has_managed and cs.get_bool("password_safe_enabled"))
    if refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail)
    # A no-op by construction today: this form sends no named-var and no become SECRET,
    # and secret_ssh_key_source is deliberately excluded from the store-residency check.
    # Kept because it is the line that stops being a no-op the day a become source is
    # added here, and its absence would be the silent gap.
    if wants_secret and eff_runner in ("ecs", "aci", "gcp"):
        _validate_cloud_secret_stores(eff_runner, None, "")
    # A lab is always a .yml playbook against a bare IP, so the last two are True by
    # construction — spelled out so the call reads the same as /run's.
    needs_ephemeral = _ma.requires_ephemeral_store(has_managed, eff_runner, True, True)
    refusal = ansible_run_gate.check_runner_capability(
        needs_ephemeral_store=needs_ephemeral,
        ephemeral_enabled=(needs_ephemeral
                           and cs.get_bool("ansible_cloud_ephemeral_secrets_enabled")),
        runner=eff_runner,
        gcp_runner_service_account=(
            ansible_local_service._cfg("gcp_ansible_runner_service_account")
            if needs_ephemeral else ""))
    if refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail)
    try:
        return spire_lab_service.provision(
            db, name=req.name, trust_domain=req.trust_domain, cloud=cloud,
            host=req.host, admin_spiffe_id=req.admin_spiffe_id,
            created_by=user.username,
            secret_ssh_key_source=req.secret_ssh_key_source,
            managed_account=(req.managed_account.model_dump()
                             if req.managed_account else None),
            managed_become_self=req.managed_become_self,
            login_user=req.login_user)
    except SpireLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{lab_id}/k8s-link")
def link_kubernetes(lab_id: str, req: K8sLinkRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    """Attest a k3s node into this trust domain and make its API server accept JWT-SVIDs.

    Deliberately a SEPARATE action on an existing lab rather than a stage of the build:
    the governance half is what most labs are built for and stands on its own, so a k3s
    failure must not make a working trust domain read as broken. An existing lab can also
    gain the capability without being rebuilt.

    The k3s node brings its OWN connection identity, because the two VMs are deployed
    independently and do not share an SSH key. Leaving it blank is the normal case and
    already works: the runner derives a keypair from the deploy job of the host it is
    connecting to, and these stages target the k3s node. What it must never do is inherit
    the SPIRE host's chosen account or key secret — that would connect to one VM with
    another VM's credential.
    """
    _require_enabled()
    _visible_or_404(db, lab_id, user)
    try:
        return spire_lab_service.start_k8s_link(
            db, lab_id=lab_id, created_by=user.username, host=req.host,
            audience=req.audience, workload_role=req.workload_role,
            secret_ssh_key_source=req.secret_ssh_key_source,
            managed_account=(req.managed_account.model_dump()
                             if req.managed_account else None),
            managed_become_self=req.managed_become_self,
            login_user=req.login_user)
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
