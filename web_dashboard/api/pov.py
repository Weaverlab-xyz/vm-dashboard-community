"""POV environments — read-only.

  GET    /api/pov/platforms             — the registry, with capabilities and configured state
  GET    /api/pov/templates             — templates a POV could be created from
  GET    /api/pov/environments          — environments visible on the platform
  GET    /api/pov/environments/{id}     — one, live from the platform
  GET    /api/pov/managed               — the POVs this dashboard provisioned
  POST   /api/pov/managed               — provision one from a template
  POST   /api/pov/managed/{id}/power    — running | suspended | stopped | halted
  POST   /api/pov/managed/{id}/broker   — install / re-enrol the in-environment agent
  DELETE /api/pov/managed/{id}          — destroy it and reap the platform side

The ``/environments`` reads are LIVE against the platform and include environments this
dashboard never created — an SE's hand-built POVs are exactly what they want to see next to
the managed ones. ``/managed`` is this dashboard's own inventory. Keeping them apart is
what lets the read-only view stay honest about what it does and does not own.

The whole router is gated on ``pov_environments_enabled``, which
``feature_flags._POV_ONLY`` masks off on a demo instance — so on the demo dashboard these
routes 404 with a message naming the profile, not a stack trace.

Every endpoint takes ``?platform=`` and resolves it through ``lab_platforms``. Skytap is the
only adapter today; the parameter exists so the second one is a registry entry rather than a
second router.
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM, User, get_db
from ..services import job_service, lab_platforms, pov_broker, pov_env_service
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pov", tags=["pov"])

_DEFAULT_PLATFORM = "skytap"

# Names become the environment's display name on the platform and appear in job output, so
# they are constrained rather than free text. Also the place someone would otherwise type a
# customer name -- the docs say not to, and this keeps the shape unfriendly to it.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class ProvisionRequest(BaseModel):
    """Note what is absent: there is no customer field, and adding one later should be
    treated as a schema regression. A POV is scoped by workgroup and created_by."""
    platform: str = _DEFAULT_PLATFORM
    template_id: str
    name: str
    workgroup: str = ""
    project_id: str = ""
    # 0 disables it. Defaulted by the caller rather than here so the value that reaches
    # the platform is always one somebody chose.
    suspend_on_idle_seconds: int = 0
    # Which VM in the template runs the agent. Per-POV rather than a global setting:
    # templates come from wherever the SE got them, and one account can hold two that name
    # it differently. Blank means pov_broker.DEFAULT_BROKER_VM_NAME.
    broker_vm_name: str = ""

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _NAME_RE.match(v):
            raise ValueError(
                "name must be 2-63 characters of lowercase letters, digits and hyphens, "
                "starting with a letter or digit")
        return v


class PowerRequest(BaseModel):
    runstate: str

    @field_validator("runstate")
    @classmethod
    def _known(cls, v: str) -> str:
        v = (v or "").strip().lower()
        # The adapter owns the authoritative list; this is the early, friendly refusal.
        if v not in ("running", "suspended", "stopped", "halted"):
            raise ValueError("runstate must be running, suspended, stopped or halted")
        return v


def _serialize(env: PovEnvironment, vms: list | None = None,
               broker: dict | None = None) -> dict:
    """The row as the POV page reads it.

    ``broker`` is passed in rather than derived here because describing it needs a session
    and a query per row — the list endpoint does that once per row deliberately, and a
    caller that does not care (the 202 responses) should not pay for it.
    """
    out = {
        "id": env.id,
        "platform": env.platform,
        "name": env.name,
        "platform_environment_id": env.platform_environment_id or "",
        "template_id": env.template_id or "",
        "template_name": env.template_name or "",
        "status": env.status,
        "runstate": env.runstate or "",
        "region": env.region or "",
        "workgroup": env.workgroup or "",
        "created_by": env.created_by or "",
        "created_at": env.created_at.isoformat() if env.created_at else None,
        "provision_job_id": env.provision_job_id or "",
        "error_message": env.error_message or "",
        "broker_vm_name": pov_broker.broker_vm_name(env),
    }
    if broker is not None:
        out.update(broker)
    if vms is not None:
        out["vms"] = [{
            "id": v.platform_vm_id,
            "name": v.name or "",
            "os_family": v.os_family or "",
            "runstate": v.runstate or "",
            "private_ip": v.private_ip or "",
            "published_services": v.published_services_list,
        } for v in vms]
    return out


def _adapter(platform: str):
    """Resolve a platform to its adapter, or fail with a message that names the problem.

    Two different failures, kept apart on purpose: an unknown platform is a bad request
    (the caller asked for something that does not exist), while an unconfigured one is a
    409 telling the operator where to fix it. Collapsing them into one 500 is how "Skytap
    isn't set up" ends up looking like a dashboard bug.
    """
    try:
        name = lab_platforms.normalize(platform)
    except lab_platforms.LabPlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mod = lab_platforms.adapter(name)
    if not mod.configured():
        raise HTTPException(
            status_code=409,
            detail=f"{lab_platforms.capabilities(name)['label']} is not configured — "
                   f"add its credentials in Settings → Integrations.",
        )
    return name, mod


def _platform_error(exc: Exception, what: str) -> HTTPException:
    """Turn an adapter error into a 502 that carries the platform's own words.

    The platform is upstream, so 502 rather than 500: this is not the dashboard failing,
    and the distinction is what stops an SE debugging the wrong system. The upstream text
    is included because a rate-limit and a bad token are both "it didn't work" without it.
    """
    logger.warning("POV: %s failed", what, exc_info=True)
    return HTTPException(status_code=502, detail=f"{what} failed: {exc}")


@router.get("/platforms")
async def list_platforms(current_user: User = Depends(get_current_user)):
    """Every lab platform this build knows about, with what it can do.

    ``capabilities`` is served to the UI rather than kept server-side so a platform that
    cannot, say, produce a share link renders "PRA only" instead of a button that 502s.
    """
    configured = set(lab_platforms.configured_platforms())
    return {
        "platforms": [
            {"name": name,
             "configured": name in configured,
             **lab_platforms.capabilities(name)}
            for name in lab_platforms.VALID_PLATFORMS
        ],
        "default": _DEFAULT_PLATFORM,
    }


@router.get("/templates")
async def list_templates(platform: str = Query(_DEFAULT_PLATFORM),
                         current_user: User = Depends(get_current_user)):
    """Templates a POV environment could be created from."""
    name, mod = _adapter(platform)
    try:
        return {"platform": name, "templates": await mod.list_templates()}
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"listing {name} templates") from exc


@router.get("/environments")
async def list_environments(platform: str = Query(_DEFAULT_PLATFORM),
                            current_user: User = Depends(get_current_user)):
    """Environments visible on the platform.

    Includes ones the dashboard did not create. That is the point of a read-only view: an
    SE's existing hand-built POVs are exactly what they want to see next to the managed
    ones.
    """
    name, mod = _adapter(platform)
    try:
        return {"platform": name, "environments": await mod.list_environments()}
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"listing {name} environments") from exc


@router.get("/environments/{env_id}")
async def get_environment(env_id: str,
                          platform: str = Query(_DEFAULT_PLATFORM),
                          current_user: User = Depends(get_current_user)):
    """One environment, with its VMs, private IPs and published services."""
    name, mod = _adapter(platform)
    try:
        return {"platform": name, "environment": await mod.get_environment(env_id)}
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"reading {name} environment {env_id}") from exc


# ── managed POVs: this dashboard's own inventory ─────────────────────────────

@router.get("/managed")
async def list_managed(db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    rows = (db.query(PovEnvironment)
              .filter(PovEnvironment.status != pov_env_service.STATUS_DESTROYED)
              .order_by(PovEnvironment.created_at.desc()).all())
    return {"environments": [_serialize(e, broker=pov_broker.describe(db, e))
                             for e in rows]}


@router.get("/managed/{env_id}")
async def get_managed(env_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    vms = (db.query(PovEnvironmentVM)
             .filter(PovEnvironmentVM.environment_id == env.id).all())
    return {"environment": _serialize(env, vms, broker=pov_broker.describe(db, env))}


@router.post("/managed", status_code=202)
async def provision(payload: ProvisionRequest,
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Create a POV environment from a template.

    202, not 201: the environment does not exist yet. This returns the row and the job
    that will build it, and the job is where the progress and the failure live.
    """
    name, mod = _adapter(payload.platform)

    if db.query(PovEnvironment).filter(
            PovEnvironment.name == payload.name,
            PovEnvironment.status != pov_env_service.STATUS_DESTROYED).first():
        raise HTTPException(
            status_code=409,
            detail=f"a POV environment named {payload.name!r} already exists")

    # Resolve the template name now, while we can say something useful about a bad id.
    # A wrong template id would otherwise fail inside the job, minutes later.
    template_name = ""
    try:
        for t in await mod.list_templates():
            if t["id"] == str(payload.template_id):
                template_name = t["name"]
                break
        else:
            raise HTTPException(
                status_code=400,
                detail=f"template {payload.template_id!r} is not visible to this "
                       f"{name} account")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"listing {name} templates") from exc

    env = PovEnvironment(
        platform=name,
        name=payload.name,
        template_id=str(payload.template_id),
        template_name=template_name,
        project_id=payload.project_id or "",
        workgroup=payload.workgroup or None,
        created_by=getattr(current_user, "username", None),
        status=pov_env_service.STATUS_PROVISIONING,
    )
    # On the row rather than in the job metadata: every later broker re-run reads it, and
    # a job's metadata is the record of one run, not of the environment.
    if payload.broker_vm_name.strip():
        env.metadata_dict = {"broker_vm_name": payload.broker_vm_name.strip()}
    db.add(env)
    db.commit()

    job = job_service.create_job(
        db, job_type="pov_env_provision",
        created_by=getattr(current_user, "username", None),
        workgroup=payload.workgroup or None,
        metadata={
            "environment_id": env.id,
            "platform": name,
            "template_id": env.template_id,
            "suspend_on_idle_seconds": int(payload.suspend_on_idle_seconds or 0),
        })
    env.provision_job_id = job.id
    db.commit()
    return {"environment": _serialize(env), "job_id": job.id}


