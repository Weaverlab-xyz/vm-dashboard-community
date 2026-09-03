"""Installing a Password Safe Resource Broker inside a POV environment.

Slice 5b. See ``docs/design/pov-resource-broker.md`` for why it is shaped this way; the
short version is that **the RB package comes from the customer's own Password Safe
tenant**, so the dashboard can never fetch it and the "policy names the image" pattern
``pov_gateway`` uses does not transfer. The customer stages the installer through the
existing Config-Management upload, and this runs it.

Three things follow, and each removes something rather than adding it:

**No new agent verb.** Delivery is ``agent_ansible``, which has shipped since agent 2.3 —
so unlike the Gateway slice this costs no image rebuild. What it does cost is a *wider
grant* on the broker's policy, and §4 of the design note is about not widening it further
than one host.

**No new credential in this database.** The Windows login comes from the lab platform's
own ``stored_credentials``, parsed by ``pov_credentials`` and fetched per run.

**No Linux Resource Broker.** ``BeyondTrust.Agents.Bootstrapper.exe`` is a Windows program,
so the RB lives on a *second* special VM and the Gateway stays on the Linux broker.

The two installer parameters are the part most worth reading carefully. A silent install
needs **both** ``INSTALLKEY`` and ``ZONE``; without the zone the installer *prompts*, and
a prompt in an unattended run is not an error — it is a process that sits there until the
run's timeout kills it. So both are refused at preflight. ``RESTART`` is deliberately never
set: during a silent install it reboots the machine automatically, which would drop the
WinRM session mid-play and report the failure of a step that had in fact succeeded.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, PovEnvironmentVM
from . import (agent_ansible_meta, agent_service, ansible_local_service, config_service,
               job_service, lab_platforms, pov_credentials, pov_gateway, storage_service)

logger = logging.getLogger(__name__)


class ResourceBrokerError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# Where a POV's installer key lives. Same shape as the Gateway's deploy key, so it is
# Fernet-encrypted at rest, resolvable from an external vault by reference, and carried by
# the config-migration tool.
_KEY_FMT = "pov/{env_id}/rb_installer_key"

# The VM in the template that hosts the Resource Broker. A convention, not a discovery —
# and a different one from the broker's, because these are different machines.
DEFAULT_RB_VM_NAME = "rb"

# Where the zone and the staged installer are recorded. Neither is a secret: a zone is a
# name and the asset is a storage key, so both live on the row's metadata rather than in
# the encrypted config space.
_META_ZONE = "rb_zone"
_META_ASSET = "rb_asset"
_META_VM_NAME = "rb_vm_name"

# The play variable the generated Windows-installer wrapper renders into `arguments:`.
# Owned by ansible_local_service so the wrapper and this caller cannot disagree about the
# name; imported rather than re-spelled for that reason.
ARGS_VAR = ansible_local_service.WINPKG_ARGS_VAR

# The var the installer key is bound to. Only the NAME reaches the job row and the
# envelope — the value is resolved when the agent fetches the sealed bundle, exactly as
# `epml_token_var` does for an EPM-L installation token.
KEY_VAR = "rb_install_key"

# WinRM over HTTP, matching `agent_ansible_meta._DEFAULT_PORT`. The credential reaches the
# run sealed to a per-fetch key rather than riding WinRM's own transport, which is why 5985
# is the default here as it is there.
RB_PORT = 5985


# ── stored values ────────────────────────────────────────────────────────────

def installer_key_config_key(env_id: str) -> str:
    return _KEY_FMT.format(env_id=env_id)


def set_installer_key(env: PovEnvironment, key: str) -> None:
    """Store a POV's installer key, encrypted. Blank clears it."""
    config_service.set(installer_key_config_key(env.id), (key or "").strip())


def has_installer_key(env: PovEnvironment) -> bool:
    return bool(config_service.get(installer_key_config_key(env.id)))


