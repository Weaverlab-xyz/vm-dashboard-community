"""The local Password Safe functional account on an AWS POV's Windows guests.

``pov_functional_account`` mints a POV's functional account from the guests' own login,
and that only works where every guest of a family reports the SAME login. On AWS it never
does: EC2Launch gives each Windows instance its own random Administrator password, so the
"one credential valid on every guest" rule that module enforces fails on any POV with two
Windows guests. Every AWS Windows VM was therefore skipped by the Password Safe half of
the wire-up.

So on a platform that holds a per-VM login (``holds_login``, AWS today) the dashboard
makes the credential agree instead of looking for one that does:

1. **One account, one password, every guest.** A built-in play
   (``builtin:pov-windows-functional-account``) creates the local account
   :data:`ACCOUNT` on each Windows guest, logging on as that guest's own Administrator.
   The password is generated once per POV and stored encrypted at
   :func:`password_config_key`. Only the var NAME reaches the job row.
2. **That account is the POV's functional account.** ``pov_functional_account.ensure``
   uses it in place of the guests' own login (see :func:`credential`).
3. **Administrator becomes a managed account and is rotated at once**
   (``pov_wireup.onboard_vm``), so the password EC2 still publishes stops working.
4. **Later WinRM runs log on as this account** (``pov_resource_broker.platform_login``).
   That is what keeps a Resource Broker install or a guest step working after rotation:
   nothing depends on EC2's stale copy, and no run has to check Administrator out of
   Password Safe.

What was prepared and rotated is recorded on the POV row's metadata, keyed by the
platform VM id, so teardown and the login precedence can read it without a schema change.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, PovEnvironmentVM
from . import (agent_ansible_meta, agent_service, config_service, job_service,
               lab_platforms, pov_credentials, pov_gateway)

logger = logging.getLogger(__name__)


class FunctionalPrepError(Exception):
    """A guest could not be given the functional account. The message names the remedy."""


# The local account's name. Short, lowercase and not a Windows reserved name; it says what
# it is to anybody who finds it in Computer Management.
ACCOUNT = "povpsfunc"

ASSET = "builtin:pov-windows-functional-account"

# The play's two inputs. The name is play data; the password is a secret var, so only its
# NAME and the config key it binds to are ever written to the job row.
USER_VAR = "pov_functional_user"
PASSWORD_VAR = "pov_functional_password"

# WinRM over HTTP, as the Resource Broker install uses.
WINRM_PORT = 5985

# Keyed on the POV ROW id, like every other `pov/<id>/...` secret the wire-up owns.
_PASSWORD_FMT = "pov/{env_id}/windows_functional_password"

# Platform VM ids, on the POV row's metadata.
_META_READY = "ps_functional_ready"
_META_ROTATED = "ps_admin_rotated"

# How the wire-up waits on the prepare runs. A Windows play over WinRM is well under a
# minute; the cap is for an agent that is slow to lease, not for the play.
POLL_S = 10
TIMEOUT_S = 15 * 60


# ── whether this applies ─────────────────────────────────────────────────────

def applies(env: PovEnvironment) -> bool:
    """True on a platform whose Windows guests each hold their OWN login (AWS today)."""
    try:
        mod = lab_platforms.adapter(env.platform)
    except Exception:  # noqa: BLE001 - an unknown platform is simply "no"
        return False
    return callable(getattr(mod, "holds_login", None))


def wanted(env: PovEnvironment) -> bool:
    """Whether this POV's wire-up should prepare the account: it applies AND there is a
    Password Safe tenant to onboard into. Without a tenant nothing would use it."""
    return applies(env) and bool(env.ps_tenant_id)


def windows_guests(db: Session, env: PovEnvironment) -> list:
    return [r for r in (db.query(PovEnvironmentVM)
                          .filter(PovEnvironmentVM.environment_id == env.id)
                          .order_by(PovEnvironmentVM.name).all())
            if r.guest_os == "windows" and (r.private_ip or "").strip()
            and (r.platform_vm_id or "")]


def windows_targets(db: Session, env: PovEnvironment) -> list[str]:
    """Every Windows guest's address, for the control node's WinRM grant.

    Every one rather than the named Resource Broker host alone: Password Safe onboards
    every Windows guest, and each needs the account first. Still bounded to this POV and
    to Windows. Empty when :func:`wanted` is false, so no other POV's grant widens.
    """
    if not wanted(env):
        return []
    return sorted({r.private_ip.strip() for r in windows_guests(db, env)})


# ── the password ─────────────────────────────────────────────────────────────

def password_config_key(env_id: str) -> str:
    return _PASSWORD_FMT.format(env_id=env_id)


def password(env: PovEnvironment) -> str:
    """This POV's functional account password, generated once and remembered."""
    key = password_config_key(env.id)
    existing = config_service.get_opt(key)
    if existing:
        return existing
    value = pov_credentials.generate_password()
    config_service.set(key, value)
    return value


