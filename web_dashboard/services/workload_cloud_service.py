"""Short-lived AWS and Azure credentials for a workload, minted by Workload Credentials.

The Workload Lab's fourth answer, and the one aimed at the most common non-human credential
there is. Ask any customer how their build server reaches AWS and the answer is an access
key in a CI secret store: no expiry, no revocation, and no record of who read it. The other
three tabs replace a certificate, a SPIFFE identity and a Kubernetes token; this one
replaces that key.

**THE SCOPE IS NOT DEFINED HERE.** This is the honest difference from the sibling tabs and
it is worth stating before anything else. The Kubernetes tab chooses a RoleBinding and the
Certificate tab chooses a profile, so the dashboard decides what the identity may do. Here
a **dynamic secret** defined in Workload Credentials decides — which role is assumed, which
subscription, which permissions — and this module names the secret it draws from without
being able to widen or narrow it. A tab that implied otherwise would be claiming a control
it does not have.

What this module DOES own is the lease lifecycle, which is where the demonstration lives:

  * **issue** — mint against the dynamic secret. Returns the credential to the caller that
    asked and records only the lease id, the provider's expiry and a count.
  * **inspect** — read the lease back from Workload Credentials, so the page shows the
    provider's own view rather than this row's cached opinion of it.
  * **revoke** — release the lease early, WHERE THE PROVIDER ALLOWS IT (see below).
  * **decommission** — revoke what can be revoked and retire the identity, which is what
    stops it minting again.

**REVOCATION IS ASYMMETRIC, AND PRETENDING OTHERWISE WOULD BE THE WORST THING THIS MODULE
COULD DO.** Azure leases can be released early. AWS cannot: STS refuses with
``lease_not_revocable``, because a credential it has already signed cannot be withdrawn
before it expires. ``workload_credentials_service.revoke_lease`` swallows that refusal by
design, so a caller that reported "revoked" on its return value would tell an operator an
AWS credential was dead while it kept working for up to an hour. So this module checks the
cloud first, reports `revocable` honestly, and says what actually happened.

The consequence is worth stating plainly rather than hiding: **on AWS the TTL is the only
control there is**, which makes a short one matter more there, not less.

**GENERATE IS METERED.** Workload Credentials bills per issuance. Onboarding therefore mints
nothing — it records an identity and stops — and every mint is an explicit action with a
count behind it. A number climbing on an identity nobody is using is the signature of a
misconfigured consumer retrying, which is a cost problem before it is an audit one.

**NOT the dashboard's own lease.** ``workload_credential_lease`` is a singleton per
``(cloud, purpose)`` holding the credential THIS APPLICATION uses for its own cloud calls,
configured by ``wlc_{cloud}_secret_name``, wrapped in a memo, a lock and a billing backoff.
Nothing here touches it. Minting into that store would overwrite a credential the dashboard
may be mid-deployment with — the module's own docstring warns that a cleared one is
indistinguishable from a deployment that was never on the dynamic tier — and would bill for
doing so. A test pins that this module never imports it.

See docs/workload-cloud.md.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..database import WorkloadCloudCredential

logger = logging.getLogger(__name__)

JOB_TYPE = "workload_cloud_credential"
INVENTORY_KIND = "workloadcloud"

VALID_ACTIONS = ("issue", "revoke", "retire")
VALID_CLOUDS = ("aws", "azure")

# Which clouds can release a lease before it expires.
#
# AWS is absent and that is a provider fact, not a gap here: STS will not withdraw a
# credential it has already signed, so `revoke_lease` gets `400 lease_not_revocable`. The
# client swallows that refusal (callers "revoke unconditionally and let the provider
# decide"), which is right for the dashboard's own housekeeping and wrong for a page that
# has to tell an operator whether access actually stopped. Hence this table, and hence
# `revoke` below reporting what the provider allows rather than what the call returned.
_REVOCABLE_CLOUDS = frozenset({"azure"})

# What a mint hands back per cloud, as field NAMES. Used to tell an operator what their
# consumer will receive without this module ever holding the values — `parse_generated`
# returns them and they go straight to the caller.
_CREDENTIAL_SHAPE = {
    "aws": ("access_key_id", "secret_access_key", "session_token"),
    "azure": ("client_id", "client_secret", "tenant_id"),
}


class WorkloadCloudError(Exception):
    pass


def _cfg(key: str, default: str = "") -> str:
    from . import config_service
    try:
        return config_service.get(key) or default
    except Exception:                                  # pragma: no cover — config layer
        return default


def _cfg_bool(key: str, default: bool = False) -> bool:
    """A BOOLEAN config read, and it exists because the obvious shortcut is wrong.

    `bool(_cfg("wlc_azure_enabled"))` is True for the string "false" — config values are
    stored as text, so every disabled flag reads as enabled. That let an identity be
    registered against a cloud with no Workload Credentials configuration at all, which then
    fails at the first mint with a message about the site rather than about the cloud.
    Found by resolving a real row, which is what that step is for.
    """
    from . import config_service
    try:
        return config_service.get_bool(key, default)
    except Exception:                                  # pragma: no cover — config layer
        return default


def revocable(cloud: str) -> bool:
    """Whether this cloud's leases can be released early. See `_REVOCABLE_CLOUDS`."""
    return (cloud or "").strip().lower() in _REVOCABLE_CLOUDS


