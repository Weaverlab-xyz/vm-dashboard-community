"""Wiring a POV's VMs into its PRA tenant: one jump item per VM.

Slice 6a. Everything before this made a POV *reachable* — an environment (slice 2), an
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
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM
from . import (bt_tenant_service, job_service, pov_gateway,
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
            vault_id: str = "", error: str = "") -> None:
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

        wired = skipped = failed = 0
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

        summary = {"environment_id": env.id, "wired": wired, "skipped": skipped,
                   "failed": failed}
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
    wired = [r for r in rows if r.pra_jump_tf_state]
    if not wired:
        return "No PRA jump items to remove."

    try:
        tenant = tenant_override(db, env)
    except (WireupError, pov_gateway.GatewayInstallError,
            bt_tenant_service.BTTenantError) as exc:
        return (f"Could not resolve this POV's PRA tenant ({exc}), so {len(wired)} jump "
                f"item(s) were left in place — remove them by hand.")

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
        return (f"Removed {removed} PRA jump item(s); {problems} could not be destroyed "
                f"and are still in tenant {tenant['label']} — remove them by hand.")
    return f"Removed {removed} PRA jump item(s) from tenant {tenant['label']}."


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
        "wiring_error_count": sum(1 for r in rows if r.wiring_error),
        "wireup_ready": bool(env.gateway_name and env.pra_tenant_id and rows),
    }