@router.post("/managed/{env_id}/power", status_code=202)
async def power(env_id: str, payload: PowerRequest,
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    job = job_service.create_job(
        db, job_type="pov_env_power",
        created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id, "runstate": payload.runstate})
    return {"job_id": job.id, "runstate": payload.runstate}


@router.post("/managed/{env_id}/broker", status_code=202)
async def broker(env_id: str, db: Session = Depends(get_db),
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
    if not env.platform_environment_id:
        raise HTTPException(
            status_code=409,
            detail="this environment was never created on the platform, so there is no "
                   "VM to install a broker on.")
    if lab_platforms.capabilities(env.platform).get("bootstrap_injection") != "metadata":
        raise HTTPException(
            status_code=409,
            detail=f"{lab_platforms.capabilities(env.platform)['label']} does not deliver "
                   f"bootstrap data the way this broker install expects; it needs its own "
                   f"broker path.")

    job = job_service.create_job(
        db, job_type="pov_env_broker",
        created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id})
    return {"job_id": job.id}


@router.delete("/managed/{env_id}", status_code=202)
async def destroy(env_id: str, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Destroy the environment and reap the platform side.

    Allowed from `failed` as well as `active`: a POV that failed halfway through
    provisioning is exactly the one that most needs reaping, and refusing here would
    strand whatever it did create.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)

    job = job_service.create_job(
        db, job_type="pov_env_destroy",
        created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id})
    return {"job_id": job.id}
