"""The OT demo cell's DMZ broker, as a POV component: just-in-time HMI access via Entitle.

The demo cell's broker is three things, and a POV already had one of them:

* **An Entitle agent inside the plant.** ``pov_entitle_agent`` installs one per POV,
  minted in the POV's own Entitle tenant, so nothing new here.
* **The FUXA HMI's admin password, rotated off FUXA's seeded default**, so the adapter
  below holds a credential nobody else knows.
* **An Entitle "REST API" adapter on an OpenFaaS runtime beside the agent**
  (``functions/fnworkloads/fuxa_hmi_access.py``). An Entitle grant creates a short-lived
  HMI user and hands the requester the PRA Web Jump that reaches the HMI.

This module adds the last two. It reuses everything it can a layer down: the play
variables come from ``ot_faas_service.deploy_spec`` and ``rotate_spec``, which build the
demo cell's runs too, so the adapter a POV runs is the adapter the demo runs.

**Delivered through the POV's broker agent, not the dashboard's runner.** The demo cell
queues ``ansible_local`` runs from an in-cloud runner, which a POV does not have. A POV's
private guests are reached only through its broker agent (``agent_ansible``), the
channel ``pov_entitle_agent`` already uses for the same host. The agent leases one job at
a time, oldest first, so the rotation queued here always runs before the deploy that
depends on it.

**The playbooks are staged, the same contract the demo cell has.** ``examples/`` is not
copied into the image, so ``fuxa-admin-rotate.yml`` and ``openfaas-function-deploy.yml``
are read from the active storage backend by name.

**The host is the Entitle agent's host, and it must be an ``ot-broker`` guest.** The
integration's endpoint is ``gateway.openfaas.svc.cluster.local``, which resolves only
inside that guest's k3s, and Entitle calls it through the agent. An adapter on any other
guest would be unreachable by the agent. The ``ot-broker`` image is the one with
OpenFaaS baked in (``provisioners/ot``, ``OT_ROLE=broker``).

**Dry run is the default.** The adapter's own default, kept: a deploy reports what each
grant would do without touching the HMI until an operator turns it off.

Credentials and Secret names are keyed by the POV environment's id through
``ot_faas_service``'s helpers, so they live at ``ot/<env-id>/...``. Teardown removes
them with the integration, ahead of the Entitle agent token.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, PovEnvironmentVM
from . import (agent_ansible_meta, config_service, entitle_registration_service,
               job_service, ot_faas_service, pov_cell_roles, pov_entitle_agent,
               storage_service)

logger = logging.getLogger(__name__)


class OTAdapterError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# Non-secret state on the row's metadata, the split pov_entitle_agent makes.
_META = {
    "integration_id": "ot_adapter_integration_id",
    "rotate_job_id": "ot_adapter_rotate_job_id",
    "deploy_job_id": "ot_adapter_deploy_job_id",
    "workload": "ot_adapter_workload",
    "dry_run": "ot_adapter_dry_run",
    "hmi_vm": "ot_adapter_hmi_vm",
}

# The registration's Terraform state. In the encrypted config store rather than on the
# row: it records the shared secret Entitle authenticates to the adapter with.
_STATE_FMT = "pov/{env_id}/ot_adapter_tf_state"

SSH_PORT = 22


def state_config_key(env_id: str) -> str:
    return _STATE_FMT.format(env_id=env_id)


def _meta(env: PovEnvironment, key: str):
    return env.metadata_dict.get(_META[key])


def _set_meta(db: Session, env: PovEnvironment, **values) -> None:
    meta = env.metadata_dict
    for key, value in values.items():
        if value is None:
            meta.pop(_META[key], None)
        else:
            meta[_META[key]] = value
    env.metadata_dict = meta
    db.commit()


# ── the guests ───────────────────────────────────────────────────────────────

def _rows(db: Session, env: PovEnvironment) -> list:
    return (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())


def hmi_vm(db: Session, env: PovEnvironment) -> PovEnvironmentVM:
    """The OT simulator guest whose HMI the adapter manages. Exactly one, by role."""
    sims = [r for r in _rows(db, env) if r.cell_role == pov_cell_roles.OT_SIM]
    if not sims:
        raise OTAdapterError(
            "this POV has no OT simulator guest. Give the guest running the ot-sim image "
            "the ot-sim cell role on the VMs tab, then run Wire up.")
    if len(sims) > 1:
        names = ", ".join(sorted(r.name or r.platform_vm_id for r in sims))
        raise OTAdapterError(
            f"this POV has {len(sims)} OT simulator guests ({names}), and one adapter "
            f"manages one HMI. Leave the cell role on one of them.")
    vm = sims[0]
    if not (vm.private_ip or "").strip():
        raise OTAdapterError(
            f"{vm.name} has no private address yet. Power the environment on and "
            f"refresh the POV.")
    return vm


def hmi_url(vm: PovEnvironmentVM) -> str:
    return f"http://{vm.private_ip}:{pov_cell_roles.OT_HMI_PORT}"


def web_jump_name(env: PovEnvironment, vm: PovEnvironmentVM) -> str:
    """The HMI Web Jump ``pov_cell_roles.wire_extras`` built. The grant hands this name to
    the requester, because their browser has no route to the HMI and the Web Jump does."""
    return f"{env.name}-{vm.name}-hmi"


def cell_record(env: PovEnvironment, vm: PovEnvironmentVM) -> dict:
    """The cell-shaped record ``ot_faas_service.workload_env`` reads, built from the POV."""
    return {"instance_name": f"{env.name}-{vm.name}",
            "ot_hmi_url": hmi_url(vm),
            "ot_web_jump_name": web_jump_name(env, vm)}


# ── preflight ────────────────────────────────────────────────────────────────

def preflight(db: Session, env: PovEnvironment) -> tuple:
    """Everything checkable before a job row exists. Returns ``(agent, host, hmi)``.

    The Entitle agent's own preflight first, because this runs on the same broker agent
    against the same host and every one of its refusals applies here unchanged.
    """
    try:
        agent, host, _tenant = pov_entitle_agent.preflight(db, env)
    except pov_entitle_agent.EntitleAgentError as exc:
        raise OTAdapterError(str(exc)) from None

    if host.cell_role != pov_cell_roles.OT_BROKER:
        raise OTAdapterError(
            f"the Entitle agent host {host.name!r} is not an OT DMZ broker guest. The "
            f"adapter runs on the OpenFaaS runtime baked into the ot-broker image, in the "
            f"same k3s as the Entitle agent that calls it. Point the Entitle agent at the "
            f"ot-broker guest (or give {host.name!r} that role if it runs that image), "
            f"and install the agent there.")
    if not pov_entitle_agent.has_token(env) or not pov_entitle_agent.agent_token_name(env):
        raise OTAdapterError(
            "this POV's Entitle agent is not installed yet, and the integration is "
            "agent-brokered. Install the Entitle agent first.")

    hmi = hmi_vm(db, env)
    have = hmi.cell_artifacts_dict.get("hmi") or {}
    if not have.get("tf_state"):
        raise OTAdapterError(
            f"{hmi.name} has no HMI Web Jump yet, and a grant hands the requester that "
            f"Web Jump because nothing else reaches the HMI. Run Wire up first.")

    if not storage_service.active_backend():
        raise OTAdapterError(
            f"no storage backend is configured, and the two plays are read from it: "
            f"stage {ot_faas_service.FUXA_ROTATE_PLAYBOOK} and "
            f"{ot_faas_service.FAAS_DEPLOY_PLAYBOOK} from examples/playbooks/ot/.")
    return agent, host, hmi


# ── the jobs ─────────────────────────────────────────────────────────────────

def _job(db: Session, env: PovEnvironment, *, agent, host: PovEnvironmentVM, asset: str,
         spec: dict, description: str, created_by: str) -> Job:
    meta = agent_ansible_meta.run_meta(
        object(),
        description=description,
        asset_backend=storage_service.active_backend(),
        run_kind="vm",
        transport="ssh",
        target_host=host.private_ip,
        target_port=SSH_PORT,
        target_label=host.name or host.platform_vm_id,
        asset=asset,
        # NAMES of config keys, never values. Resolved when the agent fetches the bundle.
        secret_vars=spec["secret_vars"],
        extra_vars=spec["extra_vars"],
        pov_environment_id=env.id,
        pov_vm_id=host.platform_vm_id)
    return job_service.create_job(
        db, job_type="agent_ansible", created_by=created_by,
        workgroup=env.workgroup, agent_id=agent.id, metadata=meta)


async def queue(db: Session, env: PovEnvironment, *, dry_run: bool = True,
                created_by: str = "", workload: str = "") -> str:
    """Rotate the HMI password, deploy the adapter, register it. Returns a note.

    Safe to press again: the rotation and the deploy converge on values minted once, and
    a second press is how an operator switches dry run off. The registration happens
    once; while its state is stored a press does not register a second integration.
    """
    from . import pov_wireup

    agent, host, hmi = preflight(db, env)
    try:
        tenant = await pov_wireup.entitle_tenant_ctx(db, env)
    except pov_wireup.WireupError as exc:
        raise OTAdapterError(str(exc)) from None
    if not tenant:
        raise OTAdapterError("this POV is not wired into an Entitle tenant.")

    record = cell_record(env, hmi)
    try:
        deploy = ot_faas_service.deploy_spec(env.id, record, workload, dry_run=dry_run)
    except ot_faas_service.OTFaasError as exc:
        raise OTAdapterError(str(exc)) from None
    rotate = ot_faas_service.rotate_spec(env.id, record["ot_hmi_url"])

    # Rotation FIRST. The adapter is given this password as a mounted secret, and the
    # agent runs these in the order they were queued.
    rotate_job = _job(db, env, agent=agent, host=host,
                      asset=ot_faas_service.FUXA_ROTATE_PLAYBOOK, spec=rotate,
                      description=f"Rotate the HMI admin password on {hmi.name} for POV "
                                  f"{env.name}",
                      created_by=created_by)
    deploy_job = _job(db, env, agent=agent, host=host,
                      asset=ot_faas_service.FAAS_DEPLOY_PLAYBOOK, spec=deploy,
                      description=f"Deploy the Entitle HMI adapter ({deploy['workload']}) "
                                  f"on {host.name} for POV {env.name}",
                      created_by=created_by)
    _set_meta(db, env, rotate_job_id=rotate_job.id, deploy_job_id=deploy_job.id,
              workload=deploy["workload"], dry_run=bool(dry_run), hmi_vm=hmi.name)

    notes = [f"Queued the HMI password rotation (job {rotate_job.id}) and the adapter "
             f"deploy (job {deploy_job.id}) on {host.name}"
             + (", in dry-run mode" if dry_run else ", with live HMI grants") + "."]
    notes.append(await register(db, env, tenant=tenant))
    return " ".join(notes)


async def register(db: Session, env: PovEnvironment, *, tenant: dict) -> str:
    """Register the adapter in the POV's Entitle tenant, agent-brokered. Idempotent.

    Registered as soon as the deploy is queued, the order the demo cell uses: the
    deploy is not awaited, so Entitle's first sync may run before the function answers.
    It fails, and Entitle retries it.
    """
    if config_service.get(state_config_key(env.id)):
        return (f"Already registered in Entitle (integration "
                f"{_meta(env, 'integration_id') or '?'}).")
    bearer = (config_service.get(ot_faas_service.bearer_config_key(env.id)) or "").strip()
    if not bearer:
        raise OTAdapterError(
            "the adapter's bearer is not in the config store, so the integration would be "
            "registered with a credential the function does not accept. Press this step "
            "again; the deploy mints it.")
    try:
        result = await entitle_registration_service.register_rest(
            name=f"pov-{env.name}-hmi-adapter"[:50],
            base_url=ot_faas_service.base_url(),
            shared_secret=bearer,
            # The load-bearing argument: the POV's own Entitle agent makes the calls, so
            # an in-cluster address is reachable and Entitle needs no route in.
            private=True,
            ephemeral=True,
            auth_header="Authorization",
            ctx=tenant["ctx"])
    except Exception as exc:  # noqa: BLE001
        raise OTAdapterError(
            f"the adapter is queued, but registering it in Entitle tenant "
            f"{tenant['label']!r} failed: {exc}. Press this step again once the cause is "
            f"fixed; the deploy is idempotent.") from None

    # The state before returning: an integration that exists in a customer's tenant with
    # no state here is one this dashboard cannot remove.
    if result.get("tf_state_json"):
        config_service.set(state_config_key(env.id), result["tf_state_json"])
    _set_meta(db, env, integration_id=str(result.get("integration_id") or ""))
    return (f"Registered in Entitle tenant {tenant['label']} as integration "
            f"{_meta(env, 'integration_id') or '(unnamed)'}, brokered by this POV's agent.")


# ── teardown ─────────────────────────────────────────────────────────────────

async def teardown(db: Session, env: PovEnvironment) -> str:
    """Deregister the adapter and clear its credentials. Returns a line, never raises.

    Must run BEFORE ``pov_entitle_agent.teardown``: the registration names the agent,
    and the context built here reads the agent's name off the row, which that teardown
    clears. Nothing is uninstalled on the guest; the environment delete takes it.

    On a failed deregister the state and the bearer are KEPT, because the state is the
    only handle left on an integration in the customer's tenant.
    """
    from . import pov_wireup

    state = config_service.get(state_config_key(env.id))
    if not state:
        ot_faas_service.clear_stash(env.id)
        if any(_meta(env, k) is not None for k in _META):
            _set_meta(db, env, **{k: None for k in _META})
        return ""
    try:
        tenant = await pov_wireup.entitle_tenant_ctx(db, env)
        if not tenant:
            raise OTAdapterError("this POV no longer names an Entitle tenant")
        await entitle_registration_service.deregister(state, ctx=tenant["ctx"])
    except Exception as exc:  # noqa: BLE001 — teardown reports, never blocks
        logger.warning("POV %s: removing the HMI adapter integration failed", env.id,
                       exc_info=True)
        return (f"Could not remove the HMI adapter's Entitle integration "
                f"{_meta(env, 'integration_id') or '(unnamed)'} ({exc}) — delete it in "
                f"the Entitle tenant by hand.")
    config_service.delete(state_config_key(env.id))
    ot_faas_service.clear_stash(env.id)
    integration = _meta(env, "integration_id") or "(unnamed)"
    _set_meta(db, env, **{k: None for k in _META})
    return f"Removed the HMI adapter's Entitle integration {integration}."


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(env: PovEnvironment) -> dict:
    """The adapter's recorded state for one POV row. No queries, no network calls."""
    return {
        "ot_adapter_registered": bool(_meta(env, "integration_id")),
        "ot_adapter_integration_id": _meta(env, "integration_id") or "",
        "ot_adapter_deploy_job_id": _meta(env, "deploy_job_id") or "",
        "ot_adapter_rotate_job_id": _meta(env, "rotate_job_id") or "",
        # True until a deploy says otherwise: the adapter's own default.
        "ot_adapter_dry_run": (True if _meta(env, "dry_run") is None
                               else bool(_meta(env, "dry_run"))),
        "ot_adapter_hmi_vm": _meta(env, "hmi_vm") or "",
    }
