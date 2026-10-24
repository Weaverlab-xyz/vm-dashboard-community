"""Wiring a POV to its BeyondTrust tenants: the routes under ``/api/pov/managed/{env_id}/``
that point it at tenants and put the PRA/PS/Entitle pieces in place (tenants, broker, Gateway,
resource broker, guest steps, Password Safe links and Smart Rules, the Entitle agent and key,
the OT adapter, Wire up and the Asset Group move).

Registered on ``api/pov.py``'s router, not a router of its own, so the router-level gates
(the ``pov:read`` floor and the per-POV instance gate) cover every route here exactly as they
did when this lived in that file. ``api/pov.py`` imports this module last, after everything
imported from it below is defined.
"""

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import (bt_tenant_service, job_service, lab_platforms, pov_broker,
                        pov_entitle_agent, pov_env_service, pov_gateway, pov_guest_step,
                        pov_ot_adapter, pov_pra_ps_link, pov_ps_config,
                        pov_resource_broker, pov_vendor_access, pov_wireup)
from .auth import get_current_user
from .pov import _POV_WRITE_OWN, _serialize, router


class TenantSelection(BaseModel):
    """Re-point a POV at different BeyondTrust tenants. Absent fields are left alone."""
    pra_tenant_id: str | None = None
    ps_tenant_id: str | None = None
    entitle_tenant_id: str | None = None


@router.post("/managed/{env_id}/tenants", dependencies=_POV_WRITE_OWN)
def set_tenants(env_id: str, payload: TenantSelection,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Choose, or change, the tenants this POV is wired into.

    Exists because a POV is created before its wire-up runs: an SE stands the environment
    up while the customer's PRA appliance is still being provisioned, then comes back and
    points it at one. Blank clears a selection, which is how you undo a mistake made here.

    Deliberately **not** blocked once a wire-up has run — that guard belongs with the code
    that creates the artifacts, because only it knows what re-pointing would orphan. Doing
    it here would be a rule enforced by the one layer that cannot honour it.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to change")
    merged = {
        "pra_tenant_id": fields.get("pra_tenant_id", env.pra_tenant_id) or "",
        "ps_tenant_id": fields.get("ps_tenant_id", env.ps_tenant_id) or "",
        "entitle_tenant_id": fields.get("entitle_tenant_id", env.entitle_tenant_id) or "",
    }
    try:
        chosen = bt_tenant_service.validate_selection(db, **merged)
    except bt_tenant_service.BTTenantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    for column, value in chosen.items():
        setattr(env, column, value or None)
    db.commit()
    return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}


class BrokerRequest(BaseModel):
    """Name the VM that carries the agent, and press Broker.

    ``vm_name`` is ``None`` to leave it alone and ``""`` to clear it back to auto-detect
    (the only Linux VM in the template -- see ``pov_broker.resolve_broker_candidate``).

    Editable after create, unlike before, because the refusal it answers cannot appear
    until the first Broker run: "three VMs could be the broker" and "no VM reports a
    Linux OS" are facts about a provisioned environment, not about the create form. While
    the name was a hard requirement there was nothing to say here, so the only remedy for
    a mistyped or unguessable name was recreating the POV.

    ``install`` follows ``GatewayRequest``: naming and installing are almost always the
    same intent, and a separate save step is a state an operator can leave a POV in that
    looks configured and is not.
    """
    vm_name: str | None = None
    install: bool = True