def clear_installer_key(env: PovEnvironment) -> None:
    try:
        config_service.delete(installer_key_config_key(env.id))
    except Exception:  # noqa: BLE001 — teardown must survive a key that is already gone
        logger.debug("POV %s: no installer key to clear", env.id)


def installer_key_for_job(db: Session, job: Job) -> str:
    """The installer key for a job, derived from the job row.

    Called from the bundle assembler, never from a request body — the same rule
    ``pov_gateway.deploy_key_for_job`` follows, and for the same reason: without it a
    stolen agent identity could ask for another POV's key against a job it owns.
    """
    env_id = str((job.metadata_dict or {}).get("pov_environment_id") or "")
    if not env_id:
        raise ResourceBrokerError("this job names no POV environment")
    env = db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()
    if env is None:
        raise ResourceBrokerError("the POV environment this job belongs to is gone")
    key = config_service.get(installer_key_config_key(env.id))
    if not key:
        raise ResourceBrokerError(
            "this POV has no Resource Broker installer key stored. Add it on the POV page "
            "— it is the key shown beside the installer download in the Password Safe "
            "tenant.")
    return key


def zone(env: PovEnvironment) -> str:
    return str(env.metadata_dict.get(_META_ZONE) or "").strip()


def asset(env: PovEnvironment) -> str:
    return str(env.metadata_dict.get(_META_ASSET) or "").strip()


def rb_vm_name(env: PovEnvironment) -> str:
    return (str(env.metadata_dict.get(_META_VM_NAME) or "").strip()
            or DEFAULT_RB_VM_NAME)


def stored_rb_vm_name(env: PovEnvironment) -> str:
    """What was actually stored, or "" — as opposed to ``rb_vm_name``, which substitutes
    the default.

    A caller deciding whether to fill a blank field needs to tell "nobody set this" from
    "somebody set it to the default", and the defaulted reader above cannot. The one caller
    is the blueprint pre-fill, whose whole rule is never to overwrite a value the operator
    chose."""
    return str(env.metadata_dict.get(_META_VM_NAME) or "").strip()


def configure(db: Session, env: PovEnvironment, *, zone_name: str | None = None,
              asset_key: str | None = None, vm_name: str | None = None) -> None:
    """Set the non-secret half. ``None`` leaves a field alone; ``""`` clears it."""
    meta = env.metadata_dict
    for key, value in ((_META_ZONE, zone_name), (_META_ASSET, asset_key),
                       (_META_VM_NAME, vm_name)):
        if value is None:
            continue
        value = str(value).strip()
        if value:
            meta[key] = value
        else:
            meta.pop(key, None)
    env.metadata_dict = meta
    db.commit()


def set_application_host(db: Session, env: PovEnvironment, value: int | None) -> str:
    """Set or clear ``ps_application_host_id``. Returns a line describing what happened.

    **An override, and the only writer.** ``application_host_id`` is a managed-system
    attribute naming another managed system that carries ``IsApplicationHost``; it is not
    the Resource Broker handle, and it is not what routes Password Safe to a private
    address — the broker's resource ZONE and the workgroup mapped to it are. Every other
    caller in this codebase leaves it at 0 (``cloud_database_service``) or takes an
    operator-typed config integer (``ps_vm_hook``), and the cloud-DB path proves 0 works
    through a broker.

    So this exists for the tenant that genuinely wants one, and to make sure a wire-up is
    never stuck on a column nothing fills automatically. ``0`` and ``None`` clear it, which
    is the normal state.

    Not verified against a live tenant. If setting one changes nothing observable, that is
    the expected outcome and the zone mapping is what to check.
    """
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        raise ResourceBrokerError(
            "an application host id is a whole number — a Password Safe managed system id, "
            "or 0 to clear it") from None
    if value < 0:
        raise ResourceBrokerError(
            "an application host id is a positive managed system id, or 0 to clear it")
    env.ps_application_host_id = value or None
    db.commit()
    if value:
        return f"Password Safe application host set to {value} for this POV."
    return "Cleared this POV's Password Safe application host."