def credential_fields(cloud: str) -> tuple:
    """The field NAMES a mint returns for this cloud. Never the values."""
    return _CREDENTIAL_SHAPE.get((cloud or "").strip().lower(), ())


def enabled() -> bool:
    """Workload Credentials on and configured. No preview flag of this tab's own.

    Settings owns two toggles for this page, one per lab, and "do not change the settings
    menu" still holds — so this tab rides `workload_credentials_enabled`, which already
    exists for the integration itself, plus the site id and token that integration needs.
    """
    from . import workload_credentials_service as wlc
    return wlc.configured()


def cloud_enabled(cloud: str) -> bool:
    """Whether this cloud has a dynamic secret configured for the LAB.

    Deliberately NOT `workload_credential_lease.dynamic_enabled`, which asks a different
    question: whether the DASHBOARD should take its own credentials from Workload
    Credentials for that cloud. An operator may well want the lab without rerouting the
    application's own cloud access, and conflating the two would make enabling a demo
    change how the dashboard authenticates.
    """
    # `_cfg_bool`, never `bool(_cfg(...))`: see its docstring — "false" is a truthy string.
    return _cfg_bool(f"wlc_{(cloud or '').strip().lower()}_enabled")


# ── reads ─────────────────────────────────────────────────────────────────────

def list_rows(db: Session, workgroup: Optional[str] = None) -> list:
    q = db.query(WorkloadCloudCredential).filter(
        WorkloadCloudCredential.status != "deleted")
    if workgroup:
        q = q.filter(WorkloadCloudCredential.workgroup == workgroup)
    return q.order_by(WorkloadCloudCredential.created_at.desc()).all()


def get_row(db: Session, row_id: str) -> Optional[WorkloadCloudCredential]:
    return (db.query(WorkloadCloudCredential)
            .filter(WorkloadCloudCredential.id == row_id).first())


