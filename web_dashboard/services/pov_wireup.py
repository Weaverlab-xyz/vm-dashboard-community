"""Wiring a POV's VMs into PRA and Password Safe: one jump item and one managed system
per VM.

Slices 6a and 6b. Everything before this made a POV *reachable* — an environment (slice 2), an
agent inside it (slice 3), a tenant to belong to (slice 4), a Gateway to route through
(slice 5). This is the first slice that puts something in front of a **user**: a jump item
per VM, in the customer's own appliance, through that POV's own Gateway.

Four things decide the shape, and three of them are consequences of earlier slices:

**The POV's own Gateway, never the tenant's default.** A POV's jump items must route
through the Gateway installed *inside that environment* — ``PovEnvironment.gateway_name``
— and not through the appliance-wide one. Falling back to that default would build jump items
that authenticate perfectly and cannot reach anything, because no Gateway on the customer's
side has a route into this POV's private network. So a POV with no Gateway is refused.

**The tenant is never the singleton.** ``terraform_pra_service`` reads ``bt_api_host`` and
friends by default, which on a POV instance is the wrong appliance for every POV. Slice 4's
registry answers it, and this is its second consumer — the credentials are threaded through
as a per-call override, and a *partial* override is refused rather than merged.

**The protocol follows the guest.** A Shell Jump is SSH-only, so a Windows VM gets a Remote
RDP jump instead. ``os_family`` is blank when the platform did not say, and slice 3 was
deliberate that blank means *unknown* — so a blank one is skipped with a reason rather than
guessed at. A confident wrong answer here builds an SSH jump to a Windows box, which fails
at session launch in front of whoever clicked it.

**One VM's failure is not the run's failure.** Each artifact is persisted the moment it
exists, and a VM that could not be wired records its reason in ``wiring_error`` and the run
continues. A POV where seven of eight VMs are reachable is worth more than one that rolled
back to zero because the eighth had no address yet.

**Password Safe reaches the VM through the Resource Broker, not directly.** Slice 5b
installed one inside the environment and recorded it as
``PovEnvironment.ps_application_host_id``; every managed system this creates names it as
its ``application_host_id``, which is the field that tells Password Safe to manage the
host *via* that broker. Without it the platform would try to reach a private address from
the cloud tenant and fail on every rotation — a failure that surfaces days later, on a
schedule, rather than at onboarding.

The Password Safe half is **optional and independent**. A POV wired into PRA is already
useful, and a tenant that has not been given a workgroup or a functional account is a
reason to skip that half with a message, never a reason to fail the jump item that
already worked. The Entitle half is optional on the same terms.

**Entitle needs something the dashboard does not install.** Its SSH ephemeral-accounts
integration reaches a *private* target through an Entitle agent — a Kubernetes deployment
inside the customer's network, named by ``agent_token_name`` on the tenant. This dashboard
installs a Gateway (slice 5) and a Resource Broker (slice 5b) but not that, so a POV whose
Entitle tenant names no agent is skipped with the reason rather than registered against
the install's own tenant. The service raises the same refusal from its own side; this just
gets there first, with a message an SE can act on.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM
from . import (bt_tenant_service, config_service, entitle_registration_service,
               job_service, pov_gateway, ps_api_service, ps_resource_service,
               terraform_pra_service)

logger = logging.getLogger(__name__)


class WireupError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# The tag every POV jump item carries in PRA. One value, so an SE can find or filter this
# dashboard's items in an appliance that also holds hand-built ones — and so the reaping
# story is legible from the PRA side as well as this one.
JUMP_TAG = "POV"

# The ports a jump item is created against, by guest. Not configurable: these are the
# protocols the jump TYPES speak, not a preference — a Shell Jump is SSH and a Remote RDP
# jump is RDP, and a port field that disagreed with its jump type would be a way to build
# an item that cannot work.
SSH_PORT = 22
RDP_PORT = 3389


# ── the tenant ───────────────────────────────────────────────────────────────

def tenant_override(db: Session, env: PovEnvironment) -> dict:
    """The three PRA credentials this POV's jump items are created against.

    Resolved through ``bt_tenant_service`` with the POV's explicit id, so a POV whose
    tenant was deleted or disabled is an error rather than a quiet fall back to the
    default — which here would mean creating a customer's jump items in somebody else's
    appliance.
    """
    tenant = pov_gateway.pra_tenant(db, env)   # same resolution, same refusals
    if not tenant.client_id or not tenant.secret:
        raise WireupError(
            f"the PRA tenant {tenant.name!r} has no OAuth client id and secret, so "
            f"terraform cannot authenticate to it.")
    jump_group = tenant.option("jump_group_name")
    if not jump_group:
        raise WireupError(
            f"the PRA tenant {tenant.name!r} names no Jump Group. Jump items have to be "
            f"created in one — set it on the tenant.")
    return {
        "env": terraform_pra_service.tenant_env(
            tenant.base_url, tenant.client_id, tenant.secret),
        "jump_group_name": jump_group,
        "label": tenant.name,
    }


def gateway_name(env: PovEnvironment) -> str:
    """The Gateway a POV's jump items route through.

    **Never the tenant's appliance-wide default.** That one lives on the customer's side
    of the world and has no route into this POV's private network, so a jump item pointed at it
    is created successfully, looks correct in the appliance, and times out at session
    launch — the failure that costs the most to diagnose because nothing reports it until
    a person clicks.
    """
    name = (env.gateway_name or "").strip()
    if not name:
        raise WireupError(
            "this POV has no Gateway, so its jump items would have nothing to route "
            "through. Install one from the Gateway column first — the tenant's "
            "appliance-wide Gateway cannot see inside this environment.")
    return name


# ── the Password Safe tenant ─────────────────────────────────────────────────

async def ps_context(db: Session, env: PovEnvironment) -> dict:
    """Everything the Password Safe half needs, or a refusal naming what is missing.

    Resolved once per run rather than per VM: a missing workgroup is not something the
    second VM will do better at, and the functional-account lookups are two REST round
    trips to a customer's tenant that should not be repeated eight times.

    Returns ``{}`` — not an error — when the POV has no Password Safe tenant at all. The
    two halves of this wire-up are independent, and a POV wired into PRA alone is a
    perfectly good POV.
    """
    if not env.ps_tenant_id:
        return {}
    try:
        tenant = bt_tenant_service.resolve(db, "password_safe", env.ps_tenant_id)
    except bt_tenant_service.BTTenantError as exc:
        raise WireupError(
            f"this POV's Password Safe tenant could not be resolved: {exc}") from None

    run_as = tenant.option("api_account_name")
    workgroup = tenant.option("workgroup")
    if not run_as:
        raise WireupError(
            f"the Password Safe tenant {tenant.name!r} names no run-as user, which the "
            f"passwordsafe Terraform provider requires. Set it on the tenant.")
    if not workgroup:
        raise WireupError(
            f"the Password Safe tenant {tenant.name!r} names no workgroup, so there is "
            f"nowhere for a managed system to land. Set it on the tenant.")
    if not env.ps_application_host_id:
        # The Resource Broker IS how Password Safe reaches a private address. Without it
        # the platform tries from the cloud tenant and fails on every rotation — days
        # later, on a schedule, rather than here.
        raise WireupError(
            "this POV has no Password Safe Resource Broker, so the platform would have no "
            "route to these VMs. Install one from the Resource Broker column first.")

    api = ps_api_service.tenant_creds(tenant.base_url, tenant.client_id, tenant.secret)
    tf = ps_resource_service.tenant_creds(
        tenant.base_url, tenant.client_id, tenant.secret, run_as)
    try:
        workgroup_id = await ps_api_service.get_workgroup_id(workgroup, api)
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: resolving the Password Safe workgroup failed", env.id,
                       exc_info=True)
        raise WireupError(
            f"could not resolve workgroup {workgroup!r} in Password Safe tenant "
            f"{tenant.name!r} ({type(exc).__name__}) — check the name and the API "
            f"account's permissions.") from None

    # Both are optional here and refused per VM instead: a POV with only Linux guests has
    # no use for a Windows functional account, and demanding one would block a wire-up
    # that could have completed.
    accounts = {}
    for family, option in (("linux", "linux_functional_account"),
                           ("windows", "windows_functional_account")):
        name = tenant.option(option)
        if not name:
            continue
        try:
            accounts[family] = await ps_api_service.get_functional_account(name, api)
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: functional account %r lookup failed", env.id, name,
                           exc_info=True)
            raise WireupError(
                f"could not resolve the {family} functional account {name!r} in Password "
                f"Safe tenant {tenant.name!r} ({type(exc).__name__}).") from None

    return {"tf": tf, "workgroup_id": workgroup_id, "accounts": accounts,
            "application_host_id": int(env.ps_application_host_id),
            "label": tenant.name}


# ── the Entitle tenant ───────────────────────────────────────────────────────

# Where a POV's Entitle SSH key lives. Per-POV rather than per-tenant because it is the
# key baked into THAT template's guests — the same storage shape the Gateway deploy key
# and the Resource Broker installer key use.
_ENTITLE_KEY_FMT = "pov/{env_id}/entitle_ssh_key"


def entitle_key_config_key(env_id: str) -> str:
    return _ENTITLE_KEY_FMT.format(env_id=env_id)


def set_entitle_key(env: PovEnvironment, pem: str) -> None:
    """Store a POV's Entitle SSH private key, encrypted. Blank clears it."""
    config_service.set(entitle_key_config_key(env.id), (pem or "").strip())


