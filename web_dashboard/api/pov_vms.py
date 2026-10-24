"""A POV's VMs: adding them, the application host, and each VM's login, OS and cell role.

Registered on ``api/pov.py``'s router, not a router of its own, so the router-level gates
(the ``pov:read`` floor and the per-POV instance gate) cover every route here exactly as they
did when this lived in that file. ``api/pov.py`` imports this module last, after everything
imported from it below is defined.
"""

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import PovEnvironmentVM, User, get_db
from ..services import (job_service, lab_platforms, pov_broker, pov_cell_roles,
                        pov_env_service, pov_resource_broker)
from .auth import get_current_user
from .pov import _POV_WRITE_OWN, _serialize, router


class AddVmsRequest(BaseModel):
    """Copy VMs from a template into a POV that already exists.

    The motion this is for is use case 18 of BeyondTrust's Skytap Password Safe POC: add
    two guests to a running environment and show the existing Smart Rules onboard them
    with nothing edited. Use case 15 wants the same call for a Password Safe Cache guest
    that template Part 1 does not carry.

    ``vm_ids`` is required, not optional. Skytap's own API treats an omitted list as
    "merge the whole template", which against a live POV means silently doubling it.

    ``power_on`` defaults to true because the copies arrive **stopped** even when
    everything around them is running, and a Smart Rule that "did not work" on a guest
    that was never powered on is a bad half-hour.
    """
    template_id: str
    vm_ids: list[str]
    power_on: bool = True