def job_ids(row: WorkloadCloudCredential) -> list:
    try:
        out = json.loads(row.job_ids or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def lease_state(row: WorkloadCloudCredential,
                now: Optional[datetime] = None) -> str:
    """``none`` | ``live`` | ``expired``, from the row alone.

    A separate notion from `status`, because an EXPIRED LEASE IS THE MECHANISM WORKING. The
    row stays healthy; only the credential is gone. Collapsing the two would make a
    correctly-behaving identity read as broken every time its credential aged out, which is
    most of the time.
    """
    if not row.lease_id or not row.lease_expires_at:
        return "none"
    now = now or datetime.utcnow()
    return "live" if row.lease_expires_at > now else "expired"


# ── register ──────────────────────────────────────────────────────────────────

def register(db: Session, *, name: str, cloud: str, dynamic_name: str,
             created_by: str, dynamic_folder: str = "", purpose: str = "",
             ttl_seconds: int = 0, workgroup: Optional[str] = None,
             expires_at: Optional[datetime] = None) -> dict:
    """Record a workload identity against a dynamic secret. **Mints nothing.**

    Issuance is billed, so registering is deliberately inert: it says "this workload draws
    from that secret" and stops. The first credential appears when somebody presses Issue,
    or when a consumer asks for one.
    """
    if not enabled():
        raise WorkloadCloudError(
            "Workload Credentials is not configured — set workload_credentials_enabled, "
            "wlc_site_id and wlc_pat")
    name = (name or "").strip()
    if not name:
        raise WorkloadCloudError("a name is required")
    cloud = (cloud or "").strip().lower()
    if cloud not in VALID_CLOUDS:
        raise WorkloadCloudError(
            f"unknown cloud {cloud!r} (expected one of {', '.join(VALID_CLOUDS)})")
    if not cloud_enabled(cloud):
        raise WorkloadCloudError(
            f"{cloud} has no Workload Credentials configuration — set wlc_{cloud}_enabled "
            f"and its dynamic-secret settings first")
    dynamic_name = (dynamic_name or "").strip()
    if not dynamic_name:
        raise WorkloadCloudError(
            "a dynamic-secret name is required — it is what decides the scope of every "
            "credential this identity receives, and this dashboard cannot supply a default "
            "for it")
    dynamic_folder = (dynamic_folder or "").strip()

    # One identity per (cloud, folder, secret, purpose). A second row on the same four would
    # mint from the same dynamic secret under two names — two billed streams of issuance for
    # one workload, and two rows an operator has to reconcile when revoking.
    dup = (db.query(WorkloadCloudCredential)
           .filter(WorkloadCloudCredential.cloud == cloud,
                   WorkloadCloudCredential.dynamic_name == dynamic_name,
                   WorkloadCloudCredential.dynamic_folder == (dynamic_folder or None),
                   WorkloadCloudCredential.purpose == ((purpose or "").strip() or None),
                   WorkloadCloudCredential.status != "deleted").first())
    if dup is not None:
        raise WorkloadCloudError(
            f"{cloud}/{dynamic_name} is already registered as {dup.name!r} for the same "
            f"purpose. Two identities on one dynamic secret mint two billed streams for one "
            f"workload — give this one a different purpose, or use the existing row.")

    row = WorkloadCloudCredential(
        name=name, cloud=cloud, dynamic_name=dynamic_name,
        dynamic_folder=dynamic_folder or None,
        purpose=(purpose or "").strip() or None,
        ttl_seconds=int(ttl_seconds or 0) or None,
        # Recorded at registration from the cloud, so the page never has to decide at render
        # time whether a kill switch exists.
        revocable=revocable(cloud),
        issue_count=0, status="registered",
        workgroup=workgroup, created_by=created_by, expires_at=expires_at)
    db.add(row)
    db.commit()
    logger.info("workload-cloud: registered %r against %s dynamic secret %s/%s",
                name, cloud, dynamic_folder or "(root)", dynamic_name)
    return {"id": row.id, "cloud": cloud, "dynamic_name": dynamic_name,
            "revocable": bool(row.revocable),
            "credential_fields": list(credential_fields(cloud)),
            "note": ("nothing has been minted — issuance is metered, so the first "
                     "credential appears when you press Issue")}


def start_issue(db: Session, *, row_id: str, created_by: str) -> dict:
    """Enqueue one mint. **This is the metered call** — one press, one issuance, one charge."""
    row = get_row(db, row_id)
    if row is None:
        raise WorkloadCloudError(f"workload cloud identity {row_id} not found")
    if row.status == "deleted":
        raise WorkloadCloudError(f"{row.name} has been retired")
    return _enqueue(db, row, "issue", created_by)


def start_revoke(db: Session, *, row_id: str, created_by: str) -> dict:
    """Enqueue a revoke, refusing up front where the provider cannot honour one.

    Refused rather than attempted-and-swallowed: `revoke_lease` returns quietly on an AWS
    refusal, so a job that "succeeded" would leave an operator believing a live credential
    had been withdrawn. Better to say it cannot be, at the click.
    """
    row = get_row(db, row_id)
    if row is None:
        raise WorkloadCloudError(f"workload cloud identity {row_id} not found")
    if not row.lease_id:
        raise WorkloadCloudError(
            f"{row.name} holds no lease, so there is nothing to revoke")
    if not revocable(row.cloud):
        raise WorkloadCloudError(
            f"an {row.cloud} lease cannot be revoked. STS will not withdraw a credential it "
            f"has already signed, so the credential stays valid until "
            f"{row.lease_expires_at.isoformat() if row.lease_expires_at else 'it expires'}. "
            f"On {row.cloud} the TTL is the only control — which is why a short one matters "
            f"more here, not less. Delete the identity to stop it minting again.")
    return _enqueue(db, row, "revoke", created_by)


def start_decommission(db: Session, *, row_id: str, created_by: str) -> dict:
    """Enqueue teardown: revoke what can be revoked, then retire the identity.

    Also the reaper's entry point, so an expiry timer running out ends in exactly the
    teardown a human pressing Delete runs.

    **What this actually stops is minting**, and saying so matters: on AWS an outstanding
    lease keeps working until it expires whatever happens here. What retiring the identity
    ends is its ability to draw ANOTHER credential, which is the thing that would otherwise
    continue indefinitely and keep billing.
    """
    row = get_row(db, row_id)
    if row is None:
        raise WorkloadCloudError(f"workload cloud identity {row_id} not found")
    row.status = "retiring"
    return _enqueue(db, row, "retire", created_by)


def _enqueue(db: Session, row: WorkloadCloudCredential, action: str,
             created_by: str) -> dict:
    from . import job_service
    job = job_service.create_job(
        db, JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"row_id": row.id, "action": action})
    row.job_ids = json.dumps(job_ids(row) + [job.id])
    db.commit()
    return {"id": row.id, "job_id": job.id, "action": action}