# ── the RB host ──────────────────────────────────────────────────────────────

def select_rb_vm(db: Session, env: PovEnvironment) -> PovEnvironmentVM:
    """The Windows VM that will host the Resource Broker, or a refusal naming what it found.

    Exact name match, case-insensitively — the same rule ``pov_broker.select_broker_vm``
    follows, and deliberately not "the first Windows VM": a POV template with a domain
    controller and a member server has two, and installing a Resource Broker on whichever
    the platform happened to list first is not a decision a position in a list can make.
    """
    wanted = rb_vm_name(env).strip().lower()
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())
    if not rows:
        raise ResourceBrokerError(
            "this environment has no VM rows yet, so there is nowhere to install a "
            "Resource Broker. Refresh the POV once the platform reports its VMs.")

    match = next((r for r in rows if (r.name or "").strip().lower() == wanted), None)
    if match is None:
        found = ", ".join(sorted((r.name or r.platform_vm_id) for r in rows)) or "none"
        raise ResourceBrokerError(
            f"no VM in this environment is named {wanted!r}, so there is nowhere to "
            f"install the Resource Broker. Found: {found}. Rename the template's VM, or "
            f"set a different Resource Broker VM name on this POV.")

    # os_family is blank when the platform did not say, and slice 3 was deliberate that
    # blank means UNKNOWN rather than a guess. Refusing both cases is right: a Linux VM
    # reached over WinRM fails as a connection timeout, which reads as a firewall problem
    # and sends an SE somewhere else entirely.
    if (match.os_family or "").strip().lower() != "windows":
        reported = match.os_family or "nothing"
        raise ResourceBrokerError(
            f"{match.name!r} reports its OS as {reported}, and a Resource Broker is a "
            f"Windows program (Server 2019 or 2022 x64). Point this POV at a Windows VM — "
            f"a WinRM run against a Linux host fails as a connection timeout, which looks "
            f"like a firewall.")
    if not (match.private_ip or "").strip():
        raise ResourceBrokerError(
            f"{match.name!r} has no private address yet, so there is nothing to connect "
            f"to. Power the environment on and refresh the POV.")
    return match


def windows_targets(db: Session, env: PovEnvironment) -> list[str]:
    """The addresses a POV's broker may run a playbook against.

    The named RB host alone when it is resolvable, and every Windows VM otherwise. That
    fallback exists because the broker's ``policy.yaml`` is written at *enrolment*, which
    is usually before anyone has chosen an RB host — so the alternative is a policy that
    grants nothing until the POV is re-brokered.

    Bounded twice either way: to this environment, and to its Windows guests. A blank
    ``os_family`` is excluded, because slice 3 made blank mean unknown.
    """
    try:
        return [select_rb_vm(db, env).private_ip]
    except ResourceBrokerError:
        rows = (db.query(PovEnvironmentVM)
                  .filter(PovEnvironmentVM.environment_id == env.id).all())
        return sorted({(r.private_ip or "").strip() for r in rows
                       if (r.os_family or "").strip().lower() == "windows"
                       and (r.private_ip or "").strip()})


# ── the installer arguments ──────────────────────────────────────────────────

def installer_arguments(env: PovEnvironment) -> str:
    """The bootstrapper's command line, as an Ansible template.

    ``INSTALLKEY`` is rendered from a variable rather than interpolated here: the value is
    resolved when the agent fetches the sealed bundle, so what lands in the job row and the
    envelope is a var *name*. The zone is not a secret and goes in as-is.

    ``RESTART`` is absent on purpose — see the module docstring. ``-l`` names a log the
    installer writes on the target, which is where an operator looks when the exit code
    alone is not enough.
    """
    return (f'/quiet -l "install.log" '
            f'INSTALLKEY={{{{ {KEY_VAR} }}}} ZONE={zone(env)}')


