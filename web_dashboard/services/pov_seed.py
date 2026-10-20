"""Sample activity for a demo: make a freshly wired POV's tenant look lived-in.

A POV wired an hour ago has nothing in its Password Safe request history, and the first
screen an SE shows -- "here is who checked out what, and when" -- is an empty table. The
fix was to check a few credentials out by hand before the call.

This does that, as a job, from a **fixed catalog**. There is no free text anywhere in a
seed: a seed is an id in :data:`SEEDS`, the accounts it touches are the ones this POV's
own wire-up onboarded, and every request carries a reason tagged
``[pov-seed:<pov>:<seed>]`` so anybody reading the tenant's audit can tell sample activity
from real use.

**It writes to the customer's tenant, and says so.** That is the point of it, so the
button's confirmation names the tenant, the enqueue is audited, and nothing here runs
without a person pressing it. What it can never do:

  * rotate anything -- check-in is the plain ``Checkin`` (``ps_api_service.request_cycle``);
  * return, log or store a credential -- the release is read and discarded;
  * be undone in the tenant -- request and release history are the tenant's audit trail,
    and "clearing" sample activity would mean editing it. The dashboard keeps its own
    record of each run under ``extra_data["seeds"]``.

**What is deliberately not seeded**, so nobody goes looking:

  * PRA sessions: there is no API that creates one; a session needs a representative.
  * Entitle requests: the only request client here (``entitle_service``) uses the
    instance-wide Entitle credentials, not this POV's tenant, and an auto-approved request
    is a real grant. Sample activity must not be either of those.
  * EPM for Linux events: there is no events API. Producing them means running commands on
    a guest, which ``pov_guest_step`` can do with an operator-staged script.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, PovEnvironmentVM
from ..utils.clock import utcnow
from . import bt_tenant_service, job_service, ps_api_service

logger = logging.getLogger(__name__)

JOB_TYPE = "pov_env_seed"
META_KEY = "seeds"
# At most this many accounts per run: enough to fill a request table, few enough that a
# POV with forty onboarded guests does not put forty requests into a customer's audit.
MAX_ACCOUNTS = 5
# Another run of the same seed is refused inside this window.
RUN_MIN_SECONDS = 60


@dataclass(frozen=True)
class Seed:
    id: str
    label: str
    product: str              # the POV tenant it writes to: env.<product>_tenant_id
    description: str
    writes: str               # what lands in the tenant, said on the button's confirmation
    extra: dict = field(default_factory=dict)


SEEDS = (
    Seed(
        id="ps_checkout_cycle",
        label="Password Safe check-outs",
        product="password_safe",
        description=("Requests, releases and checks back in the credential of up to "
                     f"{MAX_ACCOUNTS} of this POV's onboarded accounts, so the request "
                     "history and release audit show real activity."),
        writes=("credential requests and releases (plain check-in, nothing is rotated), "
                "tagged as sample activity"),
    ),
)

_BY_ID = {s.id: s for s in SEEDS}


class SeedError(ValueError):
    """A refusal written for the person who pressed the button."""


def get(seed_id: str) -> Seed | None:
    return _BY_ID.get(seed_id)


def reason_for(env: PovEnvironment, seed: Seed) -> str:
    """The tag every request carries. Short: Password Safe caps the reason field."""
    return f"[pov-seed:{(env.name or env.id)[:40]}:{seed.id}] Sample activity for a demo"


def _accounts(db: Session, env: PovEnvironment) -> list:
    """``[(account_id, system_id)]`` this POV's wire-up onboarded, oldest guest first."""
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id,
                      PovEnvironmentVM.ps_managed_account_id.isnot(None),
                      PovEnvironmentVM.ps_managed_system_id.isnot(None))
              .order_by(PovEnvironmentVM.name).all())
    out = []
    for vm in rows:
        try:
            out.append((int(vm.ps_managed_account_id), int(vm.ps_managed_system_id)))
        except (TypeError, ValueError):
            continue
    return out[:MAX_ACCOUNTS]


def _in_flight(db: Session, env_id: str) -> str:
    rows = (db.query(Job).filter(Job.job_type == JOB_TYPE,
                                 Job.status.in_(job_service.ACTIVE_STATUSES)).all())
    return next((r.id for r in rows if r.metadata_dict.get("environment_id") == env_id), "")