# ── the worker ────────────────────────────────────────────────────────────────

async def run(db: Session, *, row_id: str, job_id: str, action: str = "issue") -> None:
    """Worker entry point for ``workload_cloud_credential``."""
    if action not in VALID_ACTIONS:
        raise WorkloadCloudError(f"unknown action {action!r}")
    row = get_row(db, row_id)
    if row is None:
        logger.warning("workload-cloud: row %s vanished before the %s", row_id, action)
        return
    from . import job_service
    job_service.set_running(db, job_id)
    try:
        if action == "issue":
            result = await _run_issue(db, row, job_id)
        elif action == "revoke":
            result = await _run_revoke(db, row, job_id)
        else:
            result = await _run_retire(db, row, job_id)
        job_service.set_completed(db, job_id, result=result)
    except Exception as exc:
        # The message, never a traceback and never a chained cause: it reaches a browser
        # through the row (CodeQL py/stack-trace-exposure, and the reason ansible_run_gate
        # gives at length).
        row.error_message = str(exc)[:2000]
        row.status = "failed"
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("workload-cloud: %s failed for %s: %s", action, row_id, exc)
        job_service.set_failed(db, job_id, str(exc))


async def _run_issue(db: Session, row: WorkloadCloudCredential, job_id: str) -> dict:
    """Mint one credential. **The credential itself is returned to nobody.**

    The job result carries the lease id, the expiry and the field names — enough for an
    operator to see that an issuance happened and when it dies, and not enough to use. A
    consumer that needs the values calls Workload Credentials itself with its own token,
    which is what puts the consumer in WC's audit log rather than this dashboard.

    That is the same rule the Kubernetes tab follows for its bearer token, and it is the
    reason neither tab has an endpoint that hands a credential back.
    """
    import asyncio

    from ..api.websocket import broadcast_progress
    from . import workload_credentials_service as wlc

    await broadcast_progress(job_id, 25,
                             f"Minting from {row.cloud} dynamic secret {row.dynamic_name}…")
    # `generate` is synchronous by design (see its module docstring on the thread pool), so
    # it goes off the event loop here rather than blocking every other request.
    result = await asyncio.to_thread(
        wlc.generate, row.dynamic_name, row.dynamic_folder or "")

    # Names only. `result["values"]` holds the credential and is deliberately not read,
    # not logged, and not put on the row — the one thing taken from it is which fields came
    # back, so the page can tell a consumer what to expect.
    got = sorted((result.get("values") or {}).keys())
    row.lease_id = result.get("lease_id") or None
    # The PROVIDER'S expiry, never one computed from the requested TTL: AWS caps a
    # role-chained credential at an hour and the provider clamps besides, so a page showing
    # the ask would tell an operator the credential lives longer than it does.
    row.lease_expires_at = result.get("expires_at")
    row.lease_issued_at = datetime.utcnow()
    row.issue_count = int(row.issue_count or 0) + 1
    row.status = "issued"
    row.error_message = None
    row.updated_at = datetime.utcnow()
    db.commit()

    if not row.lease_id:
        # Not fatal — the credential is real and already in the caller's hands. But without
        # a lease id nothing can revoke it or read it back, so an operator has to know that
        # the only remaining control is the TTL.
        from . import job_service
        job_service.append_job_log(
            db, job_id,
            "the provider returned no lease id, so this issuance cannot be revoked or "
            "inspected — the credential is valid and its expiry is the only control")

    expires = row.lease_expires_at.isoformat() if row.lease_expires_at else ""
    return {"id": row.id, "cloud": row.cloud, "lease_id": row.lease_id or "",
            "expires_at": expires, "issue_count": row.issue_count,
            "credential_fields": got,
            "revocable": bool(revocable(row.cloud)),
            "note": ("the credential was returned to the caller and is not stored here. "
                     + ("It can be revoked early." if revocable(row.cloud)
                        else f"It CANNOT be revoked — on {row.cloud} it stays valid until "
                             f"it expires."))}


