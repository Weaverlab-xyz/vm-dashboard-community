"""Run a staged playbook, script or installer on a named guest inside a POV.

Slice 5b built exactly one of these — the Password Safe Resource Broker install — and it
is the general case with a specific asset and a specific set of arguments. The runbook a
POV is usually being driven from wants several more of the same shape: BeyondTrust's Skytap
Password Safe POC step-by-step asks an SE to add the Remote Desktop Session Host role to
``BtPocApp01`` by hand (step 3), to install SQL Server and SSMS on the same guest (also step
3, whose body the document never wrote), and to run ``SetupMoreUsers.ps1`` on the domain
controller (use case 18).

Every one of those is "put this file on that guest and run it", which the machinery already
does. So this module is deliberately thin: it is ``pov_resource_broker`` with the asset and
the target chosen by an operator instead of fixed.

**No new agent verb, and no image rebuild.** The job is ``agent_ansible``, which has shipped
since agent 2.3, and the play comes from ``ansible_local_service.generate_playbook_yaml`` —
already able to wrap a ``.ps1`` (``win_script``), a ``.sh`` (``script``), an ``.exe``/``.msi``
(``win_package``) or use a ``.yml`` as-is. Nothing about the agent changes.

## What actually gated this, and it was not the play

``pov_broker.render_policy`` grants the agent a **closed list of ``/32`` targets**, and
until this module existed that list was one address: ``pov_resource_broker.windows_targets``
returns the named Resource Broker host alone once somebody has named one. So the broker
agent could run a Windows play against ``BtPocBroker01`` and *nothing else* — every step-3
task names a different guest.

Widening it is the substance of this slice, and it is done by **opt-in, never by default**:

  * an operator names the guests they want to be able to configure (:func:`configure`);
  * ``pov_broker``'s policy render unions those addresses with the two it already grants;
  * a POV that has named none behaves exactly as it did before.

The grant widens because somebody asked for a specific guest, not because a POV exists.

## The refusal that stops a support call

``policy.yaml`` is written when the agent is **enrolled**, so naming a guest today does not
grant it until the POV is re-brokered. Without help, a run against a newly-named guest fails
as a **WinRM timeout** — and a WinRM timeout reads as a firewall problem, a credential
problem, or a guest that is not up, in any order but the right one.

So the render records what it granted (:func:`record_grant`) and :func:`preflight` refuses
against that record, naming the remedy: press Broker. It is the same discipline as
``pov_resource_broker``'s "press Broker to rewrite policy.yaml" refusal, one field along.

The record is what the DASHBOARD last rendered, not what the agent confirms holding — the
agent reports its job types but not its target CIDRs, so this is the honest limit. A stale
record can only ever produce a spurious refusal with a remedy that fixes it, never a run
this dashboard thought was granted and was not.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, PovEnvironmentVM
from . import (agent_ansible_meta, agent_service, ansible_local_service, job_service,
               pov_gateway, storage_service)

logger = logging.getLogger(__name__)


class GuestStepError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# The opted-in guests, by NAME rather than by address: an address changes when a POV is
# re-provisioned from the same template and a name does not, and the operator typed the
# name. Same reasoning as `pov_resource_broker`'s `rb_vm_name`.
_META_VMS = "guest_step_vms"

# What `pov_broker` last rendered into policy.yaml. Written by the render, read by the
# preflight; see the module docstring on why it is the dashboard's record rather than the
# agent's.
_META_GRANTED = "ansible_granted"

# WinRM over HTTP and SSH, matching `pov_broker.ANSIBLE_PORTS` / `ANSIBLE_SSH_PORTS`. Not
# imported from there because this module is what FEEDS that render, and a cycle between
# the two would be a real one.
_WINDOWS = "windows"
_LINUX = "linux"

# WinRM over HTTP, the same port `pov_resource_broker` uses and the one the generated
# policy grants. 5986 is granted too but not chosen here: picking it would need a
# certificate on the guest that a POV template does not carry.
WINRM_PORT = 5985
SSH_PORT = 22


# ── the opt-in list ──────────────────────────────────────────────────────────

def opted_in_names(env: PovEnvironment) -> list[str]:
    """The guest names an operator has opened up for configuration, in order."""
    raw = env.metadata_dict.get(_META_VMS) or []
    if not isinstance(raw, list):
        return []
    return [str(n).strip() for n in raw if str(n).strip()]


def configure(db: Session, env: PovEnvironment, *, vm_names: list[str] | None) -> None:
    """Set or clear the opted-in guests. ``None`` leaves them alone; ``[]`` clears them.

    Names are stored as given, deduplicated case-insensitively, and **not** validated
    against the POV's VM rows here. A name that matches nothing is refused later by
    :func:`select_vm` with what it found — the same rule ``pov_resource_broker.configure``
    follows, and it matters because an operator may name a guest from a template Part 2
    before those VMs have been added.
    """
    if vm_names is None:
        return
    seen, ordered = set(), []
    for name in vm_names:
        name = str(name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        ordered.append(name)
    meta = env.metadata_dict
    if ordered:
        meta[_META_VMS] = ordered
    else:
        meta.pop(_META_VMS, None)
    env.metadata_dict = meta
    db.commit()


def _rows(db: Session, env: PovEnvironment) -> list:
    return (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())


def opted_in_vms(db: Session, env: PovEnvironment) -> list:
    """The ``PovEnvironmentVM`` rows for the opted-in names that actually exist.

    Silently skips a name with no row: this feeds the POLICY, and a policy render must not
    fail because somebody typed a guest that is not there yet. The refusal belongs on the
    run, where an operator is watching.
    """
    wanted = {n.lower() for n in opted_in_names(env)}
    if not wanted:
        return []
    return [r for r in _rows(db, env) if (r.name or "").strip().lower() in wanted]


def _targets(db: Session, env: PovEnvironment, family: str) -> list[str]:
    return sorted({(r.private_ip or "").strip() for r in opted_in_vms(db, env)
                   if (r.os_family or "").strip().lower() == family
                   and (r.private_ip or "").strip()})


def windows_targets(db: Session, env: PovEnvironment) -> list[str]:
    """Opted-in Windows guests, for the WinRM half of the grant."""
    return _targets(db, env, _WINDOWS)


def linux_targets(db: Session, env: PovEnvironment) -> list[str]:
    """Opted-in Linux guests, for the SSH half of the grant.

    A blank ``os_family`` is in neither list. Slice 3 made blank mean *unknown*, and a
    guest granted the wrong port is a run that fails as a timeout.
    """
    return _targets(db, env, _LINUX)


# ── what the policy actually granted ─────────────────────────────────────────

def record_grant(db: Session, env: PovEnvironment, addresses: list[str]) -> None:
    """Record the addresses ``pov_broker`` just rendered into ``policy.yaml``.

    Called by the render rather than computed here, so what is recorded is what was
    WRITTEN — including the two grants this module knows nothing about (the Resource Broker
    host and the Entitle agent host). A record assembled independently would drift from the
    file the moment either of those changed.
    """
    meta = env.metadata_dict
    cleaned = sorted({str(a).strip() for a in (addresses or []) if str(a).strip()})
    if cleaned:
        meta[_META_GRANTED] = cleaned
    else:
        meta.pop(_META_GRANTED, None)
    env.metadata_dict = meta
    db.commit()


def granted_addresses(env: PovEnvironment) -> list[str]:
    raw = env.metadata_dict.get(_META_GRANTED) or []
    if not isinstance(raw, list):
        return []
    return [str(a).strip() for a in raw if str(a).strip()]


# ── the target ───────────────────────────────────────────────────────────────

def select_vm(db: Session, env: PovEnvironment, vm_name: str) -> PovEnvironmentVM:
    """The guest a step will run on, or a refusal naming what it found.

    Exact name match, case-insensitively — the rule ``pov_broker.select_broker_vm`` and
    ``pov_resource_broker.select_rb_vm`` both follow. Deliberately not "the first Windows
    guest": a POV template with a domain controller and two member servers has three, and
    running an unattended installer on whichever came back first is not a mistake anyone
    can undo.
    """
    wanted = str(vm_name or "").strip()
    if not wanted:
        raise GuestStepError(
            "name the guest this step runs on. Its name is on the POV's VMs tab.")

    rows = _rows(db, env)
    match = next((r for r in rows
                  if (r.name or "").strip().lower() == wanted.lower()), None)
    if match is None:
        known = ", ".join(sorted((r.name or "?") for r in rows)) or "none yet"
        raise GuestStepError(
            f"this POV has no guest called {wanted!r}. It has: {known}.")

    if wanted.lower() not in {n.lower() for n in opted_in_names(env)}:
        raise GuestStepError(
            f"{match.name} is not opened up for configuration on this POV. Add it to the "
            f"guest-step list first — the agent is granted a closed list of addresses, so "
            f"a run at a guest outside it fails as a connection timeout rather than as a "
            f"permission error.")

    family = (match.os_family or "").strip().lower()
    if family not in (_WINDOWS, _LINUX):
        raise GuestStepError(
            f"{match.name} has no known operating system, so this dashboard cannot choose "
            f"between WinRM and SSH for it. Re-read the POV's VMs from the platform; a "
            f"blank family means unknown, never Linux.")
    if not (match.private_ip or "").strip():
        raise GuestStepError(
            f"{match.name} has no address yet. Power the POV on and re-read its VMs.")
    return match


# ── preflight ────────────────────────────────────────────────────────────────

def preflight(db: Session, env: PovEnvironment, *, vm_name: str, asset: str,
              arguments: str = "") -> tuple:
    """``(agent, vm)`` for a runnable step, or a refusal naming the remedy.

    Ordered so that the cheapest and most common misconfigurations are named first, and so
    that a refusal never mentions a later problem an operator cannot see yet.
    """
    try:
        # No broker / a vanished row / an offline or revoked agent are three different
        # fixes, and this already tells them apart. Translated rather than propagated so
        # this module's callers catch one type -- the pattern `platform_login` follows for
        # `pov_credentials.CredentialParseError`.
        agent = pov_gateway.broker_agent(db, env)
    except pov_gateway.GatewayInstallError as exc:
        raise GuestStepError(str(exc)) from None

    if not agent_service.supports_ansible(agent):
        raise GuestStepError(agent_service.ansible_upgrade_hint(agent))
    reported = agent.reported_job_types_list
    if reported and "agent_ansible" not in reported:
        raise GuestStepError(
            f"the broker agent {agent.name!r} reports it may run "
            f"{', '.join(reported) or 'nothing'} — its policy.yaml predates the "
            f"guest-step grant. Press Broker on this POV to rewrite it, then try again.")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise GuestStepError(
            f"this dashboard does not permit {agent.name!r} to run Config Management. "
            f"Widen its allowed job types on the Agents page.")

    staged = str(asset or "").strip()
    if not staged:
        raise GuestStepError(
            "name the file this step runs. Upload it on the Storage page first — a "
            "PowerShell script for a Windows guest, a shell script for a Linux one, or a "
            "playbook.")
    # `asset_type` maps an unrecognised extension to "playbook" rather than to nothing, so
    # there is no unknown-type refusal to write here: `storage_service._ASSET_EXTENSIONS`
    # is what decides whether a file can be staged and listed at all, and the picker only
    # ever offers one that passed it.
    atype = ansible_local_service.asset_type(staged)
    if arguments and atype != "winpkg":
        # Refused rather than dropped. Only the `win_package` play templates an arguments
        # variable; `win_script` and `script` take none, so a value typed here would be
        # accepted, stored on the job, and silently have no effect on the run.
        raise GuestStepError(
            f"{staged!r} takes no arguments — only a Windows installer (.exe / .msi) does. "
            f"Put the arguments inside the script, or upload an installer.")

    vm = select_vm(db, env, vm_name)
    family = (vm.os_family or "").strip().lower()
    if family == _WINDOWS and atype in ("script", "rpm", "deb"):
        raise GuestStepError(
            f"{staged!r} is a Linux asset and {vm.name} is a Windows guest. Upload a .ps1 "
            f"or an installer for it.")
    if family == _LINUX and atype in ("powershell", "winpkg"):
        raise GuestStepError(
            f"{staged!r} is a Windows asset and {vm.name} is a Linux guest. Upload a .sh "
            f"or a playbook for it.")

    granted = granted_addresses(env)
    if granted and (vm.private_ip or "").strip() not in granted:
        raise GuestStepError(
            f"{vm.name} is on this POV's guest-step list but its address is not in the "
            f"policy the broker agent was last given. Press Broker to rewrite it — "
            f"without that the run reaches the agent and fails as a connection timeout.")
    return agent, vm


# ── the job ──────────────────────────────────────────────────────────────────

def queue(db: Session, env: PovEnvironment, *, vm_name: str, asset: str,
          arguments: str = "", created_by: str = "") -> Job:
    """Queue one guest step on this POV's broker agent."""
    agent, vm = preflight(db, env, vm_name=vm_name, asset=asset, arguments=arguments)
    family = (vm.os_family or "").strip().lower()
    windows = family == _WINDOWS

    extra_vars = {}
    if arguments:
        extra_vars[ansible_local_service.WINPKG_ARGS_VAR] = arguments

    # Everything here is RESOLVED rather than passed through: the address comes from the
    # platform's own VM read and the transport from the guest's family. The only operator
    # values that reach the job are the asset key and the arguments, both of which the
    # preflight has already checked against the asset's type.
    #
    # `pov_environment_id` + `pov_vm_id` are what make the login work without storing one:
    # `agent_ansible_bundle` reads the lab platform's stored credential for that VM at
    # bundle-assembly time and adds it to the scrub set. Same channel the Resource Broker
    # install uses, and the reason there is no login field here either.
    meta = agent_ansible_meta.run_meta(
        object(),
        description=f"Run {asset} on {vm.name} for POV {env.name}",
        asset_backend=storage_service.active_backend(),
        run_kind="vm",
        transport="winrm" if windows else "ssh",
        target_host=vm.private_ip,
        target_port=WINRM_PORT if windows else SSH_PORT,
        target_label=vm.name or vm.platform_vm_id,
        asset=asset,
        extra_vars=extra_vars,
        pov_environment_id=env.id,
        pov_vm_id=vm.platform_vm_id)

    job = job_service.create_job(
        db, job_type="agent_ansible", created_by=created_by,
        workgroup=env.workgroup, agent_id=agent.id, metadata=meta)
    logger.info("POV %s: queued guest step %s on %s (job %s)",
                env.id, asset, vm.name, job.id)
    return job


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """This POV's guest-step state for one row — no network calls.

    ``guest_step_stale`` is the one an operator acts on: it says a guest has been opened up
    since the agent was last given its policy, so the next run at it would time out. The
    remedy is Broker, and the POV list can say so without anybody pressing anything first.
    """
    names = opted_in_names(env)
    rows = opted_in_vms(db, env)
    granted = set(granted_addresses(env))
    pending = sorted({(r.private_ip or "").strip() for r in rows
                      if (r.private_ip or "").strip() and granted
                      and (r.private_ip or "").strip() not in granted})
    return {
        "guest_step_vms": names,
        "guest_step_resolved": sorted((r.name or "") for r in rows),
        "guest_step_ready": bool(rows and env.broker_agent_id),
        "guest_step_pending_grant": pending,
        "guest_step_stale": bool(pending),
    }
