"""The POV's UI robot: a browser container the control node's agent starts to do the two
setup steps the Password Safe and PRA APIs cannot.

Design, and why it is shaped this way: ``docs/profiles/pov/design/ui-robot.md``. In short:

* ``installer``  fetch the Resource Broker installer and its key from Password Safe, upload
  the installer to the active storage backend, stage the key on this POV.
* ``integration``  create the PRA to Password Safe integration in PRA.

**What the dashboard owns here.** The preflight, the job row, and the sealed bundle that hands
the robot its UI logins. The robot calls the dashboard itself, as a SPIFFE-authenticated
service account, so nothing here accepts an upload or a key: those are the existing storage
and ``/resource-broker`` routes.

**The logins are write-only.** Stored Fernet-encrypted under ``pov/<env>/ui_robot/<field>``
(same shape as the Gateway deploy key), never returned by any route, never in job metadata,
never in the signed envelope. They leave only through the sealed per-job channel.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, RemoteAgent
from . import (agent_service, agent_ui_robot_meta, bt_tenant_service, config_service,
               job_service, pov_gateway)

logger = logging.getLogger(__name__)


class UiRobotError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# Binds a sealed bundle to this purpose (agent_sealing AAD): a bundle sealed for the robot
# cannot be opened as a Gateway key, or the reverse. Must match runners/agent/agent.py.
SEAL_REF = "pov-ui-robot-bundle"

_KEY_FMT = "pov/{env_id}/ui_robot/{field}"

# Every login field the robot can be given. The route accepts exactly these.
# `ps_api_key` and `ps_runas_user` are what PRA's Password Safe integration form asks for;
# they are separate fields rather than read off the tenant because the tenant's secret is
# what the dashboard itself authenticates with, and a robot login should be its own grant.
CREDENTIAL_FIELDS = ("ps_user", "ps_password", "ps_totp_secret",
                     "pra_user", "pra_password", "pra_totp_secret",
                     "ps_api_key", "ps_runas_user")

# What each task cannot run without. TOTP secrets are optional: an account without MFA has none.
_REQUIRED = {
    "installer": ("ps_user", "ps_password"),
    "integration": ("pra_user", "pra_password", "ps_api_key", "ps_runas_user"),
}


def _key(env_id: str, field: str) -> str:
    return _KEY_FMT.format(env_id=env_id, field=field)


def set_credentials(env: PovEnvironment, values: dict) -> list[str]:
    """Store the fields given. A key absent from ``values`` is left alone; a blank value
    clears it. Returns the fields that were written. Unknown keys are refused, not dropped."""
    unknown = sorted(set(values) - set(CREDENTIAL_FIELDS))
    if unknown:
        raise UiRobotError(f"unknown credential field(s): {', '.join(unknown)} "
                           f"(known: {', '.join(CREDENTIAL_FIELDS)})")
    written = []
    for field, value in values.items():
        config_service.set(_key(env.id, field), str(value or "").strip())
        written.append(field)
    return written


def credential_state(env: PovEnvironment) -> dict:
    """Which fields are stored, never their values."""
    return {f: bool(config_service.get(_key(env.id, f))) for f in CREDENTIAL_FIELDS}


def clear_credentials(env: PovEnvironment) -> None:
    for field in CREDENTIAL_FIELDS:
        try:
            config_service.delete(_key(env.id, field))
        except Exception:  # noqa: BLE001 — teardown must not fail on a key already gone
            logger.debug("POV %s: no %s to clear", env.id, field)


def _ui_url(base_url: str) -> str:
    """The scheme and host of a tenant, with any API path an operator pasted stripped off."""
    raw = (base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


def _tenant(db: Session, env: PovEnvironment, kind: str):
    tenant_id = env.ps_tenant_id if kind == "password_safe" else env.pra_tenant_id
    try:
        return bt_tenant_service.resolve(db, kind, tenant_id or None)
    except bt_tenant_service.BTTenantError as exc:
        label = "Password Safe" if kind == "password_safe" else "PRA"
        raise UiRobotError(f"this POV's {label} tenant could not be resolved: {exc}") from None


def wanted(db: Session, env: PovEnvironment) -> bool:
    """Whether this POV's control node should be granted the robot: it has a Password Safe
    tenant to drive. Decides the policy block and the image pull, nothing else."""
    try:
        bt_tenant_service.resolve(db, "password_safe", env.ps_tenant_id or None)
    except bt_tenant_service.BTTenantError:
        return False
    return True


def preflight(db: Session, env: PovEnvironment, task: str) -> RemoteAgent:
    """Everything checkable before a job row exists. Cheapest and most likely wrong first;
    every refusal ends "Nothing was queued." because a failed row renders only its error."""
    problem = agent_ui_robot_meta.check({"task": task})
    if problem or not task:
        raise UiRobotError(problem or "no UI robot task was named. Nothing was queued.")

    try:
        agent = pov_gateway.broker_agent(db, env)
    except pov_gateway.GatewayInstallError as exc:
        raise UiRobotError(f"{exc} Nothing was queued.") from None

    if not agent_service.supports_ui_robot(agent):
        raise UiRobotError(agent_service.ui_robot_upgrade_hint(agent))
    reported = agent.reported_job_types_list
    if reported and "agent_ui_robot" not in reported:
        raise UiRobotError(
            f"the control node's agent {agent.name!r} reports it may run "
            f"{', '.join(reported) or 'nothing'}; its policy.yaml predates the UI robot "
            f"grant. Press Rebuild control node on this POV to rewrite it. Nothing was queued.")
    if "agent_ui_robot" not in agent_service.allowed_job_types(agent):
        raise UiRobotError(
            f"this dashboard does not permit {agent.name!r} to run UI robot jobs. Widen "
            f"its allowed job types on the Agents page. Nothing was queued.")

    ps = _tenant(db, env, "password_safe")
    if not _ui_url(ps.base_url):
        raise UiRobotError(f"Password Safe tenant {ps.name!r} has no URL. Nothing was queued.")
    if task == "integration":
        pra = _tenant(db, env, "pra")
        if not _ui_url(pra.base_url):
            raise UiRobotError(f"PRA tenant {pra.name!r} has no URL. Nothing was queued.")

    stored = credential_state(env)
    missing = [f for f in _REQUIRED[task] if not stored[f]]
    if missing:
        raise UiRobotError(
            f"this POV has no stored robot login for: {', '.join(missing)}. Add them on the "
            f"POV (PUT /api/pov/managed/{env.id}/ui-robot/credentials); they are write-only. "
            f"Nothing was queued.")
    return agent


def queue(db: Session, env: PovEnvironment, *, task: str, created_by: str = "") -> Job:
    """Queue an ``agent_ui_robot`` job on this POV's control node agent.

    No local job wrapping it, for the reason ``pov_gateway.queue`` gives. ``environment_id``
    goes on the row, not in the envelope: it is how the bundle route derives which POV's
    logins this job may have.
    """
    agent = preflight(db, env, task)
    meta = agent_ui_robot_meta.ui_robot_meta(
        {"task": task}, description=f"UI robot ({task}) for POV {env.name}")
    meta["environment_id"] = env.id
    job = job_service.create_job(
        db, job_type="agent_ui_robot", created_by=created_by,
        workgroup=env.workgroup, agent_id=agent.id, metadata=meta)
    logger.info("POV %s: queued UI robot task %s on agent %s", env.id, task, agent.name)
    return job


def bundle_for_job(db: Session, job: Job, dashboard_url: str) -> tuple[dict, list[str]]:
    """The robot's logins and endpoints for ``job``, derived from the job row alone.

    Returns ``(bundle, scrub)``. The keys are the robot's own environment variable names, so
    the robot reads this file as it would its environment. ``scrub`` lists every secret so
    the agent redacts it from Live Output; the agent also holds them itself.
    """
    meta = job.metadata_dict or {}
    env_id = str(meta.get("environment_id") or "")
    if not env_id:
        raise UiRobotError("this job names no POV environment")
    env = db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()
    if env is None:
        raise UiRobotError("the POV environment this job belongs to is gone")
    task = agent_ui_robot_meta.normalize(meta)["task"]

    stored = {f: config_service.get(_key(env.id, f)) or "" for f in CREDENTIAL_FIELDS}
    missing = [f for f in _REQUIRED[task] if not stored[f]]
    if missing:
        raise UiRobotError(
            f"this POV has no stored robot login for: {', '.join(missing)}.")

    ps = _tenant(db, env, "password_safe")
    bundle = {
        "ROBOT_TASK": task,
        "DASHBOARD_URL": dashboard_url.rstrip("/"),
        "POV_ENV_ID": env.id,
        "PS_URL": _ui_url(ps.base_url),
    }
    if task == "installer":
        bundle.update({
            "PS_USER": stored["ps_user"],
            "PS_PASSWORD": stored["ps_password"],
            "PS_TOTP_SECRET": stored["ps_totp_secret"],
        })
    if task == "integration":
        pra = _tenant(db, env, "pra")
        bundle.update({
            "PRA_URL": _ui_url(pra.base_url),
            "PRA_USER": stored["pra_user"],
            "PRA_PASSWORD": stored["pra_password"],
            "PRA_TOTP_SECRET": stored["pra_totp_secret"],
            "PS_API_KEY": stored["ps_api_key"],
            "PS_RUNAS_USER": stored["ps_runas_user"],
        })
    scrub = [v for k, v in stored.items()
             if v and ("password" in k or "totp" in k or "api_key" in k)]
    return bundle, scrub