def credential(env: PovEnvironment) -> tuple:
    """``(username, password)`` for the Password Safe functional account."""
    return ACCOUNT, password(env)


def forget_password(env: PovEnvironment) -> None:
    try:
        config_service.delete(password_config_key(env.id))
    except Exception:  # noqa: BLE001 - never fail a teardown over housekeeping
        logger.warning("POV %s: could not clear the functional account password", env.id,
                       exc_info=True)


# ── what was done, per guest ─────────────────────────────────────────────────

def _ids(env: PovEnvironment, key: str) -> list:
    raw = (env.metadata_dict or {}).get(key) or []
    return [str(v) for v in raw] if isinstance(raw, list) else []


def _mark(db: Session, env: PovEnvironment, key: str, vm: PovEnvironmentVM) -> None:
    meta = env.metadata_dict
    ids = set(_ids(env, key))
    ids.add(str(vm.platform_vm_id))
    meta[key] = sorted(ids)
    env.metadata_dict = meta
    db.commit()


def prepared(env: PovEnvironment, vm: PovEnvironmentVM) -> bool:
    return str(vm.platform_vm_id) in _ids(env, _META_READY)


def mark_prepared(db: Session, env: PovEnvironment, vm: PovEnvironmentVM) -> None:
    _mark(db, env, _META_READY, vm)


def rotated(env: PovEnvironment, vm: PovEnvironmentVM) -> bool:
    return str(vm.platform_vm_id) in _ids(env, _META_ROTATED)


def mark_rotated(db: Session, env: PovEnvironment, vm: PovEnvironmentVM) -> None:
    _mark(db, env, _META_ROTATED, vm)


def clear_records(db: Session, env: PovEnvironment) -> None:
    meta = env.metadata_dict
    for key in (_META_READY, _META_ROTATED):
        meta.pop(key, None)
    env.metadata_dict = meta
    db.commit()


def login(env: PovEnvironment, vm: PovEnvironmentVM):
    """``(ACCOUNT, password)`` for a guest that has the account, else ``None``.

    Read without generating: a guest that is recorded as prepared but whose password is
    gone (a teardown half-done) gets ``None`` and falls back to the platform's own login.
    """
    if not (applies(env) and vm.guest_os == "windows" and prepared(env, vm)):
        return None
    value = config_service.get_opt(password_config_key(env.id))
    return (ACCOUNT, value) if value else None


# ── the prepare run ──────────────────────────────────────────────────────────

def preflight(db: Session, env: PovEnvironment, vm: PovEnvironmentVM):
    """The control node's agent, or a refusal naming the remedy. Mirrors the guest-step
    preflight, minus the asset checks a built-in play does not need."""
    try:
        agent = pov_gateway.broker_agent(db, env)
    except pov_gateway.GatewayInstallError as exc:
        raise FunctionalPrepError(str(exc)) from None
    if not agent_service.supports_ansible(agent):
        raise FunctionalPrepError(agent_service.ansible_upgrade_hint(agent))
    reported = agent.reported_job_types_list
    if reported and "agent_ansible" not in reported:
        raise FunctionalPrepError(
            f"the control node's agent {agent.name!r} reports it may run "
            f"{', '.join(reported) or 'nothing'}, so its policy.yaml predates this grant. "
            f"Press Rebuild control node on this POV, then wire it again.")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise FunctionalPrepError(
            f"this dashboard does not permit {agent.name!r} to run Config Management. "
            f"Widen its allowed job types on the Agents page.")

    from . import pov_guest_step, pov_resource_broker
    problem = pov_resource_broker.login_problem(env, vm)
    if problem:
        raise FunctionalPrepError(problem)
    granted = pov_guest_step.granted_addresses(env)
    if granted and (vm.private_ip or "").strip() not in granted:
        raise FunctionalPrepError(
            f"{vm.name}'s address is not in the policy the control node's agent was last "
            f"given, so the run would fail as a connection timeout. Press Rebuild control "
            f"node on this POV, then wire it again.")
    return agent


