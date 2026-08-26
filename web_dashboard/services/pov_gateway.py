"""The POV's BeyondTrust Gateway: a Gateway node running inside the customer's environment.

Slice 5. Slice 3 put an agent on a broker VM inside the POV; slice 4 recorded which
customer's PRA appliance the POV belongs to. This is the first slice that uses both at
once — it asks that agent to start a Gateway container beside itself, registered into that
tenant's appliance. Every PRA jump item a later slice creates has to name a Gateway, and
this is the one that can see the POV's private network.

**What the dashboard does and does not own here.** It does not create the Gateway in PRA.
An operator creates it in the appliance and copies its deploy key — the same thing they do
for the cloud gateway hosts, where the key sits in ``aws_ecs_docker_deploy_key`` and
friends. What this adds is per-POV: a key per environment, installed on a VM the dashboard
cannot reach directly, into a tenant it had to be told about.

Three properties are worth stating before the code:

**The deploy key never travels as metadata.** Unlike an enrolment code it is neither
single-use nor short-lived — every node registered with it joins the same Gateway, which
is exactly what makes the cloud hosts a cluster. So it does not go in the job row, it does
not go in the signed envelope, and above all it does not go through the lab platform's
``user_data``, where anyone with read access to the environment can see it. It is stored
encrypted and fetched once per job over the agent's sealed channel.

**The image comes from the agent's policy, not from here.** Same rule as the
Config-Management and hypervisor sibling runners: a job says only what kind of thing to
do. On a POV the dashboard also *writes* that policy, which does not make the rule
pointless — it keeps this code path identical to the one a customer-owned agent uses, so
there is one shape to reason about rather than two.

**"Is the Gateway there?" is the wrong question.** A Gateway is a cluster. Re-installing
on a rebuilt broker VM adds a node and PRA parks the dead one, so the name is present
either way. What the status check asks is whether a node of it is *connected*.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, RemoteAgent
from . import (agent_gateway_meta, agent_service, bt_tenant_service, config_service,
               job_service, pra_tenant_api)

logger = logging.getLogger(__name__)


class GatewayInstallError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# The `ref` the deploy key is sealed under. Binds the sealed envelope to *this* secret's
# purpose, so a bundle sealed for a hypervisor credential cannot be opened as a Gateway
# key and vice versa — see agent_sealing.seal_aad.
SEAL_REF = "pov-gateway-deploy-key"

# Where a POV's deploy key lives. Same `<feature>/<id>/<name>` shape as
# `clouddb/<id>/admin`, so it is Fernet-encrypted at rest, resolvable from an external
# vault by reference, and carried by the config-migration tool — all for free.
_KEY_FMT = "pov/{env_id}/gateway_deploy_key"

# The container the agent runs it as. Generated, never supplied: there is no field in this
# protocol through which an operator string could name something on the broker VM.
CONTAINER_NAME = "pov-gateway"


def deploy_key_config_key(env_id: str) -> str:
    return _KEY_FMT.format(env_id=env_id)


def set_deploy_key(env: PovEnvironment, key: str) -> None:
    """Store a POV's deploy key, encrypted.

    A blank key **clears** it, which is deliberate and the opposite of the "blank keeps"
    rule the Settings panels use. There the form cannot render what is stored so blank has
    to mean keep; here clearing is the only way to revoke a key an operator pasted by
    mistake, and the POV page shows whether one is stored rather than pretending to show
    the value.
    """
    config_service.set(deploy_key_config_key(env.id), (key or "").strip())


def has_deploy_key(env: PovEnvironment) -> bool:
    return bool(config_service.get(deploy_key_config_key(env.id)))


def clear_deploy_key(env: PovEnvironment) -> None:
    try:
        config_service.delete(deploy_key_config_key(env.id))
    except Exception:  # noqa: BLE001 — teardown must not fail on a key that is already gone
        logger.debug("POV %s: no deploy key to clear", env.id)


def deploy_key_for_job(db: Session, job: Job) -> str:
    """The deploy key for an ``agent_gateway`` job, derived from the job row.

    The one reader the sealed endpoint calls. Derived from the row and never from the
    request body, which is the property that matters: without it a stolen agent identity
    could ask for another POV's key against a job it legitimately owns.
    """
    env_id = str((job.metadata_dict or {}).get("environment_id") or "")
    if not env_id:
        raise GatewayInstallError("this job names no POV environment")
    env = db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()
    if env is None:
        raise GatewayInstallError("the POV environment this job belongs to is gone")
    key = config_service.get(deploy_key_config_key(env.id))
    if not key:
        raise GatewayInstallError(
            "this POV has no Gateway deploy key stored. Add it on the POV page — it comes "
            "from the Gateway you created in the customer's PRA appliance.")
    return key


# ── preflight ────────────────────────────────────────────────────────────────

def broker_agent(db: Session, env: PovEnvironment) -> RemoteAgent:
    """The enrolled agent that will run the Gateway, or a refusal naming the remedy.

    Every failure here has a different fix, so they are different messages: no broker at
    all is the Broker button, an offline one is the VM, and a revoked one is a re-broker.
    Collapsing them into "the broker is not available" would leave an operator guessing
    between three.
    """
    if not env.broker_agent_id:
        raise GatewayInstallError(
            "this POV has no broker agent, so there is nothing inside the environment to "
            "run a Gateway on. Press Broker on the POV first.")
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == env.broker_agent_id).first()
    if agent is None:
        raise GatewayInstallError(
            "this POV's broker agent row is gone. Press Broker to enrol a new one.")
    status = agent_service.status_of(agent)
    if status == "revoked":
        raise GatewayInstallError(
            f"the broker agent {agent.name!r} has been revoked. Press Broker to re-enrol "
            f"it.")
    if status == "enrolling":
        raise GatewayInstallError(
            f"the broker agent {agent.name!r} has never enrolled. Nothing executes the "
            f"bootstrap for you — check the broker VM has the metadata runner, then press "
            f"Broker to re-issue the code.")
    if status == "offline":
        raise GatewayInstallError(
            f"the broker agent {agent.name!r} is offline, so it cannot lease this job. "
            f"Check the broker VM is powered on and the container is running.")
    return agent


def pra_tenant(db: Session, env: PovEnvironment):
    """The PRA tenant this POV is wired into.

    Goes through ``bt_tenant_service.resolve`` with the POV's explicit id, so a POV whose
    tenant was deleted or disabled is an **error** rather than a quiet fall back to the
    default — which would install a customer's Gateway into somebody else's appliance.
    """
    try:
        return bt_tenant_service.resolve(db, "pra", env.pra_tenant_id or None)
    except bt_tenant_service.BTTenantError as exc:
        raise GatewayInstallError(
            f"this POV's PRA tenant could not be resolved: {exc}") from None


def preflight(db: Session, env: PovEnvironment) -> tuple[RemoteAgent, object]:
    """Everything checkable before a job row exists, checked here.

    The order is deliberate: the cheapest, most-likely-wrong thing first. An SE who has
    not pressed Broker yet should be told that, not told about a deploy key they were
    about to be asked for anyway.
    """
    agent = broker_agent(db, env)

    if not agent_service.supports_gateway(agent):
        raise GatewayInstallError(agent_service.gateway_upgrade_hint(agent))
    reported = agent.reported_job_types_list
    if reported and "agent_gateway" not in reported:
        # Covers what a version number cannot: a current agent whose policy.yaml predates
        # the grant. On a POV that file is generated, so the fix is a button rather than
        # an editor — which is worth saying, because the generic advice is "edit
        # policy.yaml" and here that would mean SSH into a customer's environment.
        raise GatewayInstallError(
            f"the broker agent {agent.name!r} reports it may run "
            f"{', '.join(reported) or 'nothing'} — its policy.yaml predates the Gateway "
            f"grant. Press Broker on this POV to rewrite it, then try again.")
    if "agent_gateway" not in agent_service.allowed_job_types(agent):
        raise GatewayInstallError(
            f"this dashboard does not permit {agent.name!r} to run Gateway jobs. Widen "
            f"its allowed job types on the Agents page.")

    tenant = pra_tenant(db, env)
    if not has_deploy_key(env):
        raise GatewayInstallError(
            "this POV has no Gateway deploy key stored. Create a Gateway in "
            f"{tenant.name!r} ({tenant.base_url}), copy its deploy key, and paste it on "
            f"this POV.")
    if not (env.gateway_name or "").strip():
        raise GatewayInstallError(
            "this POV names no Gateway. Set the name you gave it in PRA — it is what the "
            "status check and every later jump item look it up by.")
    return agent, tenant


# ── the job ──────────────────────────────────────────────────────────────────

def queue(db: Session, env: PovEnvironment, *, action: str = "install",
          created_by: str = "") -> Job:
    """Queue an ``agent_gateway`` job on this POV's broker agent.

    No local-worker job wrapping it. The work happens entirely on the far side of the
    agent channel, and a local job whose only content is "wait for another job" is a
    second row to reconcile, a second thing to cancel, and a second place for the two to
    disagree about what happened.
    """
    action = (action or "install").strip().lower()
    if action not in agent_gateway_meta.VALID_ACTIONS:
        raise GatewayInstallError(
            f"{action!r} is not a Gateway action (known: "
            f"{', '.join(agent_gateway_meta.VALID_ACTIONS)})")

    if action == "install":
        agent, _tenant = preflight(db, env)
    else:
        # A removal needs the agent and nothing else. Requiring a resolvable tenant or a
        # stored key to take a container away would make a half-configured POV
        # un-teardownable, which is the state that most needs tearing down.
        agent = broker_agent(db, env)

    meta = agent_gateway_meta.gateway_meta(
        {"gateway_action": action},
        description=f"{action.title()} the BeyondTrust Gateway for POV {env.name}")
    # environment_id is on the job ROW, not in the envelope: it is how the sealed-key
    # endpoint derives which POV's key this job may have, and the agent has no use for it.
    meta["environment_id"] = env.id

    job = job_service.create_job(
        db, job_type="agent_gateway", created_by=created_by,
        workgroup=env.workgroup, agent_id=agent.id, metadata=meta)
    logger.info("POV %s: queued a Gateway %s on broker agent %s", env.id, action,
                agent.name)
    return job


# ── status ───────────────────────────────────────────────────────────────────

async def status(db: Session, env: PovEnvironment) -> dict:
    """What PRA says about this POV's Gateway.

    Live rather than stored. A stored status is how a row still reads "connected" three
    weeks after the environment was suspended — the same reason ``RemoteAgent`` has no
    status column.

    ``connected`` is tri-state on purpose. ``None`` means the appliance did not tell us,
    which happens when its version spells the field differently, and reporting that as
    "disconnected" would send an operator to debug a Gateway that is fine.
    """
    name = (env.gateway_name or "").strip()
    if not name:
        return {"state": "none", "detail": "No Gateway is named on this POV."}
    try:
        tenant = pra_tenant(db, env)
    except GatewayInstallError as exc:
        return {"state": "unknown", "detail": str(exc)}
    try:
        found = await pra_tenant_api.find_gateway(tenant, name)
    except pra_tenant_api.PRATenantError as exc:
        return {"state": "unknown", "detail": str(exc)}

    if found is None:
        return {"state": "missing",
                "detail": (f"PRA tenant {tenant.name!r} has no Gateway named {name!r}. "
                           f"Create it in the appliance, or correct the name here.")}
    if found["connected"] is False:
        return {"state": "disconnected", "nodes": found["nodes"],
                "detail": (f"{name!r} exists in PRA but no node is connected. If the "
                           f"broker VM was rebuilt, PRA keeps the dead node — install "
                           f"again and compare node counts.")}
    if found["connected"] is None:
        return {"state": "unknown", "nodes": found["nodes"],
                "detail": (f"PRA lists {name!r} but did not report whether it is "
                           f"connected. Check the appliance directly.")}
    return {"state": "connected", "nodes": found["nodes"],
            "detail": f"{name!r} is connected in PRA tenant {tenant.name!r}."}


# ── teardown ─────────────────────────────────────────────────────────────────

def teardown(db: Session, env: PovEnvironment) -> str:
    """Queue the Gateway removal, if there is anything to remove. Returns a job-log line.

    Queues rather than waits. ``run_env_destroy`` deletes the whole environment moments
    later, which takes the container with it — so blocking the teardown on an agent that
    may already be gone would trade a tidy removal for a POV that cannot be destroyed.
    The removal is best-effort tidiness; the environment delete is the reaping.

    The stored deploy key IS cleared synchronously, because that is local state and
    leaving a customer's credential behind after their POV is gone is the part that
    matters.
    """
    lines = []
    if env.gateway_name and env.broker_agent_id:
        try:
            job = queue(db, env, action="remove", created_by="teardown")
            lines.append(f"Queued removal of Gateway {env.gateway_name!r} "
                         f"(job {job.id}).")
        except GatewayInstallError as exc:
            # Not a failure of the teardown. The environment is about to be deleted and
            # the container with it; this line exists so the operator knows the PRA-side
            # node will linger and can retire it by hand.
            lines.append(f"Could not queue the Gateway removal ({exc}) — the environment "
                         f"delete will take the container, but PRA keeps the node.")

    if has_deploy_key(env):
        clear_deploy_key(env)
        lines.append("Cleared the stored Gateway deploy key.")

    env.gateway_name = None
    db.commit()
    return " ".join(lines) or "No Gateway to remove."


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """The Gateway's configured state for one POV row — no network calls.

    Deliberately does not ask PRA. This runs once per row on the POV list, and a live
    appliance read per row would make the page as slow as the slowest customer's network.
    The live answer is :func:`status`, behind a button.
    """
    return {
        "gateway_name": env.gateway_name or "",
        "gateway_has_key": has_deploy_key(env),
        "gateway_ready": bool(env.gateway_name and has_deploy_key(env)
                              and env.broker_agent_id),
    }