@router.post("/managed/{env_id}/vms", status_code=202, dependencies=_POV_WRITE_OWN)
def add_vms(env_id: str, payload: AddVmsRequest,
                  db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Queue a VM copy into this POV."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    if not lab_platforms.supports(env.platform, "vm_add"):
        label = lab_platforms.capabilities(env.platform).get("label", env.platform)
        raise HTTPException(
            status_code=409,
            detail=f"{label} cannot add VMs to an environment that already exists. Its "
                   f"VM set is whatever created it.")

    template_id = (payload.template_id or "").strip()
    vm_ids = [v.strip() for v in (payload.vm_ids or []) if v and v.strip()]
    if not template_id:
        raise HTTPException(status_code=400, detail="Name the source template.")
    if not vm_ids:
        raise HTTPException(
            status_code=400,
            detail="Pick at least one VM. An empty selection would merge the whole "
                   "template into this environment.")
    if pov_env_service.power_job_in_flight(db, env.id):
        # The copy waits for the environment to settle and may power it on, so it and a
        # power job would each be waiting on a runstate the other is changing.
        raise HTTPException(
            status_code=409,
            detail="A power job is already running on this POV. Let it finish first.")

    job = job_service.create_job(
        db, job_type="pov_env_add_vms", created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id, "template_id": template_id,
                  "vm_ids": vm_ids, "power_on": bool(payload.power_on)})
    return {"job_id": job.id,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


class ApplicationHostRequest(BaseModel):
    """Set or clear this POV's Password Safe application host id.

    An **override**, and the only writer of that column. It is not the Resource Broker
    handle: what routes Password Safe to a private address is the broker's resource zone
    and the workgroup mapped to it, which is BeyondInsight configuration this dashboard
    performs no part of. The wire-up sends ``0`` when nothing is set here, which is what
    every other Password Safe caller in this codebase does.

    ``0`` clears it, and that is the normal state.
    """
    application_host_id: int = 0


@router.post("/managed/{env_id}/application-host", dependencies=_POV_WRITE_OWN)
def application_host(env_id: str, payload: ApplicationHostRequest,
                           db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    """Record an operator-supplied application host id, so a wire-up is never stuck on it."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    try:
        note = pov_resource_broker.set_application_host(
            db, env, payload.application_host_id)
    except pov_resource_broker.ResourceBrokerError as exc:
        # 400 rather than 409: this one is about the value in the request, not about a
        # step the operator has yet to take.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"note": note,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


class VmLoginRequest(BaseModel):
    """Say which of a guest's stored platform credentials its runs should use.

    A **username**, not a credential, and the field is named so. The password stays on the
    lab platform and is read live at the moment of every run, exactly as before; all this
    records is which of several accounts an SE meant. Sending an empty string clears the
    override, which restores the guest-OS rule and is the normal state.

    The control exists because several credentials on one host is the norm, and the rule
    in ``services/pov_credentials`` cannot separate two logins of equal standing — two
    local service accounts, say. Without a way to answer that, the refusal it raises would
    have no remedy but editing the credential box on the platform.

    ``login_password`` applies only on a platform that stores no VM credentials (AWS, GCP,
    OCI), where the login is set here or nowhere. It is stored encrypted and never served
    back. Omitted leaves the stored one as it is; ``""`` clears it. Sent for a platform
    that holds its own, it is refused.
    """
    login_username: str = ""
    login_password: str | None = None


@router.post("/managed/{env_id}/vms/{vm_id}/login", dependencies=_POV_WRITE_OWN)
def set_vm_login(env_id: str, vm_id: str, payload: VmLoginRequest,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Pin one POV guest to one stored login, or set its login where none is stored."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    try:
        note = pov_env_service.set_vm_login(db, env, vm_id, payload.login_username,
                                            payload.login_password)
    except pov_env_service.VmLoginError as exc:
        # 400, not 409: this is about the value in the request, not a step still owed.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    vms = (db.query(PovEnvironmentVM)
             .filter(PovEnvironmentVM.environment_id == env.id).all())
    return {"note": note,
            "environment": _serialize(env, vms=vms,
                                      broker=pov_broker.describe(db, env))}


class VmOsRequest(BaseModel):
    """Say what a POV guest runs, when the lab platform would not.

    Not a guess this dashboard makes. ``skytap_service._os_family`` is a token match over
    whatever text the platform hands back and returns "" rather than answer wrongly, and
    every POV selector refuses a blank family instead of reading it as Linux. Both rules
    stay. This is the operator ending the blank, which is the only remedy those refusals
    ever had — and on a template whose guests the platform will not classify (a name like
    ``BtPocLin01`` matches no token), the alternative remedy was recreating the POV.

    Blank clears the override and hands the answer back to the platform.
    """
    os_family: str = ""


@router.post("/managed/{env_id}/vms/{vm_id}/os", dependencies=_POV_WRITE_OWN)
def set_vm_os(env_id: str, vm_id: str, payload: VmOsRequest,
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Record what one POV guest runs, or clear the record."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    try:
        note = pov_env_service.set_vm_os(db, env, vm_id, payload.os_family)
    except pov_env_service.VmOsError as exc:
        # 400, not 409: this is about the value in the request, not a step still owed.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    vms = (db.query(PovEnvironmentVM)
             .filter(PovEnvironmentVM.environment_id == env.id).all())
    return {"note": note,
            "environment": _serialize(env, vms=vms,
                                      broker=pov_broker.describe(db, env))}


class VmCellRoleRequest(BaseModel):
    """Say which demo-cell role a POV guest plays, if any.

    ``"ot-sim"`` adds the OT simulator's HMI Web Jump and one PRA protocol tunnel per PLC
    protocol to the next Wire up; ``"vyos"`` keeps the guest out of Entitle. Blank makes
    it an ordinary target. See ``services/pov_cell_roles``.
    """
    cell_role: str = ""


@router.post("/managed/{env_id}/vms/{vm_id}/cell-role", dependencies=_POV_WRITE_OWN)
def set_vm_cell_role(env_id: str, vm_id: str, payload: VmCellRoleRequest,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Record which demo-cell role one POV guest plays, or clear it."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    vm = (db.query(PovEnvironmentVM)
            .filter(PovEnvironmentVM.environment_id == env.id,
                    PovEnvironmentVM.platform_vm_id == vm_id).first())
    if vm is None:
        raise HTTPException(status_code=404, detail="No such VM in this POV")
    try:
        note = pov_cell_roles.set_role(db, env, vm, payload.cell_role)
    except pov_cell_roles.CellRoleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    vms = (db.query(PovEnvironmentVM)
             .filter(PovEnvironmentVM.environment_id == env.id).all())
    return {"note": note,
            "environment": _serialize(env, vms=vms,
                                      broker=pov_broker.describe(db, env))}