def has_entitle_key(env: PovEnvironment) -> bool:
    return bool(config_service.get(entitle_key_config_key(env.id)))


def clear_entitle_key(env: PovEnvironment) -> None:
    try:
        config_service.delete(entitle_key_config_key(env.id))
    except Exception:  # noqa: BLE001 — teardown must survive a key already gone
        logger.debug("POV %s: no Entitle SSH key to clear", env.id)


async def entitle_context(db: Session, env: PovEnvironment) -> dict:
    """Everything the Entitle half needs, or a refusal naming what is missing.

    ``{}`` — not an error — when the POV has no Entitle tenant. The three halves of this
    wire-up are independent, and a POV without Entitle is a perfectly good POV.
    """
    if not env.entitle_tenant_id:
        return {}
    try:
        tenant = bt_tenant_service.resolve(db, "entitle", env.entitle_tenant_id)
    except bt_tenant_service.BTTenantError as exc:
        raise WireupError(
            f"this POV's Entitle tenant could not be resolved: {exc}") from None

    missing = [label for label, value in (
        ("an owner id", tenant.option("owner_id")),
        ("a workflow id", tenant.option("workflow_id")),
        ("an SSH sudo user", tenant.option("ssh_sudo_user")),
    ) if not value]
    if missing:
        raise WireupError(
            f"the Entitle tenant {tenant.name!r} names {' and '.join(missing)}, which an "
            f"integration cannot be created without. Set them on the tenant.")

    agent = tenant.option("agent_token_name")
    if not agent:
        # The one prerequisite this dashboard cannot satisfy. Said here, in front of the
        # operator, rather than letting the terraform apply fail with the provider's
        # version of it half a minute later.
        raise WireupError(
            f"the Entitle tenant {tenant.name!r} names no agent token. A POV's VMs are on "
            f"a private network, and Entitle reaches a private target through an agent "
            f"running inside it — which this dashboard does not install. Deploy the "
            f"Entitle agent in the POV and name its token on the tenant.")

    pem = config_service.get(entitle_key_config_key(env.id))
    if not pem:
        raise WireupError(
            "this POV has no Entitle SSH key. Entitle's ephemeral-accounts integration "
            "authenticates with a key, not a password — add the private half of the key "
            "baked into this template's guests.")

    return {
        "ctx": entitle_registration_service.tenant_ctx(
            api_key=tenant.secret, endpoint=tenant.base_url,
            owner_id=tenant.option("owner_id"),
            workflow_id=tenant.option("workflow_id"),
            agent_token_name=agent, ssh_sudo_user=tenant.option("ssh_sudo_user")),
        "sudo_user": tenant.option("ssh_sudo_user"),
        "private_key": pem,
        "label": tenant.name,
    }