async def _run_revoke(db: Session, row: WorkloadCloudCredential, job_id: str) -> dict:
    """Release the lease early. Only reached for a cloud that allows it — see `start_revoke`.

    Re-checked here rather than trusted from the enqueue: a job can sit in the queue while
    an operator changes the row, and `revoke_lease` swallows a provider refusal, so a
    mistaken run would otherwise report success on a credential that is still live.
    """
    import asyncio

    from ..api.websocket import broadcast_progress
    from . import workload_credentials_service as wlc

    if not revocable(row.cloud):
        raise WorkloadCloudError(
            f"an {row.cloud} lease cannot be revoked — the provider refuses, and reporting "
            f"success here would claim a live credential had been withdrawn")
    lease_id = row.lease_id
    if not lease_id:
        raise WorkloadCloudError(f"{row.name} holds no lease")

    await broadcast_progress(job_id, 40, "Releasing the lease…")
    await asyncio.to_thread(wlc.revoke_lease, lease_id)
    # Cleared because the row must not name an issuance that no longer exists — the id is a
    # handle to a live credential, and a stale one would offer a revoke that does nothing.
    row.lease_id = None
    row.lease_expires_at = None
    row.status = "registered"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"id": row.id, "revoked": True,
            "note": ("the credential stopped working immediately. The identity can still "
                     "mint another — delete it to stop that too.")}