def _record(env: PovEnvironment) -> dict:
    rec = (env.metadata_dict or {}).get(META_KEY)
    return rec if isinstance(rec, dict) else {}


def availability(db: Session, env: PovEnvironment, seed: Seed) -> str:
    """Why this seed cannot run here, or "" when it can."""
    if env.status != "active":
        return "only an active POV can be seeded"
    if seed.product == "password_safe":
        if not env.ps_tenant_id:
            return "this POV has no Password Safe tenant"
        if not _accounts(db, env):
            return "no account has been onboarded into Password Safe yet; run the wire-up"
    return ""


def describe(db: Session, env: PovEnvironment) -> dict:
    """The seed catalog for one POV, with what each did last. No calls."""
    rec = _record(env)
    running = _in_flight(db, env.id)
    return {
        "running": running,
        "seeds": [{
            "id": s.id, "label": s.label, "product": s.product,
            "description": s.description, "writes": s.writes,
            "unavailable": availability(db, env, s),
            "last": rec.get(s.id) or {},
        } for s in SEEDS],
    }


def enqueue(db: Session, env: PovEnvironment, seed_id: str, *, by: str) -> str:
    """Queue one seed run. Returns the job id; raises :class:`SeedError`."""
    seed = get(seed_id)
    if seed is None:
        raise SeedError(f"no sample-activity set called {seed_id!r}")
    why = availability(db, env, seed)
    if why:
        raise SeedError(why)
    running = _in_flight(db, env.id)
    if running:
        return running
    last = core_when((_record(env).get(seed.id) or {}).get("at"))
    if last and (utcnow() - last).total_seconds() < RUN_MIN_SECONDS:
        raise SeedError("this ran moments ago; give the tenant a minute")
    job = job_service.create_job(db, job_type=JOB_TYPE, created_by=by,
                                 workgroup=env.workgroup,
                                 metadata={"environment_id": env.id, "seed": seed.id})
    return job.id


def core_when(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None


async def _run_ps(db: Session, env: PovEnvironment, seed: Seed) -> list:
    tenant = bt_tenant_service.resolve(db, "password_safe", env.ps_tenant_id)
    creds = ps_api_service.tenant_creds(tenant.base_url, tenant.client_id, tenant.secret)
    return await ps_api_service.request_cycle(creds, _accounts(db, env),
                                              reason=reason_for(env, seed))


async def run_seed(job_id: str, meta: dict) -> None:
    """The job body. Records the outcome on the row whether it worked or not."""
    from ..database import SessionLocal
    from . import pov_env_service

    db = SessionLocal()
    try:
        env = pov_env_service.get(db, meta.get("environment_id", ""))
        seed = get(meta.get("seed", ""))
        if env is None or seed is None:
            job_service.set_failed(db, job_id, "the POV or the sample-activity set is gone")
            return
        why = availability(db, env, seed)
        if why:
            job_service.set_failed(db, job_id, why)
            return
        job_service.update_progress(db, job_id, 20, f"Running {seed.label}…")
        try:
            results = await _run_ps(db, env, seed)
        except (bt_tenant_service.BTTenantError, ps_api_service.PSApiError) as exc:
            results, fatal = [], str(exc)[:400]
        else:
            fatal = ""
        ok = sum(1 for r in results if r["ok"])
        for r in results:
            job_service.append_job_log(
                db, job_id, f"account {r['account_id']}: "
                            + ("checked out and back in" if r["ok"] else r["detail"]))
        db.refresh(env)
        meta_all = dict(env.metadata_dict or {})
        rec = dict(meta_all.get(META_KEY) or {})
        prev = rec.get(seed.id) or {}
        rec[seed.id] = {"at": utcnow().isoformat(), "job_id": job_id,
                        "ok": ok, "failed": len(results) - ok,
                        "runs": int(prev.get("runs") or 0) + 1,
                        "error": fatal or next((r["detail"] for r in results if not r["ok"]),
                                               "")}
        meta_all[META_KEY] = rec
        env.metadata_dict = meta_all
        db.commit()
        if fatal or not ok:
            job_service.set_failed(
                db, job_id, fatal or f"Password Safe refused all {len(results)} request(s): "
                                     f"{results[0]['detail'] if results else ''}")
            return
        job_service.set_completed(db, job_id, {"environment_id": env.id, "seed": seed.id,
                                               "ok": ok, "failed": len(results) - ok})
    finally:
        db.close()