# ── per-VM ───────────────────────────────────────────────────────────────────

def wireable(vm: PovEnvironmentVM) -> str:
    """Why this VM cannot be wired, or "" when it can."""
    if not (vm.private_ip or "").strip():
        return ("no private address — the platform reports one once the VM is running, "
                "so power the environment on and refresh")
    family = (vm.os_family or "").strip().lower()
    if family not in ("linux", "windows"):
        # Blank means the platform did not say. Slice 3 refused to guess for exactly this
        # reason: an SSH jump to a Windows box fails at session launch, in front of
        # whoever clicked it.
        return ("the platform did not report an OS, and guessing would build an SSH jump "
                "to a Windows box or the reverse")
    return ""


def _record(db: Session, vm: PovEnvironmentVM, *, jump_id: str = "", state: str = "",
            vault_id: str = "", error: str = "", ps_system_id: str = "",
            ps_account_id: str = "", ps_state: str = "",
            entitle_id: str = "", entitle_state: str = "") -> None:
    """Persist one VM's outcome immediately.

    The moment the artifact exists, not at the end of the run. A re-derived id is how you
    delete the wrong thing at teardown, and a run that crashed halfway with nothing written
    leaves items in a customer's appliance that this dashboard no longer knows about.
    """
    if jump_id:
        vm.pra_jump_id = jump_id
    if state:
        vm.pra_jump_tf_state = state
    if vault_id:
        vm.vault_account_id = vault_id
    if ps_system_id:
        vm.ps_managed_system_id = ps_system_id
    if ps_account_id:
        vm.ps_managed_account_id = ps_account_id
    if ps_state:
        vm.ps_registration_tf_state = ps_state
    if entitle_id:
        vm.entitle_integration_id = entitle_id
    if entitle_state:
        vm.entitle_tf_state = entitle_state
    vm.wiring_error = error or None
    db.commit()