@router.post("/managed/{env_id}/broker", status_code=202, dependencies=_POV_WRITE_OWN)
def broker(env_id: str, payload: BrokerRequest | None = None,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Install, or re-enrol, the agent inside this POV.

    The provision runs this once and treats a failure as a warning, because a POV with no
    broker is incomplete rather than broken. This is the re-run: it re-issues the
    enrolment code and re-writes the bootstrap, which is the remedy for every reason the
    first attempt can fail — a template with no metadata runner, a VM that was still
    booting, an expired code.

    Refused on a platform that cannot deliver bootstrap data at all, with 409 naming the
    mechanism. That refusal is the point of the capability table: the alternative is a
    button that always 502s.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    # Before the gates below, so a name an operator typed is saved even when the run that
    # follows is refused. The refusal they are answering came FROM a run, so losing the
    # answer to another one is the loop this control exists to break.
    if payload is not None and payload.vm_name is not None:
        pov_broker.configure(db, env, vm_name=payload.vm_name)
    if payload is not None and not payload.install:
        return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}
    if not env.platform_environment_id:
        raise HTTPException(
            status_code=409,
            detail="this environment was never created on the platform, so there is no "
                   "VM to install a control node on.")
    if lab_platforms.capabilities(env.platform).get("bootstrap_injection") != "metadata":
        raise HTTPException(
            status_code=409,
            detail=f"{lab_platforms.capabilities(env.platform)['label']} does not deliver "
                   f"bootstrap data the way this control node install expects; it needs its "
                   f"own control node path.")

    job = job_service.create_job(
        db, job_type="pov_env_broker",
        created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id})
    return {"job_id": job.id}


class GatewayRequest(BaseModel):
    """Configure and install this POV's Gateway.

    ``name`` is the Gateway as it is named **in the customer's PRA appliance** — this is
    the manual path, for a Gateway somebody created there by hand; ``/gateway/create`` is
    the one that makes it. ``deploy_key`` is that Gateway's key; blank leaves
    whatever is stored, so an operator can correct the name without re-pasting a secret
    the form cannot show them.

    ``install`` is what makes this one endpoint rather than two. Setting the name and
    pasting the key is almost always immediately followed by installing, and a separate
    save step is a state an operator can leave a POV in that looks configured and is not.
    """
    name: str = ""
    deploy_key: str = ""
    install: bool = True


