"""
Certificate Lab API — preview (gated by the ``cert_lab_enabled`` flag).

  GET    /api/cert-lab                    — list dashboard-built certificate authorities
  POST   /api/cert-lab                    — build a CA (record + schedule apply)
  GET    /api/cert-lab/options            — clouds, locations, tiers and what is missing
  GET    /api/cert-lab/{id}               — one CA
  GET    /api/cert-lab/{id}/chain         — the CA chain PEM, for the mTLS endpoint
  POST   /api/cert-lab/{id}/identities    — onboard a certificate identity onto it
  DELETE /api/cert-lab/{id}/identities    — remove the Password Safe objects
  DELETE /api/cert-lab/{id}               — destroy the CA (terraform destroy)
  POST   /api/cert-lab/preview-address    — compose + validate a profile without saving

The plugin ships as TWO .psplugin packages over a shared core, appearing in BeyondInsight
as separate platforms with separate access control: "Certificate" for an end-entity
certificate and "Subordinate CA" for an issuing authority. So an identity carries a
``package``, and a CA carries the ``ca_path_length`` that decides whether it can serve the
second one at all — a root created to issue leaves has a path length of zero and refuses
to sign a subordinate, and that is fixed when the root is created.

``preview-address`` exists because the managed system's address is the ENTIRE
configuration surface for this plugin — a Password Safe Cloud tenant cannot edit the
appsettings.json inside the .psplugin — and Password Safe's address column is 255
characters. The fully spelled-out ADCS profile from the plugin's own documentation is 269
characters, so an operator needs to see the length while the field is still editable
rather than after a rotation fails.

Permission-gated via the ``cloud_function``-style pattern: list results are scoped to the
caller's own rows for non-admins, mirroring the cloud-database and functions pages.

The chain PEM is deliberately NOT admin-only. A CA certificate is a public document by
construction — it is what every client has to trust — and the whole lab depends on
pasting it into an nginx ``ssl_client_certificate``.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import cert_lab_service, cert_ps_service, config_service
from ..services.cert_lab_service import CertLabError
from ..services.cert_ps_service import CertPSError
from .auth import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cert-lab", tags=["cert-lab"])


def _require_enabled() -> None:
    if not config_service.get_bool("cert_lab_enabled", settings.cert_lab_enabled):
        raise HTTPException(status_code=403, detail="the Certificate Lab is disabled")


def _row_or_404(db: Session, lab_id: str):
    row = cert_lab_service.get_lab(db, lab_id)
    if not row:
        raise HTTPException(status_code=404, detail="certificate authority not found")
    return row


def _visible(row, user: User) -> bool:
    """Creator-scoped for non-admins, exactly like the databases and functions pages."""
    return bool(getattr(user, "is_admin", False)) or row.created_by == user.username


def _shape(row) -> dict:
    return {"id": row.id, "name": row.name, "cloud": row.cloud, "backend": row.backend,
            "project": row.project, "location": row.location, "pool_id": row.pool_id,
            "status": row.status, "error_message": row.error_message,
            # What the root permits beneath it, and therefore whether this CA can serve
            # the "Subordinate CA" platform at all. Fixed when the root was created, so
            # the page uses it to hide a button rather than to offer one that fails.
            "ca_path_length": cert_lab_service.ca_path_length(row),
            "can_sign_subordinate": cert_lab_service.can_sign_subordinate(row),
            # The enrollment identity's EMAIL. Its KEY is never returned by any route
            # here: it goes straight into the Password Safe functional account, which is
            # the protected field built for it.
            "enroll_account": row.enroll_account,
            # The functional account's NAME (never its credential), and whether this
            # dashboard minted it — which is what decides if teardown may delete it.
            #
            # One per PACKAGE, because a functional account is platform-bound and a
            # managed system inherits its platform: the account on "Certificate" cannot
            # carry a managed system on "Subordinate CA". The subordinate one is NULL
            # until the CA first issues on that package, which is not a fault — it is
            # minted lazily, so a CA that never issues an authority carries no account on
            # the platform that would.
            "functional_account": row.ps_functional_account,
            "functional_account_owned": bool(row.ps_functional_account_id),
            "subca_functional_account": row.ps_subca_functional_account,
            "subca_functional_account_owned": bool(row.ps_subca_functional_account_id),
            "has_chain": bool(row.ca_chain_pem),
            "ps_system_id": row.ps_system_id, "ps_account_id": row.ps_account_id,
            "ps_address": row.ps_address,
            # Which platform the registered managed system is on. NULL on a row that
            # predates the plugin split, which the readers treat as the leaf package.
            "ps_package": row.ps_package or (
                cert_lab_service.LEAF_PACKAGE if row.ps_system_id else None),
            "deploy_job_id": row.deploy_job_id,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None}


class BuildRequest(BaseModel):
    name: str
    project: str
    cloud: str = "gcp"
    location: str = ""
    pool_id: str = ""
    # How many CAs the root permits beneath it. 0 (the default, and what every CA built
    # before this field existed is) signs end-entity certificates only; 1 lets the
    # "Subordinate CA" platform obtain a subordinate from it. A one-way decision — it is
    # fixed when the root is created — which is why it is asked here rather than settable.
    path_length: int = cert_lab_service.CA_PATH_LENGTH_LEAF_ONLY


class IdentityRequest(BaseModel):
    account_name: str
    # Per-identity profile overrides — lifetime, key, subject, dns, eku on the leaf
    # package; lifetime, pathlen and the permit*/exclude* name constraints on the
    # subordinate one. Each overrides the CA's default for this identity alone, which is
    # what removes the need for a separate platform instance per SAN set.
    overrides: Optional[dict] = None
    # Which of the plugin's two packages — "certificate" for an end-entity certificate,
    # "subca" for an issuing authority. Not a flag on the profile: they are separate
    # .psplugin packages appearing as separate PLATFORMS with separate access control, so
    # this decides which platform the managed system lands on and which of the CA's two
    # functional accounts carries it.
    package: str = cert_lab_service.LEAF_PACKAGE


class AddressPreviewRequest(BaseModel):
    backend: str
    backend_options: dict = {}
    overrides: Optional[dict] = None
    package: str = cert_lab_service.LEAF_PACKAGE


class WireUpRequest(BaseModel):
    """Which package's functional account to (re)create. The leaf one is minted by the
    build; the subordinate one lazily on first use, so retrying it by hand is how an
    operator gets one before onboarding anything."""
    package: str = cert_lab_service.LEAF_PACKAGE


# ── read ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_authorities(db: Session = Depends(get_db),
                     user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    rows = cert_lab_service.list_labs(db)
    return [_shape(r) for r in rows if r.status != "deleted" and _visible(r, user)]


@router.get("/options")
def build_options(user: User = Depends(require_permission("cloud_function", "read"))):
    """What the build form needs, plus an honest list of what is not configured yet.

    ``missing`` is the point of this route. Every item on it produces a failure that
    surfaces hours later inside the plugin rather than at the click, so naming them up
    front is the difference between a five-minute setup and an afternoon."""
    _require_enabled()
    missing = []
    if not cert_ps_service.default_biurl():
        missing.append("cert_ps_biurl (or pscli_api_url) — the plugin has nowhere to "
                       "write the bundle, and a Cloud tenant cannot supply it from "
                       "appsettings.json")
    if not config_service.get("cert_ps_owner_group_id"):
        missing.append("cert_ps_owner_group_id — Secrets Safe requires an owner for "
                       "created secrets, and it is a GROUP id here")
    # What is missing depends on who makes the functional account. In create mode the
    # dashboard holds the CA half already — it comes out of the build — so the only
    # thing it cannot produce is the BeyondInsight half.
    if cert_ps_service.functional_account_mode() == "reference":
        if not config_service.get("cert_ps_functional_account"):
            missing.append("cert_ps_functional_account — one account carries BOTH the "
                           "CA enrollment credential and the BeyondInsight API user, "
                           "split on the last colon")
    else:
        # There are two shapes of the BeyondInsight half and the resolver picks one, so
        # naming a single missing key here would name the wrong one: an install with an
        # OAuth registration and no API key is completely configured. Ask the resolver,
        # and report what it refuses — its message already names every key the credential
        # could have come from.
        try:
            cert_ps_service.resolve_bi_credential()
        except cert_ps_service.CertPSError as exc:
            missing.append(str(exc))
    # A stamped timer is necessary and not sufficient: the reaper only DELETES when
    # `resource_expiry_enforce` is on and dry-run is off. `cert_lab_service.provision`
    # refuses an AWS build that would get no timer at all; this is the other half, and it
    # belongs on `missing` rather than in a refusal because arming enforcement is a thing
    # an operator may be part-way through, not a reason to have no timer.
    from ..services import expiry_policy
    if not expiry_policy.enforce() or expiry_policy.dry_run():
        missing.append("resource_expiry_enforce, with resource_expiry_dry_run off — a "
                       "timer is stamped but the reaper only reports, so an AWS Private "
                       "CA at ~$400/month standing would keep billing until somebody "
                       "destroys it by hand")

    # The plugin split its subordinate-CA half into a second .psplugin, so there are two
    # platforms to install and either may be absent from a tenant. Naming both here lets
    # the page label the choice with what the operator will actually look for in
    # BeyondInsight, rather than with the dashboard's own word for it.
    packages = [
        {"value": cert_lab_service.LEAF_PACKAGE,
         "platform": cert_ps_service.platform_name(cert_lab_service.LEAF_PACKAGE),
         "label": "Certificate — an end-entity certificate",
         "requires_path_length": cert_lab_service.CA_PATH_LENGTH_LEAF_ONLY},
        {"value": cert_lab_service.SUBCA_PACKAGE,
         "platform": cert_ps_service.platform_name(cert_lab_service.SUBCA_PACKAGE),
         "label": "Subordinate CA — an issuing authority",
         "requires_path_length": cert_lab_service.CA_PATH_LENGTH_SUBCA_CAPABLE},
    ]

    return {"clouds": list(cert_lab_service.PROVISIONING_CLOUDS),
            "locations": ["us-central1", "us-east1", "europe-west1", "asia-east1"],
            "tiers": ["DEVOPS", "ENTERPRISE"],
            "packages": packages,
            # What a root may permit beneath it. Offered on the build form because it
            # cannot be changed once the root exists — a CA built leaf-only can never
            # serve the Subordinate CA platform, whatever is configured later.
            "path_lengths": [
                {"value": cert_lab_service.CA_PATH_LENGTH_LEAF_ONLY,
                 "label": "End-entity certificates only",
                 "detail": "The root signs leaves directly. Cannot sign a subordinate CA."},
                {"value": cert_lab_service.CA_PATH_LENGTH_SUBCA_CAPABLE,
                 "label": "Can also sign a subordinate CA",
                 "detail": "Needed for the Subordinate CA platform. Still issues leaves."},
            ],
            "default_path_length": cert_lab_service.CA_PATH_LENGTH_LEAF_ONLY,
            "default_location": config_service.get("cert_gcp_cas_location") or "us-central1",
            # The project the dashboard's own GCP credential is scoped to, so the build
            # form can prefill it. `cert_lab_service.provision` falls back to the same two
            # keys server-side, so a blank field still works — this only makes the value
            # visible and editable before the click.
            "default_project": (config_service.get("gcp_project")
                                or config_service.get("gcp_project_id")
                                or settings.gcp_project_id or ""),
            # AWS PCA is available in most regions, so this is a default for a free-text
            # field rather than the fixed list CAS's limited locations justify.
            "default_region": config_service.get("aws_region") or settings.aws_region,
            "biurl": cert_ps_service.default_biurl(),
            "folder": config_service.get("cert_ps_folder") or settings.cert_ps_folder,
            "address_limit": 255,
            "missing": missing}


@router.get("/{lab_id}")
def get_authority(lab_id: str, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    return _shape(row)


@router.get("/{lab_id}/chain")
def get_chain(lab_id: str, db: Session = Depends(get_db),
              user: User = Depends(require_permission("cloud_function", "read"))):
    """The CA chain PEM — feed it to the mTLS endpoint playbook as ``ca_chain_pem``."""
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    if not row.ca_chain_pem:
        raise HTTPException(
            status_code=409,
            detail=f"{row.name} has no chain yet — it is {row.status}")
    return {"id": row.id, "name": row.name, "ca_chain_pem": row.ca_chain_pem}


# ── write ─────────────────────────────────────────────────────────────────────

@router.post("")
def build_authority(req: BuildRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    _require_enabled()
    try:
        return cert_lab_service.provision(
            db, name=req.name, project=req.project, cloud=req.cloud,
            location=req.location, pool_id=req.pool_id,
            path_length=req.path_length, created_by=user.username)
    except CertLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/preview-address")
def preview_address(req: AddressPreviewRequest,
                    user: User = Depends(require_permission("cloud_function", "read"))):
    """Compose and validate a profile without saving anything. Never 400s on an invalid
    profile — it returns the address it would have built plus the reason it is refused,
    so the field can show both while it is still being edited."""
    _require_enabled()
    return cert_ps_service.address_preview(req.backend, req.backend_options,
                                           req.overrides, req.package)


@router.post("/{lab_id}/identities")
def add_identity(lab_id: str, req: IdentityRequest, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("cloud_function", "write"))):
    """Onboard one identity onto this CA, on either of the plugin's two packages.

    One managed account per identity — and with an Entra publisher, one per app
    registration. Graph's PATCH replaces the whole keyCredentials collection, so two
    rotations against the same registration can clobber each other's key; Password Safe
    serialises per managed account, which is what makes that mapping safe.

    ``package="subca"`` onboards onto the "Subordinate CA" platform instead, where the
    managed credential is an ISSUER rather than a leaf. That is refused here when this
    CA's root cannot sign a subordinate — a decision fixed when the root was created."""
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    try:
        return cert_lab_service.start_ps_register(
            db, lab_id=lab_id, account_name=req.account_name,
            created_by=user.username, overrides=req.overrides, package=req.package)
    except (CertLabError, CertPSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{lab_id}/functional-account")
async def wire_up_functional_account(
        lab_id: str, req: Optional[WireUpRequest] = None, db: Session = Depends(get_db),
        user: User = Depends(require_permission("cloud_function", "write"))):
    """Create or retry one of this CA's two functional accounts.

    Recovers the enrollment credential from the CA's own terraform state, so this needs
    no rebuild — which matters, because CAS never hands a deleted pool id back and a
    rebuild is therefore not a free retry.

    Two reasons an account can be missing, and this serves both: the build could not
    create the leaf one, or the CA has never issued on the subordinate package, whose
    account is minted lazily. The body is optional so a caller that predates the split
    still asks for the leaf one.

    Synchronous rather than a job: it is one Password Safe call plus a state read, and
    the operator who just fixed the setting is watching."""
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    package = req.package if req else cert_lab_service.LEAF_PACKAGE
    try:
        return await cert_lab_service.rewire_functional_account(
            db, lab_id=lab_id, package=package)
    except (CertLabError, CertPSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:                                        # noqa: BLE001
        # A Password Safe API failure is the operator's to fix (a wrong API key, a
        # platform that is not there), so it belongs in the response rather than as a
        # 500 with the detail only in the log.
        logger.error("cert-lab: wire-up failed for %s: %s", lab_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{lab_id}/identities")
def remove_identity(lab_id: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    try:
        return cert_lab_service.start_ps_register(
            db, lab_id=lab_id, account_name="", created_by=user.username,
            action="deregister")
    except CertLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{lab_id}")
def destroy_authority(lab_id: str, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("cloud_function", "write"))):
    """Destroy the CA pool and everything in it. The same teardown the auto-delete timer
    runs, so there is exactly one path and it is exercised both ways."""
    _require_enabled()
    row = _row_or_404(db, lab_id)
    if not _visible(row, user):
        raise HTTPException(status_code=404, detail="certificate authority not found")
    try:
        return cert_lab_service.start_decommission(
            db, lab_id=lab_id, created_by=user.username)
    except CertLabError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