async def wire_vm(db: Session, env: PovEnvironment, vm: PovEnvironmentVM, *,
                  tenant: dict, gateway: str) -> str:
    """Create one VM's jump item. Returns a line for the job log.

    Idempotent in the only way that matters: a VM that already has a jump item is skipped
    rather than given a second one. PRA will happily create two items with the same name
    pointing at the same host, and the second is invisible in this database — so the check
    is here rather than left to the appliance.
    """
    if vm.pra_jump_id:
        return f"{vm.name}: already wired (jump {vm.pra_jump_id})."

    problem = wireable(vm)
    if problem:
        _record(db, vm, error=problem)
        return f"{vm.name}: skipped — {problem}."

    label = f"{env.name}-{vm.name}"
    windows = (vm.os_family or "").strip().lower() == "windows"

    try:
        if windows:
            # The RDP path creates the Vault account in the SAME workspace, so one state
            # destroys both — which is why `vault_tf_state` stays NULL here rather than
            # holding a copy. A credential is optional: without one the jump item still
            # works, the user just types the password.
            username, password = "", ""
            try:
                from . import pov_resource_broker
                username, password = await pov_resource_broker.platform_login(
                    db, env.id, vm.platform_vm_id)
            except Exception as exc:  # noqa: BLE001
                # Best-effort, and quiet by design: a template with no stored credential
                # is normal, and a jump item without injection is still a working jump
                # item. The reason is logged, never surfaced as a failure.
                logger.info("POV %s: no injectable credential for %s (%s)",
                            env.id, vm.name, type(exc).__name__)
            result = await terraform_pra_service.provision_rdp_jump(
                name=label, hostname=vm.private_ip,
                jump_group_name=tenant["jump_group_name"], jumpoint_name=gateway,
                rdp_username=username, tag=JUMP_TAG,
                admin_password=password,
                vault_account_name=f"{label}-admin" if (username and password) else "",
                tenant=tenant["env"])
            jump_id = str(result.get("rdp_jump_id") or "")
            vault_id = str(result.get("vault_account_id") or "")
        else:
            result = await terraform_pra_service.provision_jump(
                vm_name=label, hostname=vm.private_ip,
                jump_group_name=tenant["jump_group_name"], jumpoint_name=gateway,
                port=SSH_PORT, tag=JUMP_TAG, tenant=tenant["env"])
            jump_id = str(result.get("shell_jump_id") or "")
            vault_id = ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: wiring %s failed", env.id, vm.name, exc_info=True)
        # Terraform's own text is the useful part here and it is not an exception's
        # `str()` in the CodeQL sense — TerraformPRAError carries the provider's message,
        # which is what names a missing Jump Group or a permission the OAuth client lacks.
        _record(db, vm, error=str(exc)[:2000])
        return f"{vm.name}: FAILED — {exc}"

    state = result.get("tf_state_json") or ""
    if not state:
        # The item exists in PRA and this database cannot destroy it. Loud, because it is
        # the one outcome that leaves an orphan nobody can reap from here.
        logger.error("POV %s: %s was wired but terraform returned no state", env.id, vm.name)
        _record(db, vm, jump_id=jump_id, vault_id=vault_id,
                error="the jump item was created but terraform returned no state, so this "
                      "dashboard cannot destroy it — remove it in PRA by hand at teardown")
        return f"{vm.name}: wired (jump {jump_id}) but NO STATE — see the row."

    _record(db, vm, jump_id=jump_id, state=state, vault_id=vault_id)
    kind = "RDP" if windows else "SSH"
    extra = f", vault account {vault_id}" if vault_id else ""
    return f"{vm.name}: {kind} jump {jump_id} created{extra}."