async def _run_retire(db: Session, row: WorkloadCloudCredential, job_id: str) -> dict:
    """Revoke what can be revoked, then retire the identity so it cannot mint again.

    Best-effort on the revoke and it reports rather than raises: the lease may already have
    expired, the provider may refuse (AWS always does), or Workload Credentials may be
    unreachable — and none of those may leave the row stuck in `retiring`. What the teardown
    guarantees is the part that matters: **this identity will not mint another credential.**
    """
    import asyncio

    from ..api.websocket import broadcast_progress
    from . import workload_credentials_service as wlc

    notes = []
    if row.lease_id and revocable(row.cloud):
        await broadcast_progress(job_id, 30, "Releasing the outstanding lease…")
        try:
            await asyncio.to_thread(wlc.revoke_lease, row.lease_id)
            notes.append("outstanding lease released — it stopped working immediately")
        except Exception as exc:                        # noqa: BLE001 — see the docstring
            notes.append(f"could not release the lease: {exc}")
            logger.warning("workload-cloud: revoke during retire of %s failed: %s",
                           row.id, exc)
    elif row.lease_id:
        # The honest version, and the one an operator has to hear: AWS cannot withdraw it.
        when = (row.lease_expires_at.isoformat() if row.lease_expires_at
                else "its expiry")
        notes.append(
            f"the outstanding {row.cloud} lease CANNOT be revoked and stays valid until "
            f"{when} — STS will not withdraw a credential it has already signed. Retiring "
            f"the identity stops it minting another; it does not stop this one.")
    else:
        notes.append("no outstanding lease")

    row.lease_id = None
    row.lease_expires_at = None
    row.status = "deleted"
    row.updated_at = datetime.utcnow()
    db.commit()
    notes.append(f"identity retired after {row.issue_count or 0} issuance(s) — it can no "
                 f"longer mint")
    return {"id": row.id, "notes": notes}


async def inspect_lease(db: Session, *, row_id: str) -> dict:
    """The provider's own view of the current lease, read live.

    Read from Workload Credentials rather than served from the row, because the row is this
    dashboard's recollection of an issuance and the provider is the authority on whether it
    is still good. They diverge in exactly the case that matters — somebody revoked the
    lease elsewhere — and a page that reported the cached view would show a credential as
    live after it had been withdrawn.
    """
    import asyncio

    from . import workload_credentials_service as wlc

    row = get_row(db, row_id)
    if row is None:
        raise WorkloadCloudError(f"workload cloud identity {row_id} not found")
    if not row.lease_id:
        return {"id": row.id, "lease_id": "", "state": "none",
                "note": "this identity holds no lease"}
    try:
        data = await asyncio.to_thread(wlc.get_lease, row.lease_id)
    except Exception as exc:                            # noqa: BLE001
        # `state: unknown` is the honest answer and it is deliberately NOT "expired": the
        # provider did not say the lease was gone, this dashboard failed to ask. A page that
        # rendered an unreachable provider as an expired lease would report a credential dead
        # while it was still working, which is the more dangerous of the two wrong answers.
        #
        # The note carries the exception's TYPE NAME, never its message — the same rule the
        # POV clients follow. This dict is an HTTP response body, a stringified HTTP-client
        # exception carries the request URL and the provider's own body with it, and that is
        # a stack trace reaching an external user (CodeQL's py/stack-trace-exposure).
        #
        # The type is a COARSE signal and worth being honest about: the client wraps every
        # provider-side failure in `WorkloadCredentialsError`, so it says "the call to WC
        # failed" and not why. What it does distinguish is a provider failure from a bug in
        # this dashboard — which is the first fork an operator takes — and the log carries
        # the rest.
        logger.warning("workload-cloud: lease %s could not be read from the provider",
                       row.lease_id, exc_info=True)
        return {"id": row.id, "lease_id": row.lease_id, "state": "unknown",
                "note": ("Workload Credentials could not be reached "
                         f"({type(exc).__name__}) — see the dashboard log")}
    return {"id": row.id, "lease_id": row.lease_id,
            "state": lease_state(row),
            "expires_at": (row.lease_expires_at.isoformat()
                           if row.lease_expires_at else ""),
            "revocable": bool(revocable(row.cloud)),
            # The provider's payload, passed through for display. It carries lease metadata
            # rather than the credential — `generate` is the only call that returns values.
            "provider": data if isinstance(data, dict) else {}}
