"""POV environment lifecycle: provision, power, destroy.

Platform-agnostic. Everything platform-specific goes through the adapter resolved from
``lab_platforms``; this module never imports ``skytap_service`` directly, which is what
makes the second platform an adapter module rather than a second orchestrator.

Slice 3 covers a *running environment with a broker agent inside it and no PAM wiring
yet*. The Gateway and Resource Broker installs and the per-VM Password Safe / PRA wire-up
land in later slices — their columns already exist on the row so they need no migration.

The broker step is the first one that is **best-effort inside the provision**: an
environment that is up and reachable but has no agent in it is still a POV, still billing
and still reapable, so failing the whole provision over it would trade a fixable gap for a
destroyed environment. It records why on the row and the POV page offers a re-run.

The ordering rule that matters most is in :func:`run_env_provision` step 2: **the row is
persisted with the platform's id before anything else can fail.** An environment that
exists on the platform and not in this database is the one failure mode nothing can clean
up automatically, and every other step is written to be re-runnable around it.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM, SessionLocal
from . import (job_service, lab_platforms, pov_broker, pov_gateway,
               pov_resource_broker)

logger = logging.getLogger(__name__)

# Dashboard-side lifecycle. Distinct from the platform's runstate: an environment can be
# `active` here and `suspended` there, which is the normal resting state for a POV nobody
# is using.
STATUS_PROVISIONING = "provisioning"
STATUS_ACTIVE = "active"
STATUS_DESTROYING = "destroying"
STATUS_DESTROYED = "destroyed"
STATUS_FAILED = "failed"

# Statuses a power or destroy request may act on. An unrecognised status is REFUSED rather
# than assumed safe, so a state added later can only ever make this do less.
_ACTIONABLE = frozenset((STATUS_ACTIVE, STATUS_FAILED))


def _now() -> datetime:
    return datetime.utcnow()


def get(db: Session, env_id: str) -> PovEnvironment | None:
    return db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()


def _adapter(env: PovEnvironment):
    return lab_platforms.adapter(env.platform)


def _fail(db: Session, env: PovEnvironment, job_id: str, message: str) -> None:
    """Record a failure in both places an operator will look.

    A failed job surfaces only its ``error_message``, so the remedy has to be inside that
    string rather than in a log line nobody will read.
    """
    env.status = STATUS_FAILED
    env.error_message = message
    db.commit()
    job_service.set_failed(db, job_id, message)


# ── provision ────────────────────────────────────────────────────────────────

async def run_env_provision(job_id: str, meta: dict) -> None:
    """Create an environment from a template and bring it up.

    Steps, in the order they must happen:

      1. preflight — refuse before anything exists
      2. create, and PERSIST THE ID IMMEDIATELY
      3. name it and set the idle timer
      4. power on, and wait for it to settle
      5. read back the VMs
      6. install and enrol the broker agent — best-effort, and only ever AFTER 4

    Step 6 cannot move earlier. It hands the guest a fifteen-minute enrolment code, and a
    first boot is not bounded; injecting before the power-on gives the VM a code that
    expired while it was starting.
    """
    db = SessionLocal()
    try:
        env = get(db, meta.get("environment_id", ""))
        if env is None:
            job_service.set_failed(db, job_id, "the POV environment row is gone")
            return

        try:
            mod = _adapter(env)
        except lab_platforms.LabPlatformError as exc:
            _fail(db, env, job_id, str(exc))
            return

        # 1. Preflight. Everything checkable before a resource exists, checked here.
        if not mod.configured():
            _fail(db, env, job_id,
                  f"{env.platform} is not configured — add its credentials in "
                  f"Settings → Integrations, then destroy this row and try again.")
            return

        job_service.update_progress(db, job_id, 5, "Creating the environment…")

        # 2. Create, then persist the id BEFORE anything else can fail.
        try:
            created = await mod.create_environment(
                env.template_id, name=env.name, project_id=env.project_id or "")
        except Exception as exc:  # noqa: BLE001
            _fail(db, env, job_id,
                  f"could not create the environment from template "
                  f"{env.template_id}: {exc}")
            return

        env.platform_environment_id = created.get("id") or ""
        env.runstate = created.get("runstate") or ""
        env.region = created.get("region") or env.region
        db.commit()
        job_service.set_cloud_resource_id(db, job_id, env.platform_environment_id)
        job_service.update_progress(
            db, job_id, 25,
            f"Created {env.platform} environment {env.platform_environment_id}")

        # 3. The idle timer. Best-effort: an environment that is up but will not
        # auto-suspend costs money, which is worth a loud warning and not worth
        # destroying a working environment over.
        idle = int(meta.get("suspend_on_idle_seconds") or 0)
        if idle > 0 and lab_platforms.supports(env.platform, "idle_suspend"):
            try:
                await mod.update_environment(env.platform_environment_id,
                                             {"suspend_on_idle": idle})
                job_service.append_job_log(
                    db, job_id, f"Idle suspend set to {idle // 60} minutes.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("POV %s: could not set suspend_on_idle", env.id,
                               exc_info=True)
                job_service.append_job_log(
                    db, job_id,
                    f"WARNING: could not set the idle timer ({exc}). The environment "
                    f"will keep running until it is suspended by hand.")

        # 4. Power on and wait. The wait is the long part of this job.
        job_service.update_progress(db, job_id, 40, "Powering on…")
        try:
            await mod.set_runstate(env.platform_environment_id, "running")
            settled = await mod.wait_for_runstate(env.platform_environment_id, "running")
            env.runstate = settled.get("runstate") or ""
            db.commit()
        except Exception as exc:  # noqa: BLE001
            # The environment EXISTS. Leave the row pointing at it so Destroy can reap
            # it — failing without the id is how an orphan is created.
            _fail(db, env, job_id,
                  f"the environment was created ({env.platform_environment_id}) but did "
                  f"not come up: {exc}. Destroy it from the POV page to reclaim it.")
            return

        # 5. Read the VMs back.
        job_service.update_progress(db, job_id, 80, "Reading the environment's VMs…")
        try:
            await refresh_vms(db, env)
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: VM read-back failed", env.id, exc_info=True)
            job_service.append_job_log(
                db, job_id,
                f"WARNING: the environment is running but its VMs could not be read "
                f"({exc}). Refresh the POV page to try again.")

        env.status = STATUS_ACTIVE
        env.error_message = None
        db.commit()

        # 6. The broker agent. Deliberately after the status flip: everything above this
        # point decides whether the environment is usable, and this step decides only
        # whether the dashboard can reach into it. A broker failure must not put a
        # running environment in front of an operator as `failed`.
        await _install_broker(db, env, job_id)

        count = db.query(PovEnvironmentVM).filter(
            PovEnvironmentVM.environment_id == env.id).count()
        job_service.set_completed(db, job_id, {
            "environment_id": env.id,
            "platform_environment_id": env.platform_environment_id,
            "runstate": env.runstate,
            "vm_count": count,
            "broker_agent_id": env.broker_agent_id or "",
        })
    finally:
        db.close()


async def _install_broker(db: Session, env: PovEnvironment, job_id: str) -> None:
    """Step 6, with the failure swallowed on purpose.

    A POV with no broker is incomplete, not broken. The reason goes on the row — where the
    POV page renders it next to a Broker button that re-runs exactly this — and into the
    job log, because a warning that only exists in a container log is a warning nobody
    reads.
    """
    job_service.update_progress(db, job_id, 90, "Installing the POV broker agent…")
    try:
        summary = await pov_broker.ensure_broker(db, env, job_id=job_id)
        job_service.append_job_log(db, job_id, summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: broker install failed", env.id, exc_info=True)
        pov_broker.record_broker_error(db, env, str(exc))
        job_service.append_job_log(
            db, job_id,
            f"WARNING: the environment is up but its broker agent is not: {exc}")


async def refresh_vms(db: Session, env: PovEnvironment) -> int:
    """Re-read the platform's VM list into ``pov_environment_vms``.

    Upsert by ``platform_vm_id``, and prune what the platform no longer reports — but
    **never on an empty read**. A transient error that returned zero VMs would otherwise
    delete every row, taking the PAM artifact columns with it, and the sync would record
    success. That is a bug this codebase has already paid for once.
    """
    mod = _adapter(env)
    detail = await mod.get_environment(env.platform_environment_id)
    vms = detail.get("vms") or []

    env.runstate = detail.get("runstate") or env.runstate

    if not vms:
        logger.info("POV %s: platform reported no VMs; leaving existing rows alone",
                    env.id)
        db.commit()
        return 0

    existing = {v.platform_vm_id: v for v in db.query(PovEnvironmentVM).filter(
        PovEnvironmentVM.environment_id == env.id).all()}

    seen = set()
    for raw in vms:
        vm_id = str(raw.get("id") or "")
        if not vm_id:
            continue
        seen.add(vm_id)
        row = existing.get(vm_id)
        if row is None:
            row = PovEnvironmentVM(environment_id=env.id, platform_vm_id=vm_id)
            db.add(row)
        row.name = raw.get("name") or ""
        row.os_family = raw.get("os_family") or ""
        row.runstate = raw.get("runstate") or ""
        row.private_ip = raw.get("private_ip") or ""
        row.published_services_list = raw.get("published_services") or []

    for vm_id, row in existing.items():
        if vm_id not in seen:
            db.delete(row)

    db.commit()
    return len(seen)


# ── power ────────────────────────────────────────────────────────────────────

async def run_env_power(job_id: str, meta: dict) -> None:
    """Change an environment's runstate and wait for it to settle."""
    db = SessionLocal()
    try:
        env = get(db, meta.get("environment_id", ""))
        if env is None:
            job_service.set_failed(db, job_id, "the POV environment row is gone")
            return
        target = (meta.get("runstate") or "").strip().lower()
        mod = _adapter(env)

        if not env.platform_environment_id:
            job_service.set_failed(
                db, job_id,
                "this environment was never created on the platform, so there is "
                "nothing to power on or off.")
            return

        job_service.update_progress(db, job_id, 10, f"Requesting {target}…")
        try:
            await mod.set_runstate(env.platform_environment_id, target)
            settled = await mod.wait_for_runstate(env.platform_environment_id, target)
        except Exception as exc:  # noqa: BLE001
            job_service.set_failed(db, job_id, f"could not reach {target!r}: {exc}")
            return

        env.runstate = settled.get("runstate") or ""
        db.commit()
        job_service.set_completed(db, job_id,
                                  {"environment_id": env.id, "runstate": env.runstate})
    finally:
        db.close()