def queue_prepare(db: Session, env: PovEnvironment, vm: PovEnvironmentVM, *,
                  created_by: str = "") -> Job:
    """Queue the built-in play on one Windows guest. Returns the job."""
    agent = preflight(db, env, vm)
    password(env)   # generated before the job exists, so the bundle fetch can resolve it
    meta = agent_ansible_meta.run_meta(
        object(),
        description=f"Create the Password Safe functional account on {vm.name} for POV "
                    f"{env.name}",
        asset_backend="",
        run_kind="vm",
        transport="winrm",
        target_host=vm.private_ip,
        target_port=WINRM_PORT,
        target_label=vm.name or vm.platform_vm_id,
        asset=ASSET,
        extra_vars={USER_VAR: ACCOUNT},
        # The var NAME and a config key, never the password.
        secret_vars={PASSWORD_VAR: password_config_key(env.id)},
        # How the bundle assembler fetches this guest's login: Administrator from EC2 on
        # the first run, this account once it exists.
        pov_environment_id=env.id,
        pov_vm_id=vm.platform_vm_id)
    job = job_service.create_job(
        db, job_type="agent_ansible", created_by=created_by,
        workgroup=env.workgroup, agent_id=agent.id, metadata=meta)
    logger.info("POV %s: queued the functional account on %s (job %s)", env.id, vm.name,
                job.id)
    return job


async def ensure_prepared(db: Session, env: PovEnvironment, *, log=None,
                          created_by: str = "", poll_s: float = POLL_S,
                          timeout_s: float = TIMEOUT_S) -> dict:
    """Give every unprepared Windows guest the account. Returns ``{vm name: problem}``.

    Queues one run per guest, then waits for all of them by reading their job rows, so
    the wire-up needs no completion hook from the agent API. A guest that fails is a
    problem RETURNED, never raised: the Password Safe half skips it and carries on.
    """
    def _log(line: str) -> None:
        if log:
            log(line)

    problems, jobs = {}, {}
    for vm in windows_guests(db, env):
        if prepared(env, vm):
            continue
        try:
            jobs[vm.id] = (vm, queue_prepare(db, env, vm, created_by=created_by).id)
        except FunctionalPrepError as exc:
            problems[vm.name] = str(exc)
            _log(f"{vm.name}: could not create the {ACCOUNT} account: {exc}")
    if jobs:
        _log(f"Creating the local {ACCOUNT} account on {len(jobs)} Windows guest(s); "
             f"Password Safe uses it as this POV's Windows functional account.")

    deadline = time.monotonic() + timeout_s
    while jobs:
        db.expire_all()
        for row_id, (vm, job_id) in list(jobs.items()):
            job = job_service.get_job(db, job_id)
            status = job.status if job is not None else "gone"
            if status in job_service.ACTIVE_STATUSES:
                continue
            del jobs[row_id]
            if status == "completed":
                mark_prepared(db, env, vm)
                _log(f"{vm.name}: {ACCOUNT} is ready.")
            else:
                reason = ((job.error_message if job is not None else "") or status)
                problems[vm.name] = f"the run that creates {ACCOUNT} {status}: {reason}"
                _log(f"{vm.name}: could not create {ACCOUNT} (job {job_id}, {status}).")
        if not jobs:
            break
        if time.monotonic() >= deadline:
            for vm, job_id in jobs.values():
                problems[vm.name] = (f"the run that creates {ACCOUNT} (job {job_id}) was "
                                     f"still waiting after {int(timeout_s // 60)} minutes. "
                                     f"Wire the POV again once it finishes.")
            break
        await asyncio.sleep(poll_s)
    return problems