# ── preflight ────────────────────────────────────────────────────────────────

def preflight(db: Session, env: PovEnvironment) -> tuple:
    """Everything checkable before a job row exists. Returns ``(agent, vm)``.

    Ordered cheapest-and-most-likely-wrong first, so an SE who has not staged an installer
    is told that rather than told about a zone they were about to be asked for anyway.
    """
    agent = pov_gateway.broker_agent(db, env)   # same refusals, same remedies

    if not agent_service.supports_ansible(agent):
        raise ResourceBrokerError(agent_service.ansible_upgrade_hint(agent))
    reported = agent.reported_job_types_list
    if reported and "agent_ansible" not in reported:
        raise ResourceBrokerError(
            f"the broker agent {agent.name!r} reports it may run "
            f"{', '.join(reported) or 'nothing'} — its policy.yaml predates the Resource "
            f"Broker grant. Press Broker on this POV to rewrite it, then try again.")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise ResourceBrokerError(
            f"this dashboard does not permit {agent.name!r} to run Config Management. "
            f"Widen its allowed job types on the Agents page.")

    staged = asset(env)
    if not staged:
        raise ResourceBrokerError(
            "no Resource Broker installer is staged for this POV. Download it from the "
            "Password Safe tenant, upload the .exe on the Storage page, and name it "
            "here.")
    if ansible_local_service.asset_type(staged) != "winpkg":
        raise ResourceBrokerError(
            f"{staged!r} is not a Windows installer. The Resource Broker bootstrapper is "
            f"an .exe — upload that file rather than a script or a playbook.")

    if not has_installer_key(env):
        raise ResourceBrokerError(
            "this POV has no Resource Broker installer key. It is shown beside the "
            "installer download in the Password Safe tenant.")
    if not zone(env):
        # The parameter most often missed, and the one whose absence does NOT fail fast:
        # a silent install without ZONE prompts, and an unattended prompt is a run that
        # hangs until its timeout with an install log ending mid-dialog.
        raise ResourceBrokerError(
            "this POV names no resource zone. A silent install needs ZONE as well as "
            "INSTALLKEY — without it the installer waits at a prompt nobody can answer, "
            "and the run hangs until it times out.")

    vm = select_rb_vm(db, env)
    return agent, vm


# ── the job ──────────────────────────────────────────────────────────────────

def queue(db: Session, env: PovEnvironment, *, created_by: str = "") -> Job:
    """Queue the Resource Broker install on this POV's broker agent."""
    agent, vm = preflight(db, env)

    # Every value here goes through `overrides` rather than the payload, and that is the
    # right channel rather than a convenience: `run_meta`'s payload is what an OPERATOR
    # typed, while overrides are what the endpoint RESOLVED. Nothing below is typed by
    # anyone — the address comes from the platform's VM read, the arguments are generated,
    # and the key is a config path. Passing an address as a request field is the same
    # mistake as letting the dashboard set a hypervisor connection's host.
    meta = agent_ansible_meta.run_meta(
        object(),
        description=f"Install the Password Safe Resource Broker for POV {env.name}",
        asset_backend=storage_service.active_backend(),
        run_kind="vm",
        transport="winrm",
        target_host=vm.private_ip,
        target_port=RB_PORT,
        target_label=vm.name or vm.platform_vm_id,
        asset=asset(env),
        # The var NAME, never the key. Resolved when the agent fetches the bundle.
        secret_vars={KEY_VAR: installer_key_config_key(env.id)},
        # Play data rather than connection configuration, so it is allowed where an
        # `ansible_*` var is not.
        extra_vars={ARGS_VAR: installer_arguments(env)},
        # How the bundle assembler knows to fetch this VM's login from the platform. Ids,
        # not credentials — the discipline every other key in RUN_META_KEYS follows.
        pov_environment_id=env.id,
        pov_vm_id=vm.platform_vm_id)

    job = job_service.create_job(
        db, job_type="agent_ansible", created_by=created_by,
        workgroup=env.workgroup, agent_id=agent.id, metadata=meta)
    logger.info("POV %s: queued a Resource Broker install on %s via agent %s",
                env.id, vm.name, agent.name)
    return job