@router.get("/managed/{env_id}/gateway")
async def gateway_status(env_id: str, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """What PRA says about this POV's Gateway, live.

    A read against the customer's appliance, so it is behind a button and not on the list
    endpoint — one live call per row would make the POV page as slow as the slowest
    customer's network.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    return {"gateway": await pov_gateway.status(db, env)}


@router.post("/managed/{env_id}/gateway", status_code=202, dependencies=_POV_WRITE_OWN)
def gateway(env_id: str, payload: GatewayRequest,
                  db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Set the Gateway's name and key, and queue its install on the broker agent.

    202 even when only the configuration changed, because the install is the point and
    the job is where its progress lives. A configuration-only call (``install: false``)
    returns the row with no job id.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    if payload.name.strip():
        env.gateway_name = payload.name.strip()
        db.commit()
    # Blank keeps: the form cannot render what is stored, so treating blank as "clear"
    # would wipe the key whenever someone corrected the name.
    if payload.deploy_key.strip():
        pov_gateway.set_deploy_key(env, payload.deploy_key)

    if not payload.install:
        return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}

    try:
        job = pov_gateway.queue(db, env, action="install",
                                created_by=getattr(current_user, "username", None))
    except pov_gateway.GatewayInstallError as exc:
        # 409 with the remedy passed through. Every refusal this raises is something the
        # operator does next — press Broker, pick a tenant, paste a key — so collapsing
        # them into a 500 would lose the only useful part.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job.id,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


class GatewayCreateRequest(BaseModel):
    """Create this POV's Gateway in its PRA tenant, then (by default) install it.

    ``name`` blank means the name already on the POV, else ``POV_<pov name>``. Either way
    it is folded to the characters PRA accepts (letters, digits, space, "_"). The tenant's
    API account does the creating and the deploy key it returns is stored as if pasted,
    so nothing about the install differs from the manual path.
    """
    name: str = ""
    install: bool = True


@router.post("/managed/{env_id}/gateway/create", status_code=202,
             dependencies=_POV_WRITE_OWN)
async def gateway_create(env_id: str, payload: GatewayCreateRequest,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """Create the Gateway in PRA with the tenant's client credentials, store its deploy
    key, and queue the install — the three things an SE did by hand in the appliance.

    Async because it is two or three calls to the customer's appliance; the database work
    either side of them is single-row reads and writes, same as ``gateway_status``.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    try:
        created = await pov_gateway.provision(db, env, name=payload.name)
        job = None
        if payload.install:
            job = pov_gateway.queue(db, env, action="install",
                                    created_by=getattr(current_user, "username", None))
    except pov_gateway.GatewayInstallError as exc:
        # The Gateway may exist in PRA by now (an install refusal comes after the create),
        # which is why the refusal is a 409 carrying the remedy and the row is returned
        # by the next list load rather than rolled back.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job.id if job else None, "gateway": created,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


class ResourceBrokerRequest(BaseModel):
    """Configure and install this POV's Password Safe Resource Broker.

    ``asset`` is the installer's storage key — the customer downloads
    ``BeyondTrust.Agents.Bootstrapper.exe`` from their own Password Safe tenant and uploads
    it on the Config Management page; the dashboard cannot fetch it, because it is
    generated per tenant.

    ``zone`` is the resource zone, and it matters more than its plainness suggests: a
    silent install needs it as well as the key, and without it the installer **prompts** —
    which in an unattended run is not an error but a process that hangs until the timeout.

    ``installer_key`` blank keeps whatever is stored; the form cannot render it.
    """
    vm_name: str | None = None
    zone: str | None = None
    asset: str | None = None
    installer_key: str = ""
    install: bool = True


@router.post("/managed/{env_id}/resource-broker", status_code=202, dependencies=_POV_WRITE_OWN)
def resource_broker(env_id: str, payload: ResourceBrokerRequest,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Set the Resource Broker's staging and queue its install on the broker agent.

    The Windows login is **not** a field here, and that is the point: it comes from the lab
    platform's own stored credentials, read per run. See
    docs/profiles/pov/design/resource-broker.md §5.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    pov_resource_broker.configure(db, env, zone_name=payload.zone,
                                  asset_key=payload.asset, vm_name=payload.vm_name)
    if payload.installer_key.strip():
        pov_resource_broker.set_installer_key(env, payload.installer_key)

    if not payload.install:
        return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}

    try:
        job = pov_resource_broker.queue(
            db, env, created_by=getattr(current_user, "username", None))
    except pov_resource_broker.ResourceBrokerError as exc:
        # 409 with the remedy passed through — every refusal this raises is something the
        # operator does next: stage an installer, name a zone, press Broker.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job.id,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


class GuestStepRequest(BaseModel):
    """Open a POV's guests up for configuration, and optionally run one step now.

    ``vm_names`` is the opt-in list, and it is what widens the broker agent's
    ``policy.yaml`` grant on the next Broker run. ``None`` leaves it alone, ``[]`` clears
    it. A POV that has named none behaves exactly as it did before this existed — the grant
    widens because somebody asked for a specific guest, never because a POV exists.

    ``asset`` is a storage key: a ``.ps1`` for a Windows guest, a ``.sh`` for a Linux one,
    a playbook, or a Windows installer. Upload it on the Storage page first.

    ``arguments`` is only meaningful for an ``.exe``/``.msi`` — it is the only play shape
    that templates a variable for them — and is refused with that reason for anything else,
    rather than accepted and silently dropped.

    There is no login field, for the same reason the Resource Broker install has none: the
    guest credential comes from the lab platform's own stored credentials, read per run.
    """
    vm_names: list[str] | None = None
    vm_name: str | None = None
    asset: str | None = None
    arguments: str = ""
    run: bool = False


@router.post("/managed/{env_id}/guest-step", status_code=202, dependencies=_POV_WRITE_OWN)
def guest_step(env_id: str, payload: GuestStepRequest,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Set the guest-step list and, when asked, queue one run on the broker agent."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    pov_guest_step.configure(db, env, vm_names=payload.vm_names)

    if not payload.run:
        # Saving the list is useful on its own: it is what the next Broker run grants, and
        # an SE usually names the guests before they have uploaded anything to run on them.
        return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}

    try:
        job = pov_guest_step.queue(
            db, env, vm_name=payload.vm_name or "", asset=payload.asset or "",
            arguments=payload.arguments,
            created_by=getattr(current_user, "username", None))
    except pov_guest_step.GuestStepError as exc:
        # 409 with the remedy passed through — every refusal this raises names what the
        # operator does next: stage a file, add the guest to the list, press Broker.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job.id,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.get("/managed/{env_id}/ps-config")
async def ps_config(env_id: str, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Read this POV's Password Safe tenant back against the runbook that filled it.

    A live read behind a button, like the Gateway check — not spread onto the POV list,
    because it is eight collections per POV.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    try:
        return await pov_ps_config.read(db, env)
    except pov_ps_config.PsConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/managed/{env_id}/pra-ps/check", dependencies=_POV_WRITE_OWN)
async def pra_ps_check(env_id: str, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Which of this POV's Password Safe accounts PRA can see. A live read behind a
    button; the count is stored for the setup ladder. POST because it writes that count."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    try:
        result = await pov_pra_ps_link.check(db, env)
    except pov_pra_ps_link.LinkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**result, "environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.post("/managed/{env_id}/pra-ps/link", dependencies=_POV_WRITE_OWN)
async def pra_ps_link(env_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Grant each of this POV's Password Safe accounts PRA can see to the vendor Group
    Policy (inject) and associate it with its guest's jump item. Idempotent."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    try:
        result = await pov_pra_ps_link.link(db, env)
    except pov_pra_ps_link.LinkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**result, "environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.get("/managed/{env_id}/ps-smart-rules")
async def ps_smart_rules(env_id: str, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """The tenant's Smart Rules, for the picker behind the re-process button."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    try:
        return {"rules": await pov_ps_config.smart_rules(db, env)}
    except pov_ps_config.PsConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/managed/{env_id}/ps-smart-rules/{rule_id}/process", dependencies=_POV_WRITE_OWN)
async def ps_process_smart_rule(env_id: str, rule_id: int,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    """Re-run one Smart Rule in this POV's Password Safe tenant.

    The runbook's use case 1 and use case 18 both come down to this, which is why it is a
    button rather than a job: the call is queued tenant-side and returns at once, and the
    rule's own last-processed time is what says it finished.

    Not a write to anything this dashboard created — it re-runs a rule the customer's
    admin made, which is what makes it safe on a POV somebody else configured.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    try:
        result = await pov_ps_config.process(db, env, rule_id)
    except pov_ps_config.PsConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    job_service.log_audit(
        db, getattr(current_user, "username", "?"), "pov_smart_rule_processed",
        details={"environment_id": env.id, "smart_rule_id": result.get("smart_rule_id"),
                 "tenant": result.get("tenant")})
    return result


class EntitleAgentRequest(BaseModel):
    """Configure and install this POV's Entitle agent.

    ``vm_name`` names the Linux guest it goes on. There is no token field and there cannot
    be one: the agent token is **minted in this POV's own Entitle tenant** when the install
    is queued, because Entitle returns a token's value only at creation and a value an
    operator pasted is a value this dashboard could never destroy at teardown.
    """
    vm_name: str | None = None
    install: bool = True


@router.post("/managed/{env_id}/entitle-agent", status_code=202, dependencies=_POV_WRITE_OWN)
async def entitle_agent(env_id: str, payload: EntitleAgentRequest,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Set the Entitle agent's host and queue its install on the broker agent.

    The SSH login is **not** a field here, for the same reason the Resource Broker's is
    not: it comes from the lab platform's own stored credentials, read per run.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    pov_entitle_agent.configure(db, env, vm_name=payload.vm_name)
    if not payload.install:
        return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}

    try:
        job = await pov_entitle_agent.queue(
            db, env, created_by=getattr(current_user, "username", None))
    except pov_entitle_agent.EntitleAgentError as exc:
        # 409 with the remedy passed through — every refusal this raises is something the
        # operator does next: press Broker, name a tenant, name a Linux host.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job.id,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


class OTAdapterRequest(BaseModel):
    """Deploy the OT HMI's Entitle adapter. See ``services/pov_ot_adapter``.

    ``dry_run`` defaults ON, the adapter's own default: grants report what they would do
    without touching the HMI. Press again with it off to grant for real.
    """
    dry_run: bool = True


@router.post("/managed/{env_id}/ot-adapter", status_code=202, dependencies=_POV_WRITE_OWN)
async def ot_adapter(env_id: str, payload: OTAdapterRequest,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Rotate the OT simulator's HMI password, deploy the Entitle adapter onto the OT DMZ
    broker guest, and register it in this POV's Entitle tenant."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    try:
        note = await pov_ot_adapter.queue(
            db, env, dry_run=payload.dry_run,
            created_by=getattr(current_user, "username", None) or "")
    except pov_ot_adapter.OTAdapterError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"note": note,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.post("/managed/{env_id}/wireup", status_code=202, dependencies=_POV_WRITE_OWN)
def wireup(env_id: str, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Wire every VM in this POV into PRA, and into Password Safe and Entitle when it has
    a tenant for them.

    Refused up front when the POV has no Gateway or no resolvable PRA tenant, because
    neither is something the second VM will do better at — and thirty identical failures
    in a job log hide the one line that matters.

    The **Password Safe and Entitle halves are not preflighted here**, deliberately. Both
    are independent and optional: a POV with no tenant for one of them, or one whose
    tenant is half configured, should still get everything else. The job says which half
    it skipped and why, in its own log.

    Re-runnable: a VM that already has an artifact is skipped rather than given a second
    one, so this is also the remedy for a run that half-finished.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    try:
        # `preflight`, not `tenant_override`: a POV with nothing wired has no Asset Group
        # yet, and the job creates it — refusing here would refuse the fix.
        pov_wireup.preflight(db, env)
        pov_wireup.gateway_name(env)
    except (pov_wireup.WireupError, pov_gateway.GatewayInstallError,
            bt_tenant_service.BTTenantError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    job = job_service.create_job(
        db, job_type="pov_env_wireup",
        created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id})
    return {"job_id": job.id}


@router.post("/managed/{env_id}/jump-group/move", status_code=202,
             dependencies=_POV_WRITE_OWN)
def move_jump_group(env_id: str, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Rebuild this POV's jump items in an Asset Group of its own.

    For a POV wired before per-POV Asset Groups existed, whose items are therefore in the
    group named on its *tenant* — one group every POV on that appliance shares. A PRA
    Group Policy grants access by Asset Group, so that is the wrong scope for a vendor, and
    ``pov_vendor_access.blocker`` refuses to create one. This is the way out it names.

    **Not the same button as Wire up, deliberately.** A jump item names its Asset Group at
    creation, so moving one means destroying and rebuilding it — a POV's items really do
    disappear from the appliance for the length of this job. That is not something a
    re-run of Wire up should do quietly to a POV that is working, which is why
    ``ensure_jump_group`` leaves an already-wired POV alone and this endpoint exists.

    Refused up front for a POV that already has its own group (nothing to move) or has
    nothing wired (Wire up creates the group itself), so neither becomes a job row whose
    only content is a refusal.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    if (env.pra_jump_group_name or "").strip():
        raise HTTPException(
            status_code=409,
            detail=f"this POV's jump items are already in its own Asset Group "
                   f"{env.pra_jump_group_name}, so there is nothing to move.")
    if not pov_vendor_access.can_move_jump_group(db, env):
        # The same three conditions the card's button is shown on, checked again here:
        # the button is a hint, and a POV with nothing wired must not be able to queue a
        # job whose first act is to destroy nothing.
        raise HTTPException(
            status_code=409,
            detail="this POV has no jump items in the tenant's Asset Group to move. Press "
                   "Wire up instead — on a POV with nothing wired yet it creates a Jump "
                   "Group of its own.")

    try:
        pov_wireup.tenant_override(db, env)
        pov_wireup.gateway_name(env)
    except (pov_wireup.WireupError, pov_gateway.GatewayInstallError,
            bt_tenant_service.BTTenantError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    job = job_service.create_job(
        db, job_type="pov_env_jump_group_move",
        created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id})
    return {"job_id": job.id}


class EntitleKeyRequest(BaseModel):
    """The private half of the SSH key baked into this POV template's Linux guests.

    Entitle's ephemeral-accounts connector authenticates with a key, not the password the
    lab platform holds — so this is the one credential the POV wire-up cannot derive from
    anything it already has. Blank clears it.
    """
    private_key: str = ""


@router.post("/managed/{env_id}/entitle-key", dependencies=_POV_WRITE_OWN)
def entitle_key(env_id: str, payload: EntitleKeyRequest,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Store (or clear) this POV's Entitle SSH key. Never returns it."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    pov_wireup.set_entitle_key(env, payload.private_key)
    return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}
