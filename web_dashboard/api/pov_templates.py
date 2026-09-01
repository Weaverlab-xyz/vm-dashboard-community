"""Authoring the templates a POV is built from, and the blueprints that build one.

  GET    /api/pov/builds                — the template builds this dashboard ran
  POST   /api/pov/builds                — start one
  GET    /api/pov/builds/{id}           — one, with its template-contract report
  DELETE /api/pov/builds/{id}           — reap its scratch environment and close it out
  GET    /api/pov/verify                — contract-check a live template or environment
  GET    /api/pov/runner-script         — the metadata runner, to paste in by hand

  GET    /api/pov/blueprints            — saved POV recipes
  POST   /api/pov/blueprints            — save one
  PUT    /api/pov/blueprints/{id}       — edit one
  DELETE /api/pov/blueprints/{id}       — delete one

  GET    /api/pov/cloud-templates       — the topology a CLOUD POV is built from
  GET    /api/pov/cloud-templates/{id}  — one, with its VMs
  POST   /api/pov/cloud-templates       — save one
  PUT    /api/pov/cloud-templates/{id}  — edit one
  DELETE /api/pov/cloud-templates/{id}  — delete one

Its own module rather than more of ``api/pov.py``, which is already long, but the **same
prefix and the same gate**: these are POV routes, and on a demo instance they 404 with the
rest of the feature.

**Reads authenticate; writes require an admin** — the split ``api/bt_tenants`` already uses.
It is worth more here than there: a build creates a real environment in the customer's lab
account and bakes a real template into their catalogue, so every mutation below spends their
money or changes what other people will build POVs from.

The build collection is ``/builds``, not ``/templates``: ``GET /api/pov/templates`` already
exists and is the catalogue read the POV create form uses. Two different things called
templates under one prefix is the ambiguity worth spending a word to avoid.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovTemplateBuild, User, get_db
from ..services import (job_service, lab_platforms, pov_blueprint_service,
                        pov_broker, pov_cloud_env, pov_cloud_template_service,
                        pov_template_builder)
from .auth import get_current_user, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pov", tags=["pov"])

_DEFAULT_PLATFORM = "skytap"


def _adapter(platform: str):
    try:
        name = lab_platforms.normalize(platform)
        return name, lab_platforms.adapter(name)
    except lab_platforms.LabPlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _platform_error(exc: Exception, doing: str) -> HTTPException:
    """A platform failure as a 502 naming what was being done.

    Same shape as ``api/pov._platform_error``: the adapter's messages already name their
    remedy, so passing them through is what makes a stale Project ID or a rejected token
    fixable from the page rather than from a log.
    """
    logger.warning("POV templates: %s failed", doing, exc_info=True)
    return HTTPException(status_code=502, detail=f"{doing} failed: {exc}")


# ── template builds ──────────────────────────────────────────────────────────

class BuildRequest(BaseModel):
    """Start a template build.

    ``install_runner`` is opt-out rather than opt-in. The runner is the piece of the
    template contract with no automation before this, and a builder that defaults to not
    installing it would bake exactly the template people already have trouble with.
    """
    platform: str = _DEFAULT_PLATFORM
    base_template_id: str
    name: str
    description: str = ""
    project_id: str = ""
    broker_vm_name: str = ""
    install_runner: bool = True
    keep_build_environment: bool = False
    workgroup: str = ""

    @field_validator("name")
    @classmethod
    def _named(cls, v: str) -> str:
        v = (v or "").strip()
        if not 2 <= len(v) <= 255:
            raise ValueError("a template name of 2-255 characters is required")
        return v


@router.get("/builds")
async def list_builds(db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    rows = (db.query(PovTemplateBuild)
              .order_by(PovTemplateBuild.created_at.desc()).all())
    return {"builds": [pov_template_builder.serialize(b) for b in rows]}


@router.get("/builds/{build_id}")
async def get_build(build_id: str, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    build = pov_template_builder.get(db, build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="No such template build")
    return {"build": pov_template_builder.serialize(build)}


@router.post("/builds", status_code=202)
async def start_build(payload: BuildRequest, db: Session = Depends(get_db),
                      current_user: User = Depends(require_admin)):
    """Build a template. 202: it does not exist yet, and the job is where the failure is.

    The refusals here are all things resolvable *before* an environment exists. A base
    template id that the account cannot see would otherwise fail inside the job, minutes
    later, against a scratch environment somebody then has to reap — which is exactly the
    ordering ``api/pov.provision`` avoids for the same reason.
    """
    name, mod = _adapter(payload.platform)

    if not lab_platforms.supports(name, "template_authoring"):
        raise HTTPException(
            status_code=409,
            detail=f"{name} cannot save an environment back as a template, so a template "
                   f"cannot be built on it.")

    if db.query(PovTemplateBuild).filter(
            PovTemplateBuild.name == payload.name,
            PovTemplateBuild.status.in_((pov_template_builder.STATUS_BUILDING,
                                         pov_template_builder.STATUS_PREPARING,
                                         pov_template_builder.STATUS_BAKING))).first():
        raise HTTPException(
            status_code=409,
            detail=f"a template named {payload.name!r} is already being built")

    base_name = ""
    try:
        for t in await mod.list_templates():
            if t["id"] == str(payload.base_template_id):
                base_name = t["name"]
                break
        else:
            raise HTTPException(
                status_code=400,
                detail=f"template {payload.base_template_id!r} is not visible to this "
                       f"{name} account")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"listing {name} templates") from exc

    effective_project = (payload.project_id or "").strip()
    if not effective_project and lab_platforms.supports(name, "projects"):
        effective_project = mod.configured_project_id()

    build = PovTemplateBuild(
        platform=name,
        name=payload.name,
        description=payload.description or None,
        base_template_id=str(payload.base_template_id),
        base_template_name=base_name,
        project_id=effective_project or None,
        broker_vm_name=(payload.broker_vm_name.strip()
                        or pov_broker.DEFAULT_BROKER_VM_NAME),
        keep_build_environment=bool(payload.keep_build_environment),
        status=pov_template_builder.STATUS_BUILDING,
        workgroup=payload.workgroup or None,
        created_by=getattr(current_user, "username", None),
    )
    db.add(build)
    db.commit()

    job = job_service.create_job(
        db, job_type="pov_template_build",
        created_by=getattr(current_user, "username", None),
        workgroup=payload.workgroup or None,
        metadata={"build_id": build.id, "platform": name,
                  "install_runner": bool(payload.install_runner)})
    build.job_id = job.id
    db.commit()
    return {"build": pov_template_builder.serialize(build), "job_id": job.id}


@router.delete("/builds/{build_id}")
async def discard_build(build_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(require_admin)):
    """Reap a build's scratch environment and close the row out.

    Not 202: this is one platform DELETE, not a job. A failure is reported to the caller
    rather than swallowed, because a scratch environment that could not be reaped is still
    running and still billing — and the row deliberately stays visible so it can be tried
    again.
    """
    build = pov_template_builder.get(db, build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="No such template build")
    try:
        message = await pov_template_builder.discard(db, build)
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"reaping the build environment for {build.name!r}") \
            from exc
    return {"build": pov_template_builder.serialize(build), "message": message}


# ── verify, and the paste-it script ──────────────────────────────────────────

@router.get("/verify")
async def verify_contract(target_id: str = Query(...),
                          kind: str = Query("template"),
                          platform: str = Query(_DEFAULT_PLATFORM),
                          broker_vm_name: str = Query(""),
                          current_user: User = Depends(get_current_user)):
    """Check a live template or environment against the template contract.

    A read, so a GET, and no row is written: this answers "would a POV built from this
    work?" for a template somebody else authored, which is the question an SE has about
    the existing catalogue and cannot currently ask at all.
    """
    name, mod = _adapter(platform)
    kind = (kind or "template").strip().lower()
    if kind not in ("template", "environment"):
        raise HTTPException(status_code=400,
                            detail="kind must be 'template' or 'environment'")
    try:
        target = (await mod.get_template(target_id) if kind == "template"
                  else await mod.get_environment(target_id))
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"reading {name} {kind} {target_id}") from exc

    wanted = broker_vm_name.strip() or pov_broker.DEFAULT_BROKER_VM_NAME
    report = pov_template_builder.check_contract(target.get("vms") or [], wanted)
    return {
        "platform": name,
        "kind": kind,
        "id": str(target.get("id") or target_id),
        "name": target.get("name") or "",
        "broker_vm_name": wanted,
        "report": report,
        "ok": pov_template_builder.contract_ok(report),
    }


@router.get("/runner-script")
async def runner_script(current_user: User = Depends(get_current_user)):
    """The metadata runner and its unit, as a script to run on a broker VM.

    Offered always, not only after a failed install: an operator baking a template from a
    network with no route to a NAT-ed high port needs this as the primary route, and should
    not have to fail once to discover it exists.
    """
    return {
        "install_script": pov_template_builder.render_install_script(),
        "runner": pov_template_builder.render_runner(),
        "unit": pov_template_builder.render_runner_unit(),
        "runner_path": pov_template_builder.RUNNER_PATH,
        "unit_path": pov_template_builder.RUNNER_UNIT_PATH,
    }


# ── blueprints ───────────────────────────────────────────────────────────────

class BlueprintRequest(BaseModel):
    platform: str = _DEFAULT_PLATFORM
    name: str
    template_id: str
    template_name: str = ""
    description: str = ""
    project_id: str = ""
    broker_vm_name: str = ""
    suspend_on_idle_seconds: int = 0
    pra_tenant_id: str = ""
    ps_tenant_id: str = ""
    entitle_tenant_id: str = ""
    gateway_name: str = ""
    rb_windows_vm_name: str = ""
    rb_zone: str = ""
    rb_asset: str = ""
    expiry_hours: int = 0
    workgroup: str = ""
    source_build_id: str = ""


@router.get("/blueprints")
async def list_blueprints(db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    return {"blueprints": [pov_blueprint_service.serialize(b)
                           for b in pov_blueprint_service.list_all(db)]}


@router.post("/blueprints", status_code=201)
async def create_blueprint(payload: BlueprintRequest, db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    try:
        row = pov_blueprint_service.create(
            db, platform=payload.platform, name=payload.name,
            template_id=payload.template_id, template_name=payload.template_name,
            description=payload.description, project_id=payload.project_id,
            broker_vm_name=payload.broker_vm_name,
            suspend_on_idle_seconds=payload.suspend_on_idle_seconds or None,
            pra_tenant_id=payload.pra_tenant_id, ps_tenant_id=payload.ps_tenant_id,
            entitle_tenant_id=payload.entitle_tenant_id,
            gateway_name=payload.gateway_name,
            rb_windows_vm_name=payload.rb_windows_vm_name,
            rb_zone=payload.rb_zone, rb_asset=payload.rb_asset,
            expiry_hours=payload.expiry_hours or None,
            workgroup=payload.workgroup,
            created_by=getattr(current_user, "username", None),
            source_build_id=payload.source_build_id)
    except pov_blueprint_service.BlueprintError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"blueprint": pov_blueprint_service.serialize(row)}


@router.put("/blueprints/{blueprint_id}")
async def update_blueprint(blueprint_id: str, payload: dict,
                           db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    """Partial update: only the keys present are touched.

    A raw dict rather than a model, because "present" and "blank" have to stay
    distinguishable — a Pydantic model with defaults cannot tell a field somebody cleared
    from one they did not send, and clearing a field is a thing an operator does.
    """
    row = pov_blueprint_service.get(db, blueprint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such blueprint")
    try:
        row = pov_blueprint_service.update(db, row, payload or {})
    except pov_blueprint_service.BlueprintError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"blueprint": pov_blueprint_service.serialize(row)}


@router.delete("/blueprints/{blueprint_id}")
async def delete_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    row = pov_blueprint_service.get(db, blueprint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such blueprint")
    name = row.name
    pov_blueprint_service.delete(db, row)
    return {"deleted": name}


# ── cloud templates ──────────────────────────────────────────────────────────
#
# The topology a cloud POV is built from. `/blueprints` above names a template and fills
# in a create form; these rows ARE the template, because no public cloud holds one.
#
# Under `/cloud-templates` rather than `/templates`: `GET /api/pov/templates` is already
# the platform catalogue read the create form uses, and on a cloud that read is served
# FROM these rows. Two different things called templates under one prefix is the
# ambiguity worth a word to avoid — the same reason builds are at `/builds`.


class CloudTemplateVMRequest(BaseModel):
    name: str
    role: str = "target"
    os_family: str = "linux"
    image_ref: str = ""
    image_id: str = ""
    instance_type: str = ""
    disk_gb: int = 0


class CloudTemplateRequest(BaseModel):
    cloud: str = ""
    name: str
    description: str = ""
    region: str = ""
    network_cidr: str = ""
    workgroup: str = ""
    vms: list[CloudTemplateVMRequest] = []


def _cloud_or_selected(cloud: str) -> str:
    """The cloud a request means, defaulting to the one this instance has selected.

    A default rather than a required field: a POV instance runs one cloud, so making
    every call name it would be asking the operator to repeat a setting back to the
    dashboard. An explicit value still wins, and an unknown one is refused by the service.
    """
    return (cloud or "").strip().lower() or lab_platforms.selected_cloud()


@router.get("/cloud-templates")
async def list_cloud_templates(cloud: str = Query(""), db: Session = Depends(get_db),
                               current_user: User = Depends(get_current_user)):
    """The cloud templates on this instance.

    Answers with the selected cloud and the built ones so the editor can render itself
    without a second call — and so a page loaded on an instance with no cloud selected
    can say that, rather than showing an empty list that looks like "none saved yet".
    """
    selected = lab_platforms.selected_cloud()
    rows = pov_cloud_template_service.list_for(db, _cloud_or_selected(cloud))
    return {
        "selected_cloud": selected,
        "clouds": list(lab_platforms.CLOUD_PLATFORMS),
        "default_network_cidr": pov_cloud_env.DEFAULT_NETWORK_CIDR,
        "roles": list(pov_cloud_template_service.VALID_ROLES),
        "os_families": list(pov_cloud_template_service.VALID_OS_FAMILIES),
        "templates": [pov_cloud_template_service.describe(db, r) for r in rows],
    }


@router.get("/cloud-templates/{template_id}")
async def get_cloud_template(template_id: str, db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    row = pov_cloud_template_service.get(db, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such cloud template")
    return {"template": pov_cloud_template_service.describe(db, row)}


@router.post("/cloud-templates", status_code=201)
async def create_cloud_template(payload: CloudTemplateRequest,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(require_admin)):
    """Save a cloud template.

    Admin, like every other write in this module, and for the sharper of the two reasons
    given there: a template is what other people's POVs get built from, so a bad one
    spends the account's money in a shape nobody chose.
    """
    cloud = _cloud_or_selected(payload.cloud)
    if not cloud:
        raise HTTPException(
            status_code=409,
            detail=("no POV cloud provider is selected on this instance. Choose one in "
                    "Settings → Integrations → POV cloud provider, then save this "
                    "template."))
    try:
        row = pov_cloud_template_service.create(
            db, cloud=cloud, name=payload.name, description=payload.description,
            region=payload.region, network_cidr=payload.network_cidr,
            workgroup=payload.workgroup,
            created_by=getattr(current_user, "username", None),
            vms=[v.model_dump() for v in payload.vms])
    except pov_cloud_template_service.CloudTemplateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"template": pov_cloud_template_service.describe(db, row)}


@router.put("/cloud-templates/{template_id}")
async def update_cloud_template(template_id: str, payload: dict,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(require_admin)):
    """Partial update: only the keys present are touched.

    A raw dict rather than a model, for the reason the blueprint editor gives above —
    "present" and "blank" have to stay distinguishable, and clearing a field is a thing an
    operator does. ``vms``, when present, replaces the whole list.
    """
    row = pov_cloud_template_service.get(db, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such cloud template")
    try:
        row = pov_cloud_template_service.update(db, row, payload or {})
    except pov_cloud_template_service.CloudTemplateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"template": pov_cloud_template_service.describe(db, row)}


@router.delete("/cloud-templates/{template_id}")
async def delete_cloud_template(template_id: str, db: Session = Depends(get_db),
                                current_user: User = Depends(require_admin)):
    """Delete a template.

    A live POV built from it is deliberately not a reason to refuse — see
    ``pov_cloud_template_service.delete``. The count of POVs that named it is reported so
    the answer is informed rather than blind.
    """
    row = pov_cloud_template_service.get(db, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such cloud template")
    name = row.name
    built = (db.query(PovEnvironment)
               .filter(PovEnvironment.template_id == row.id,
                       PovEnvironment.status != "destroyed").count())
    pov_cloud_template_service.delete(db, row)
    return {"deleted": name, "live_povs_built_from_it": built}