async def onboard_vm(db: Session, env: PovEnvironment, vm: PovEnvironmentVM, *,
                     ps: dict) -> str:
    """Onboard one VM as a Password Safe managed system + account. Returns a log line.

    Independent of the jump item: a VM whose jump failed can still be onboarded, and a
    Password Safe tenant that is half-configured skips this half without touching the
    other. Same idempotence rule — a VM already onboarded is not onboarded twice, because
    Password Safe will accept a second managed system on the same address and the second
    is invisible here.
    """
    if vm.ps_managed_system_id:
        return f"{vm.name}: already in Password Safe (system {vm.ps_managed_system_id})."

    family = (vm.os_family or "").strip().lower()
    account = (ps.get("accounts") or {}).get(family)
    if account is None:
        return (f"{vm.name}: skipped Password Safe — the tenant names no {family} "
                f"functional account, and the managed system's platform is derived from "
                f"one.")

    # The platform login, when the lab platform has one. Seeding it means the first
    # rotation replaces a password somebody knows rather than one nobody does; without it
    # the account is onboarded and the first rotation mints the credential.
    username, password = "", ""
    try:
        from . import pov_resource_broker
        username, password = await pov_resource_broker.platform_login(
            db, env.id, vm.platform_vm_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("POV %s: no seedable credential for %s (%s)", env.id, vm.name,
                    type(exc).__name__)

    label = f"{env.name}-{vm.name}"
    try:
        result = await ps_resource_service.register_managed_system(
            name=label,
            host_name=vm.private_ip,
            ip_address=vm.private_ip,
            port=RDP_PORT if family == "windows" else SSH_PORT,
            functional_account_id=account["id"],
            # From the functional account, never chosen here — the rule ps_vm_hook
            # already follows, and the reason the accounts are split by guest OS.
            platform_id=account["platform_id"],
            workgroup_id=ps["workgroup_id"],
            managed_account_name=username or "adminuser",
            initial_password=password,
            # The Resource Broker. This is the field that tells Password Safe to manage
            # the host THROUGH it rather than reaching a private address from the cloud.
            application_host_id=ps["application_host_id"],
            method="ssh",
            tenant=ps["tf"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: onboarding %s to Password Safe failed", env.id, vm.name,
                       exc_info=True)
        _record(db, vm, error=f"Password Safe: {exc}"[:2000])
        return f"{vm.name}: Password Safe FAILED — {exc}"

    system_id = str(result.get("managed_system_id") or "")
    account_id = str(result.get("managed_account_id") or "")
    state = result.get("tf_state_json") or ""
    if not state:
        logger.error("POV %s: %s was onboarded but terraform returned no state",
                     env.id, vm.name)
        _record(db, vm, ps_system_id=system_id, ps_account_id=account_id,
                error="the managed system was created but terraform returned no state, so "
                      "this dashboard cannot off-board it — remove it in Password Safe by "
                      "hand at teardown")
        return f"{vm.name}: onboarded (system {system_id}) but NO STATE — see the row."

    _record(db, vm, ps_system_id=system_id, ps_account_id=account_id, ps_state=state)
    seeded = " (credential seeded)" if result.get("initial_password_seeded") else ""
    return f"{vm.name}: Password Safe system {system_id}, account {account_id}{seeded}."


async def register_vm_entitle(db: Session, env: PovEnvironment, vm: PovEnvironmentVM, *,
                              ent: dict) -> str:
    """Register one VM as an Entitle SSH ephemeral-accounts integration.

    Linux only, and that is the app rather than a limitation of this code: the SSH
    ephemeral-accounts connector mints a short-lived account over SSH, which a Windows
    guest does not answer. A Windows VM is skipped with that reason rather than registered
    into an integration that could never grant anything.
    """
    if vm.entitle_integration_id:
        return f"{vm.name}: already in Entitle ({vm.entitle_integration_id})."

    if (vm.os_family or "").strip().lower() != "linux":
        return (f"{vm.name}: skipped Entitle — the SSH ephemeral-accounts app mints "
                f"accounts over SSH, which this guest does not answer.")

    label = f"{env.name}-{vm.name}"
    try:
        result = await entitle_registration_service.register_ssh_host(
            name=label, hostname=vm.private_ip, sudo_user=ent["sudo_user"],
            private_key=ent["private_key"], port=SSH_PORT,
            # A POV's VMs are on a private network by construction, so the integration is
            # always private and always needs the agent — which `entitle_context` has
            # already proven the tenant names.
            private=True, ctx=ent["ctx"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: registering %s in Entitle failed", env.id, vm.name,
                       exc_info=True)
        _record(db, vm, error=f"Entitle: {exc}"[:2000])
        return f"{vm.name}: Entitle FAILED — {exc}"

    integration_id = str(result.get("integration_id") or "")
    state = result.get("tf_state_json") or ""
    if not state:
        logger.error("POV %s: %s was registered in Entitle but terraform returned no "
                     "state", env.id, vm.name)
        _record(db, vm, entitle_id=integration_id,
                error="the Entitle integration was created but terraform returned no "
                      "state, so this dashboard cannot remove it — delete it in Entitle "
                      "by hand at teardown")
        return f"{vm.name}: Entitle {integration_id} but NO STATE — see the row."

    _record(db, vm, entitle_id=integration_id, entitle_state=state)
    return f"{vm.name}: Entitle integration {integration_id}."


# ── the job ──────────────────────────────────────────────────────────────────

async def run_env_wireup(job_id: str, meta: dict) -> None:
    """Wire every VM in a POV into its PRA tenant.

    Preflight refuses once, for the whole run: a missing tenant or Gateway is not
    something the second VM will do better at, and thirty identical failures in a job log
    hide the one line that matters.
    """
    from ..database import SessionLocal
    from . import pov_env_service

    db = SessionLocal()
    try:
        env = pov_env_service.get(db, meta.get("environment_id", ""))
        if env is None:
            job_service.set_failed(db, job_id, "the POV environment row is gone")
            return
        try:
            tenant = tenant_override(db, env)
            gateway = gateway_name(env)
        except (WireupError, pov_gateway.GatewayInstallError,
                bt_tenant_service.BTTenantError) as exc:
            job_service.set_failed(db, job_id, str(exc))
            return

        # The Password Safe half is optional and resolved once. A refusal here does NOT
        # fail the run: the PRA half is independently useful, and a POV whose tenant is
        # half-configured should get its jump items rather than nothing.
        ps = {}
        ps_note = ""
        try:
            ps = await ps_context(db, env)
        except (WireupError, bt_tenant_service.BTTenantError) as exc:
            ps_note = str(exc)

        # And the Entitle half, on exactly the same terms.
        ent = {}
        ent_note = ""
        try:
            ent = await entitle_context(db, env)
        except (WireupError, bt_tenant_service.BTTenantError) as exc:
            ent_note = str(exc)

        vms = (db.query(PovEnvironmentVM)
                 .filter(PovEnvironmentVM.environment_id == env.id)
                 .order_by(PovEnvironmentVM.name).all())
        if not vms:
            job_service.set_failed(
                db, job_id,
                "this environment has no VM rows, so there is nothing to wire. Refresh "
                "the POV once the platform reports them.")
            return

        job_service.append_job_log(
            db, job_id,
            f"Wiring {len(vms)} VM(s) into PRA tenant {tenant['label']} through Gateway "
            f"{gateway}.")
        if ps:
            job_service.append_job_log(
                db, job_id,
                f"Onboarding them into Password Safe tenant {ps['label']} through "
                f"Resource Broker {ps['application_host_id']}.")
        elif ps_note:
            job_service.append_job_log(
                db, job_id, f"Skipping the Password Safe half: {ps_note}")
        else:
            job_service.append_job_log(
                db, job_id,
                "This POV names no Password Safe tenant, so no managed systems are "
                "created.")

        if ent:
            job_service.append_job_log(
                db, job_id,
                f"Registering the Linux guests in Entitle tenant {ent['label']}.")
        elif ent_note:
            job_service.append_job_log(
                db, job_id, f"Skipping the Entitle half: {ent_note}")

        wired = skipped = failed = onboarded = registered = 0
        for index, vm in enumerate(vms):
            job_service.update_progress(
                db, job_id, int(5 + 90 * index / max(len(vms), 1)),
                f"Wiring {vm.name}…")
            line = await wire_vm(db, env, vm, tenant=tenant, gateway=gateway)
            job_service.append_job_log(db, job_id, line)
            if "FAILED" in line:
                failed += 1
            elif "skipped" in line:
                skipped += 1
            else:
                wired += 1

            # Independent of the jump item's outcome, and only when the VM is reachable
            # at all — `wireable` is the same precondition both halves need.
            if ps and not wireable(vm):
                ps_line = await onboard_vm(db, env, vm, ps=ps)
                job_service.append_job_log(db, job_id, ps_line)
                if "FAILED" not in ps_line and "skipped" not in ps_line:
                    onboarded += 1

            if ent and not wireable(vm):
                ent_line = await register_vm_entitle(db, env, vm, ent=ent)
                job_service.append_job_log(db, job_id, ent_line)
                if "FAILED" not in ent_line and "skipped" not in ent_line:
                    registered += 1

        summary = {"environment_id": env.id, "wired": wired, "skipped": skipped,
                   "failed": failed, "onboarded": onboarded, "registered": registered}
        if failed and not wired:
            # Nothing worked, so this is a failure rather than a partial success — the
            # distinction matters because a green job with zero artifacts is the one an
            # operator does not go back and read.
            job_service.set_failed(
                db, job_id,
                f"no VM could be wired ({failed} failed). Open the POV to see each row's "
                f"reason.")
            return
        job_service.set_completed(db, job_id, summary)
    finally:
        db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

async def teardown(db: Session, env: PovEnvironment) -> str:
    """Destroy every jump item this POV created. Returns a line for the job log.

    Runs against the SAME tenant the items were created in — a destroy pointed at another
    appliance authenticates fine, deletes nothing, and reports success.

    Best-effort per VM: one item that will not destroy must not stop the other seven, and
    it must not stop the environment delete either. A jump item left in a customer's
    appliance is untidy; a POV environment left running is billing.
    """
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())
    lines = [await _teardown_entitle(db, env, rows),
             await _teardown_password_safe(db, env, rows)]

    if has_entitle_key(env):
        clear_entitle_key(env)
        lines.append("Cleared the stored Entitle SSH key.")

    wired = [r for r in rows if r.pra_jump_tf_state]
    if not wired:
        lines.append("No PRA jump items to remove.")
        return " ".join(l for l in lines if l)

    try:
        tenant = tenant_override(db, env)
    except (WireupError, pov_gateway.GatewayInstallError,
            bt_tenant_service.BTTenantError) as exc:
        lines.append(f"Could not resolve this POV's PRA tenant ({exc}), so {len(wired)} "
                     f"jump item(s) were left in place — remove them by hand.")
        return " ".join(l for l in lines if l)

    removed = problems = 0
    for vm in wired:
        windows = (vm.os_family or "").strip().lower() == "windows"
        try:
            if windows:
                await terraform_pra_service.remove_rdp_jump(vm.pra_jump_tf_state,
                                                            tenant=tenant["env"])
            else:
                await terraform_pra_service.remove_jump(vm.pra_jump_tf_state,
                                                        tenant=tenant["env"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: removing the jump item for %s failed", env.id, vm.name,
                           exc_info=True)
            problems += 1
            continue
        # Cleared only on success. A row that still holds its state is a row a re-run can
        # finish; clearing it optimistically is how an item becomes unreachable.
        vm.pra_jump_id = None
        vm.pra_jump_tf_state = None
        vm.vault_account_id = None
        removed += 1
    db.commit()

    if problems:
        lines.append(f"Removed {removed} PRA jump item(s); {problems} could not be "
                     f"destroyed and are still in tenant {tenant['label']} — remove them "
                     f"by hand.")
    else:
        lines.append(f"Removed {removed} PRA jump item(s) from tenant {tenant['label']}.")
    return " ".join(l for l in lines if l)


async def _teardown_entitle(db: Session, env: PovEnvironment, rows: list) -> str:
    """Remove every Entitle integration this POV created. Returns a line, never raises.

    First of the three, and for the same reason the jump items come before the platform
    delete: an integration is standing *access*, so it is the artifact whose lingering
    matters most.
    """
    registered = [r for r in rows if r.entitle_tf_state]
    if not registered:
        return ""
    try:
        ent = await entitle_context(db, env)
    except (WireupError, bt_tenant_service.BTTenantError) as exc:
        return (f"Could not resolve this POV's Entitle tenant ({exc}), so "
                f"{len(registered)} integration(s) were left in place — delete them by "
                f"hand.")
    if not ent:
        return (f"This POV names no Entitle tenant, so {len(registered)} integration(s) "
                f"could not be removed — delete them by hand.")

    removed = problems = 0
    for vm in registered:
        try:
            await entitle_registration_service.deregister(vm.entitle_tf_state,
                                                          ctx=ent["ctx"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: removing %s from Entitle failed", env.id, vm.name,
                           exc_info=True)
            problems += 1
            continue
        vm.entitle_integration_id = None
        vm.entitle_tf_state = None
        removed += 1
    db.commit()

    if problems:
        return (f"Removed {removed} Entitle integration(s); {problems} could not be and "
                f"are still in tenant {ent['label']} — delete them by hand.")
    return f"Removed {removed} Entitle integration(s) from tenant {ent['label']}."


async def _teardown_password_safe(db: Session, env: PovEnvironment, rows: list) -> str:
    """Off-board every managed system this POV created. Returns a line, never raises.

    Runs before the PRA half and inside the same teardown, but its failures are reported
    rather than propagated: a managed system left in a customer's tenant is untidy, and
    stopping the jump-item removal over it would leave something worse behind.
    """
    onboarded = [r for r in rows if r.ps_registration_tf_state]
    if not onboarded:
        return ""
    try:
        ps = await ps_context(db, env)
    except (WireupError, bt_tenant_service.BTTenantError) as exc:
        return (f"Could not resolve this POV's Password Safe tenant ({exc}), so "
                f"{len(onboarded)} managed system(s) were left in place — remove them by "
                f"hand.")
    if not ps:
        return (f"This POV names no Password Safe tenant, so {len(onboarded)} managed "
                f"system(s) could not be off-boarded — remove them by hand.")

    removed = problems = 0
    for vm in onboarded:
        try:
            await ps_resource_service.deregister(vm.ps_registration_tf_state,
                                                 tenant=ps["tf"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: off-boarding %s from Password Safe failed", env.id,
                           vm.name, exc_info=True)
            problems += 1
            continue
        vm.ps_managed_system_id = None
        vm.ps_managed_account_id = None
        vm.ps_registration_tf_state = None
        removed += 1
    db.commit()

    if problems:
        return (f"Off-boarded {removed} managed system(s); {problems} could not be and "
                f"are still in tenant {ps['label']} — remove them by hand.")
    return f"Off-boarded {removed} managed system(s) from tenant {ps['label']}."


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """The wire-up's state for one POV row — counts, not a per-VM list.

    The list endpoint renders one row per POV, and a per-VM breakdown there would be a
    table inside a table. The detail endpoint already returns the VMs themselves.
    """
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())
    return {
        "vm_count": len(rows),
        "wired_count": sum(1 for r in rows if r.pra_jump_id),
        "onboarded_count": sum(1 for r in rows if r.ps_managed_system_id),
        "entitle_count": sum(1 for r in rows if r.entitle_integration_id),
        "wiring_error_count": sum(1 for r in rows if r.wiring_error),
        "wireup_ready": bool(env.gateway_name and env.pra_tenant_id and rows),
        # Reported separately from `wireup_ready` because the two halves are independent:
        # a POV with no Password Safe tenant still wires into PRA.
        "ps_onboard_ready": bool(env.ps_tenant_id and env.ps_application_host_id and rows),
        # The Entitle half needs a key this dashboard cannot derive, so "ready" here means
        # the operator supplied one — the agent-token check belongs to the tenant and is
        # reported by the run rather than guessed at per row.
        "entitle_ready": bool(env.entitle_tenant_id and has_entitle_key(env) and rows),
        "entitle_has_key": has_entitle_key(env),
    }