async def platform_login(db: Session, env_id: str, vm_id: str) -> tuple:
    """``(username, password)`` for a POV VM, read from the lab platform.

    The reason slice 5b stores no Windows credential. Fetched per run and never written
    down — which also means it cannot go stale, and that a POV whose template password
    changed picks the new one up on the next run with nothing to update here.
    """
    env = db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()
    if env is None:
        raise ResourceBrokerError("the POV environment this job belongs to is gone")

    caps = lab_platforms.capabilities(env.platform)
    if not caps.get("stored_credentials"):
        raise ResourceBrokerError(
            f"{caps.get('label', env.platform)} does not store VM credentials, so the "
            f"login for this run has to be set on the POV by hand.")

    mod = lab_platforms.adapter(env.platform)
    try:
        entries = await mod.stored_credentials(env.platform_environment_id, vm_id)
    except Exception as exc:  # noqa: BLE001
        # The exception is logged, never carried outward: it can name the appliance, the
        # resolved address, or a chained cause from somewhere unrelated.
        logger.warning("POV %s: reading stored credentials for VM %s failed",
                       env.id, vm_id, exc_info=True)
        raise ResourceBrokerError(
            f"could not read this VM's stored credentials from {env.platform} "
            f"({type(exc).__name__}). Check the platform is reachable and the account can "
            f"see the environment.") from None

    label = next((v.name for v in db.query(PovEnvironmentVM).filter(
        PovEnvironmentVM.environment_id == env.id,
        PovEnvironmentVM.platform_vm_id == vm_id).all()), None) or "that VM"
    try:
        return pov_credentials.pick(entries, vm_label=label)
    except pov_credentials.CredentialParseError as exc:
        raise ResourceBrokerError(str(exc)) from None


# ── teardown ─────────────────────────────────────────────────────────────────

def teardown(db: Session, env: PovEnvironment) -> str:
    """Forget this POV's Resource Broker. Returns a job-log line.

    Deliberately does **not** uninstall anything. The environment delete takes the VM and
    the broker with it moments later, and an uninstall run would need the very WinRM
    session the teardown is about to make unreachable. What matters locally is the key.

    The RB's own registration in the Password Safe tenant is a customer-side object this
    dashboard never created, so it says so rather than pretending to reap it.
    """
    lines = []
    if has_installer_key(env):
        clear_installer_key(env)
        lines.append("Cleared the stored Resource Broker installer key.")
    if env.ps_application_host_id:
        # Not the broker's registration — that is a customer-side object named by zone and
        # install key, and this column never held it. This is the operator's override, so
        # forgetting it is all that is owed.
        lines.append(
            f"Forgot the Password Safe application host override "
            f"({env.ps_application_host_id}).")
        env.ps_application_host_id = None
        db.commit()
    if not lines:
        return "No Resource Broker to clean up."
    # Only once there WAS one: a POV that never installed a broker must not be told to
    # retire one in the tenant.
    lines.append(
        "The Resource Broker's own registration in the Password Safe tenant is a "
        "customer-side object this dashboard never created; retire it in the tenant.")
    return " ".join(lines)


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """The RB's configured state for one POV row — no network calls."""
    return {
        "rb_vm_name": rb_vm_name(env),
        "rb_zone": zone(env),
        "rb_asset": asset(env),
        "rb_has_key": has_installer_key(env),
        "rb_ready": bool(asset(env) and zone(env) and has_installer_key(env)
                         and env.broker_agent_id),
        "ps_application_host_id": env.ps_application_host_id,
    }