# ── destroy ──────────────────────────────────────────────────────────────────

async def run_env_destroy(job_id: str, meta: dict) -> None:
    """Delete the environment from the platform and mark the row destroyed.

    Reaps the POV's Resource Broker state, then its Gateway, then its broker agent, then
    the platform side; the per-VM PAM wiring's teardown slots in at the front as its slice
    lands. The property they all rely on is kept
    here: **the platform delete is reached even when an earlier step fails**, because a
    half-torn-down POV that keeps billing is the worse outcome.

    The row is marked ``destroyed`` rather than deleted. It is the inventory record of
    something that existed, and the job row alongside it is never pruned by age.
    """
    db = SessionLocal()
    try:
        env = get(db, meta.get("environment_id", ""))
        if env is None:
            job_service.set_failed(db, job_id, "the POV environment row is gone")
            return

        env.status = STATUS_DESTROYING
        db.commit()

        problems: list[str] = []

        # The Resource Broker first: it is purely local state (a stored installer key and
        # an id), so it cannot fail in a way worth stopping for, and clearing a customer's
        # key is the part that matters most.
        try:
            job_service.append_job_log(db, job_id, pov_resource_broker.teardown(db, env))
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: resource broker teardown failed", env.id,
                           exc_info=True)
            job_service.append_job_log(
                db, job_id, f"WARNING: could not clear the Resource Broker state ({exc}).")

        # The Gateway before the broker that runs it, and both before the platform.
        # Reversed, the removal job would be queued on an agent that has just been
        # revoked and could never lease it.
        try:
            job_service.append_job_log(db, job_id, pov_gateway.teardown(db, env))
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: gateway teardown failed", env.id, exc_info=True)
            # Not a `problem`: the environment delete below takes the container with it,
            # and the PRA-side node is a customer-appliance object this dashboard never
            # created. Blocking the reaping of a billing environment on tidying somebody
            # else's appliance is the wrong trade.
            job_service.append_job_log(
                db, job_id,
                f"WARNING: the Gateway could not be removed cleanly ({exc}). The "
                f"environment delete takes the container; retire the node in PRA by hand.")

        # The broker agent goes next. It is an enrolled principal that can lease work;
        # deleting its VM out from under it leaves a row that keeps polling from a machine
        # that no longer exists, and whatever job it holds running nowhere.
        try:
            job_service.append_job_log(db, job_id, pov_broker.teardown(db, env))
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: broker teardown failed", env.id, exc_info=True)
            problems.append(f"broker agent teardown failed: {exc}")

        if env.platform_environment_id:
            job_service.update_progress(db, job_id, 50,
                                        "Deleting the environment from the platform…")
            try:
                mod = _adapter(env)
                await mod.delete_environment(env.platform_environment_id)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"platform delete failed: {exc}")
        else:
            job_service.append_job_log(
                db, job_id,
                "No platform environment was ever created; nothing to delete there.")

        # Its VM rows go with it. They describe an environment that no longer exists,
        # and keeping them would put phantom VMs in the inventory.
        db.query(PovEnvironmentVM).filter(
            PovEnvironmentVM.environment_id == env.id).delete()

        if problems:
            # The row stays visible and still carries its platform id, so a re-run can
            # finish the job. Marking it destroyed here would hide a live environment.
            _fail(db, env, job_id,
                  "; ".join(problems) + " — the environment may still exist on the "
                  "platform. Re-run Destroy once the cause is cleared.")
            return

        env.status = STATUS_DESTROYED
        env.runstate = ""
        env.error_message = None
        db.commit()
        job_service.set_completed(db, job_id, {"environment_id": env.id,
                                               "status": STATUS_DESTROYED})
    finally:
        db.close()


def may_act_on(env: PovEnvironment) -> tuple[bool, str]:
    """Whether a power/destroy request may proceed, and why not when it may not.

    An unrecognised status is refused rather than assumed safe — the same rule the expiry
    sweep follows, so a status added later can only ever make this do less.
    """
    if env.status in _ACTIONABLE:
        return True, ""
    if env.status == STATUS_PROVISIONING:
        return False, "this environment is still being provisioned"
    if env.status == STATUS_DESTROYING:
        return False, "this environment is already being destroyed"
    if env.status == STATUS_DESTROYED:
        return False, "this environment has already been destroyed"
    return False, f"this environment is in an unrecognised state ({env.status!r})"
