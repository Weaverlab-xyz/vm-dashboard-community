"""POV environments — read-only.

  GET    /api/pov/platforms             — the registry, with capabilities and configured state
  GET    /api/pov/templates             — templates a POV could be created from
  GET    /api/pov/environments          — environments visible on the platform
  GET    /api/pov/environments/{id}     — one, live from the platform
  GET    /api/pov/managed               — the POVs this dashboard provisioned
  GET    /api/pov/managed/archive       — the ones that have been destroyed, and what
                                          each evaluation covered
  GET    /api/pov/managed/{id}/summary  — one evaluation's account: what was run, what
                                          was skipped, and what the customer said
  POST   /api/pov/managed               — provision one from a template
  POST   /api/pov/managed/reconcile     — read every managed environment back off the
                                          platform, now
  POST   /api/pov/managed/{id}/power    — running | suspended | stopped | halted
  POST   /api/pov/managed/{id}/broker   — install / re-enrol the in-environment agent
  POST   /api/pov/managed/{id}/gateway  — configure and install the POV's BeyondTrust Gateway
  GET    /api/pov/managed/{id}/gateway  — what PRA says about it, live
  POST   /api/pov/managed/{id}/resource-broker
  POST   /api/pov/managed/{id}/entitle-agent
                                        — configure and install the Password Safe Resource Broker
  POST   /api/pov/managed/{id}/wireup   — PRA jump item, Password Safe managed system and
                                          Entitle integration per VM
  POST   /api/pov/managed/{id}/entitle-key
                                        — the SSH key Entitle's connector authenticates with
  POST   /api/pov/managed/{id}/share    — publish a customer-facing link
  DELETE /api/pov/managed/{id}/share    — revoke it
  POST   /api/pov/managed/{id}/share/reveal
                                        — show the link's password, once, audited
  POST   /api/pov/managed/{id}/expiry   — extend, set or clear the auto-delete timer
  POST   /api/pov/managed/{id}/schedule — set or clear the suspend schedule
  POST   /api/pov/managed/{id}/spend-cap — set or clear the spend cap
  GET    /api/pov/managed/{id}/use-cases
                                        — this POV's use-case catalog, resolved against the
                                          products it is actually wired into, with progress
  POST   /api/pov/managed/{id}/use-cases/{card_id}
                                        — tick a card off (or mark it skipped)
  DELETE /api/pov/managed/{id}/use-cases/{card_id}
                                        — un-tick it
  DELETE /api/pov/managed/{id}          — destroy it and reap the platform side

The BeyondTrust tenant registry a POV is wired into lives in ``api/bt_tenants.py``, under
the same prefix and the same gate.

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
from ..config import settings
from ..services import (bt_tenant_service, config_service, expiry_policy,
                        expiry_reaper, job_service,
                        lab_platforms, pov_blueprint_service, pov_broker, pov_env_service,
                        pov_accessor_entitle, pov_gateway, pov_reconcile,
                        pov_cloud_cost, pov_entitle_agent, pov_guest_step,
                        pov_ps_config, pov_resource_broker,
                        pov_schedule, pov_share, pov_spend, pov_summary,
                        pov_use_cases, pov_wireup)
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
    # Optional once a blueprint is named: a blueprint carries the template, so requiring
    # both would make the dropdown that fills the form also mandatory to fill in by hand.
    template_id: str = ""
    name: str
    # A saved recipe to take defaults from. It supplies only fields this request left
    # blank — see pov_blueprint_service.apply_to. Deliberately NOT a second provision
    # path: everything below this line is the same code with or without it.
    blueprint_id: str = ""
    workgroup: str = ""
    project_id: str = ""
    # 0 disables it. Defaulted by the caller rather than here so the value that reaches
    # the platform is always one somebody chose.
    suspend_on_idle_seconds: int = 0
    # Which VM in the template runs the agent. Per-POV rather than a global setting:
    # templates come from wherever the SE got them, and one account can hold two that name
    # it differently. Blank means pov_broker.DEFAULT_BROKER_VM_NAME.
    broker_vm_name: str = ""
    # Which BeyondTrust tenants this POV is wired into. Blank means "not chosen yet",
    # which is allowed: the wire-up slices are what consume these, and refusing to create
    # a POV until all three are picked would make the registry a gate on a step that does
    # not need it. What is NOT allowed is a wrong one — see bt_tenant_service.
    pra_tenant_id: str = ""
    ps_tenant_id: str = ""
    entitle_tenant_id: str = ""

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
        # Which lab-platform project this environment lives in, resolved at create time
        # from the request or the configured default. Blank means the account-wide scope.
        "project_id": env.project_id or "",
        "status": env.status,
        "runstate": env.runstate or "",
        "region": env.region or "",
        # What the reconcile sweep last read off the platform. `runstate` above is only as
        # fresh as `platform_seen_at` says — the page shows that rather than implying the
        # value is live, because presenting a stale runstate as current is the exact bug
        # services/pov_reconcile exists to end.
        **pov_reconcile.describe(env),
        "workgroup": env.workgroup or "",
        "created_by": env.created_by or "",
        "created_at": env.created_at.isoformat() if env.created_at else None,
        "provision_job_id": env.provision_job_id or "",
        # The auto-delete timer. Empty means never — the same reading as everywhere else.
        "expires_at": env.expires_at.isoformat() if env.expires_at else "",
        "expiry_enabled": expiry_policy.enabled(),
        "error_message": env.error_message or "",
        "broker_vm_name": pov_broker.broker_vm_name(env),
        "pra_tenant_id": env.pra_tenant_id or "",
        "ps_tenant_id": env.ps_tenant_id or "",
        "entitle_tenant_id": env.entitle_tenant_id or "",
    }
    # The row carries its platform's answer so the page can hide the Add-VMs panel
    # instead of offering it and refusing. `/api/pov/platforms` serves the whole
    # capability map, but the detail page reads one environment, not the registry.
    out["vm_add"] = lab_platforms.supports(env.platform, "vm_add")
    if broker is not None:
        out.update(broker)
    out.update(pov_gateway.describe(_db_of(env), env))
    out.update(pov_resource_broker.describe(_db_of(env), env))
    out.update(pov_entitle_agent.describe(_db_of(env), env))
    out.update(pov_guest_step.describe(_db_of(env), env))
    out.update(pov_ps_config.describe(env))
    # Kept rather than only spread, so the use-case summary below can be resolved from the
    # numbers this row already paid for. Recomputing it would put a second per-VM query on
    # the list endpoint for counts it is holding in a local variable.
    wireup = pov_wireup.describe(_db_of(env), env)
    out.update(wireup)
    out.update(pov_share.describe(_db_of(env), env))
    # "7 of 14", on the row. `total` counts only what THIS POV can run -- a POV wired into
    # one product has most of the catalog out of scope, and a denominator of everything
    # would make a correctly scoped evaluation look like one going badly.
    out["use_cases"] = pov_use_cases.summary_for(_db_of(env), env, wireup)
    # No Request here, so the endpoint falls back to the configured public URL. That is
    # the right answer for a list rendered for an operator: the URL Entitle would be given
    # is the configured one, not whichever host this particular request arrived on.
    out.update(pov_accessor_entitle.describe(_db_of(env), env))
    if vms is not None:
        out["vms"] = [{
            "id": v.platform_vm_id,
            "name": v.name or "",
            "os_family": v.os_family or "",
            "runstate": v.runstate or "",
            "private_ip": v.private_ip or "",
            "published_services": v.published_services_list,
            # The wire-up's per-VM half. `pra_jump_tf_state` is deliberately NOT here:
            # it is a terraform state document, it is large, and it is the thing a
            # teardown needs rather than anything a browser does.
            "pra_jump_id": v.pra_jump_id or "",
            "vault_account_id": v.vault_account_id or "",
            "ps_managed_system_id": v.ps_managed_system_id or "",
            "ps_managed_account_id": v.ps_managed_account_id or "",
            "entitle_integration_id": v.entitle_integration_id or "",
            "wiring_error": v.wiring_error or "",
        } for v in vms]
    return out


def _db_of(env: PovEnvironment) -> Session:
    """The session this row is attached to.

    ``pov_gateway.describe`` needs one and ``_serialize`` is called from six places, not
    all of which have it to hand. Taking it from the object rather than threading a
    parameter through every caller keeps the signature honest: a detached row would be a
    bug anywhere this is used.
    """
    from sqlalchemy import inspect as _inspect
    return _inspect(env).session


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
    """Every lab platform this instance may use, with what it can do.

    ``capabilities`` is served to the UI rather than kept server-side so a platform that
    cannot, say, produce a share link renders "PRA only" instead of a button that 502s.

    **The list is the SELECTABLE platforms, not every platform this build knows.** A POV
    instance runs Skytap plus at most one public cloud, and the create form is rendered
    straight from this — so the one-at-a-time rule is applied once, in the registry, and
    the form cannot offer a cloud that `provision` would then refuse.
    """
    configured = set(lab_platforms.configured_platforms())
    return {
        "platforms": [
            {"name": name,
             "configured": name in configured,
             "cloud": name in lab_platforms.CLOUD_PLATFORMS,
             **lab_platforms.capabilities(name)}
            for name in lab_platforms.selectable_platforms()
        ],
        "default": _DEFAULT_PLATFORM,
        # What is built but not chosen, so Settings can say what the alternatives are
        # without the POV page having to know the difference.
        "cloud_platforms": list(lab_platforms.CLOUD_PLATFORMS),
        "selected_cloud": lab_platforms.selected_cloud(),
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


@router.get("/templates/{template_id}/vms")
async def list_template_vms(template_id: str, platform: str = Query(_DEFAULT_PLATFORM),
                            current_user: User = Depends(get_current_user)):
    """The VMs inside one template, so an operator can pick which to copy.

    The template LIST carries a count and not the VMs — a per-template VM read on a list
    of forty templates would be forty calls for a number nobody clicked on yet. This is
    the read behind the picker, taken when somebody chooses a source.
    """
    name, mod = _adapter(platform)
    if not lab_platforms.supports(name, "vm_add"):
        label = lab_platforms.capabilities(name).get("label", name)
        raise HTTPException(
            status_code=400,
            detail=f"{label} cannot add VMs to an existing environment, so there is "
                   f"nothing to pick from.")
    try:
        detail = await mod.get_template(template_id)
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"reading {name} template {template_id}") from exc
    return {"platform": name, "template_id": template_id,
            "name": detail.get("name") or "",
            "vms": detail.get("vms") or []}


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


# DECLARED BEFORE /managed/{env_id}, AND THAT IS LOAD-BEARING. Routes match in
# declaration order, so below it "archive" is captured as an environment id and this
# endpoint answers "No such POV environment" — the same trap /pov/templates and
# /pov/access carry a comment about, and the same silence when it is wrong.
@router.get("/managed/archive")
async def list_archive(limit: int = Query(pov_summary.DEFAULT_LIMIT),
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Evaluations that are over.

    ``list_managed`` above filters destroyed rows out, which is right for a page of things
    you can act on and is why a finished POV had become unreachable without its uuid. The
    record was kept and the way to it was not.
    """
    return pov_summary.archive(db, limit=limit)


@router.get("/managed/{env_id}")
async def get_managed(env_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    vms = (db.query(PovEnvironmentVM)
             .filter(PovEnvironmentVM.environment_id == env.id).all())
    return {"environment": _serialize(env, vms, broker=pov_broker.describe(db, env))}


@router.post("/managed/reconcile")
async def reconcile_now(platform: str = Query(_DEFAULT_PLATFORM),
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Read every managed environment back off the platform, now.

    Not 202 and not a job: this is one collection read and a handful of row updates, and
    the caller is a person who just pressed Refresh and is waiting to see the answer.
    Queuing it would mean the page reports success before the numbers change.

    The timer path (``services/pov_reconcile``, enqueued by ``main._pov_reconcile_loop``)
    is what keeps the rows honest when nobody is looking; this is the same function for
    when somebody is.
    """
    name, _mod = _adapter(platform)
    try:
        summary = await pov_reconcile.reconcile(db, name)
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, f"reading {name} environments back") from exc
    return {"reconciled": summary}


@router.post("/managed", status_code=202)
async def provision(payload: ProvisionRequest,
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Create a POV environment from a template.

    202, not 201: the environment does not exist yet. This returns the row and the job
    that will build it, and the job is where the progress and the failure live.
    """
    # A blueprint supplies defaults for fields this request left blank, BEFORE anything is
    # resolved — so the template id it carries is validated against the platform exactly
    # like a hand-typed one, and every refusal below reads the same either way. The request
    # still wins field by field.
    blueprint = None
    if payload.blueprint_id.strip():
        blueprint = pov_blueprint_service.get(db, payload.blueprint_id.strip())
        if blueprint is None:
            raise HTTPException(
                status_code=400,
                detail=f"no blueprint {payload.blueprint_id!r}")
        # The platform comes from the blueprint when the request did not name one other
        # than the default: a blueprint built for one platform cannot be provisioned on
        # another, and silently using the default would create it in the wrong place.
        if payload.platform == _DEFAULT_PLATFORM and blueprint.platform:
            payload.platform = blueprint.platform
        for field, value in pov_blueprint_service.apply_to(blueprint, payload).items():
            setattr(payload, field, value)

    if not str(payload.template_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail="a template id is required — pick one, or choose a blueprint that "
                   "names one")

    # One cloud at a time, enforced where a NEW environment is created and deliberately
    # nowhere else. Reads, power and destroy all go through `_adapter`, which does not ask
    # this — an operator who switches the instance from AWS to Azure must still be able to
    # see, suspend and tear down the AWS POVs they already have, and a check in the shared
    # helper would strand every one of them the moment the setting changed.
    if not lab_platforms.selectable(payload.platform):
        chosen = lab_platforms.selected_cloud()
        raise HTTPException(
            status_code=409,
            detail=(f"this instance does not build POVs on {payload.platform}. "
                    + (f"Its POV cloud provider is {chosen}."
                       if chosen else
                       "No POV cloud provider is selected.")
                    + " Change it in Settings → Integrations → POV cloud provider — a POV "
                      "instance holds one cloud's credentials at a time."))

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

    # Resolve the tenants NOW, while a bad id is still a form error. Inside the provision
    # job it would surface minutes later, against an environment that already exists — and
    # correcting a dropdown would mean destroying it first.
    try:
        tenants = bt_tenant_service.validate_selection(
            db, pra_tenant_id=payload.pra_tenant_id, ps_tenant_id=payload.ps_tenant_id,
            entitle_tenant_id=payload.entitle_tenant_id)
    except bt_tenant_service.BTTenantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Record the project the environment will REALLY be created in, not just what the form
    # sent. The adapter falls back to the configured default when the request names none,
    # so storing the raw payload would leave this column blank for an environment that is
    # in a project — a write-only field that misleads whoever reads it later.
    #
    # Resolved through the registry rather than importing skytap_service: this router must
    # stay platform-agnostic, and `mod` is whatever `_adapter()` returned.
    effective_project = (payload.project_id or "").strip()
    if not effective_project and lab_platforms.supports(name, "projects"):
        effective_project = mod.configured_project_id()

    env = PovEnvironment(
        platform=name,
        name=payload.name,
        template_id=str(payload.template_id),
        template_name=template_name,
        project_id=effective_project,
        workgroup=payload.workgroup or None,
        created_by=getattr(current_user, "username", None),
        status=pov_env_service.STATUS_PROVISIONING,
        **tenants,
    )
    # On the row rather than in the job metadata: every later broker re-run reads it, and
    # a job's metadata is the record of one run, not of the environment.
    if payload.broker_vm_name.strip():
        env.metadata_dict = {"broker_vm_name": payload.broker_vm_name.strip()}
    # The auto-delete timer, stamped at creation like every other kind. NULL when the
    # feature is off, which is what makes enabling it later act on nothing that already
    # exists. A POV gets its own much longer default than a cloud VM — see
    # expiry_policy.pov_default_hours.
    env.expires_at = expiry_policy.default_expiry_for_kind("pov")
    # A blueprint may shorten or lengthen that. Applied only when the feature is on — a
    # blueprint must not be able to stamp a timer on an instance where expiry is off, or
    # the "NULL means never, so enabling it later acts on nothing that already exists"
    # property would depend on which recipe somebody used. `clamp_hours` still applies the
    # ceiling, so a blueprint cannot outrun resource_expiry_max_total_hours either.
    if (blueprint is not None and (blueprint.expiry_hours or 0) > 0
            and env.expires_at is not None):
        env.expires_at = expiry_policy.expiry_from_now(
            expiry_policy.clamp_hours(blueprint.expiry_hours))

    # The spend cap. NULL means no cap, exactly as NULL `expires_at` means never — so an
    # instance that has not set a default stamps nothing, and turning the feature on later
    # acts on nothing that already exists.
    #
    # Only on a cloud whose prices the dashboard can actually look up: a cap on an unpriced
    # platform would accrue a confident zero and never act, which is a worse promise than
    # no cap at all.
    if pov_cloud_cost.priced(payload.platform):
        default_cap = float(getattr(settings, "pov_spend_cap_default_usd", 0.0) or 0.0)
        try:
            configured_cap = float(
                config_service.get("pov_spend_cap_default_usd") or default_cap)
        except (TypeError, ValueError):
            configured_cap = default_cap
        chosen = (blueprint.spend_cap_usd
                  if blueprint is not None and (blueprint.spend_cap_usd or 0) > 0
                  else configured_cap)
        try:
            env.spend_cap_usd = pov_spend.validate_cap(chosen)
        except pov_spend.SpendError:
            # A stored default that cannot be read is not worth failing a provision over,
            # and refusing silently would leave a POV with a cap nobody asked for.
            logger.warning("POV %s: ignoring an unusable default spend cap %r",
                           env.name, chosen)
            env.spend_cap_usd = None
    db.add(env)
    db.commit()

    # The blueprint's non-secret install configuration, copied onto the row now that it
    # exists. Nothing is run — see pov_blueprint_service — this only means the Gateway and
    # Resource Broker panels open with the fields that are the same for every POV of this
    # kind already filled in.
    prefilled = pov_blueprint_service.prefill_environment(db, blueprint, env)

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
    return {"environment": _serialize(env), "job_id": job.id,
            "blueprint": blueprint.name if blueprint else "",
            "prefilled": prefilled}


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


class TenantSelection(BaseModel):
    """Re-point a POV at different BeyondTrust tenants. Absent fields are left alone."""
    pra_tenant_id: str | None = None
    ps_tenant_id: str | None = None
    entitle_tenant_id: str | None = None


@router.post("/managed/{env_id}/tenants")
async def set_tenants(env_id: str, payload: TenantSelection,
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


class GatewayRequest(BaseModel):
    """Configure and install this POV's Gateway.

    ``name`` is the Gateway as it is named **in the customer's PRA appliance** — the
    dashboard does not create it there. ``deploy_key`` is that Gateway's key; blank leaves
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


@router.post("/managed/{env_id}/gateway", status_code=202)
async def gateway(env_id: str, payload: GatewayRequest,
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


@router.post("/managed/{env_id}/resource-broker", status_code=202)
async def resource_broker(env_id: str, payload: ResourceBrokerRequest,
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


@router.post("/managed/{env_id}/guest-step", status_code=202)
async def guest_step(env_id: str, payload: GuestStepRequest,
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


@router.post("/managed/{env_id}/ps-smart-rules/{rule_id}/process")
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


@router.post("/managed/{env_id}/vms", status_code=202)
async def add_vms(env_id: str, payload: AddVmsRequest,
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


@router.post("/managed/{env_id}/application-host")
async def application_host(env_id: str, payload: ApplicationHostRequest,
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


class EntitleAgentRequest(BaseModel):
    """Configure and install this POV's Entitle agent.

    ``vm_name`` names the Linux guest it goes on. There is no token field and there cannot
    be one: the agent token is **minted in this POV's own Entitle tenant** when the install
    is queued, because Entitle returns a token's value only at creation and a value an
    operator pasted is a value this dashboard could never destroy at teardown.
    """
    vm_name: str | None = None
    install: bool = True


@router.post("/managed/{env_id}/entitle-agent", status_code=202)
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


@router.post("/managed/{env_id}/wireup", status_code=202)
async def wireup(env_id: str, db: Session = Depends(get_db),
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
        pov_wireup.tenant_override(db, env)
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


class EntitleKeyRequest(BaseModel):
    """The private half of the SSH key baked into this POV template's Linux guests.

    Entitle's ephemeral-accounts connector authenticates with a key, not the password the
    lab platform holds — so this is the one credential the POV wire-up cannot derive from
    anything it already has. Blank clears it.
    """
    private_key: str = ""


@router.post("/managed/{env_id}/entitle-key")
async def entitle_key(env_id: str, payload: EntitleKeyRequest,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Store (or clear) this POV's Entitle SSH key. Never returns it."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    pov_wireup.set_entitle_key(env, payload.private_key)
    return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}


class ShareRequest(BaseModel):
    """How long the customer-facing link should last.

    ``days`` omitted means "decide for me" — the POV's own auto-delete date if it has one,
    otherwise ``pov_share.DEFAULT_DAYS``. There is deliberately no "never" and no password
    field: see ``services/pov_share`` for why both would be footguns rather than options.
    """
    days: int | None = None


def _share_or_400(exc: pov_share.ShareError) -> HTTPException:
    """A share refusal is about the REQUEST, so 400 rather than 502.

    Kept apart from ``_platform_error`` on purpose: "this platform has no share links" and
    "Skytap returned 500" are different problems with different fixes, and one status code
    for both is how an SE spends an afternoon on the wrong one.
    """
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/managed/{env_id}/share")
async def share(env_id: str, payload: ShareRequest,
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Publish this POV at one customer-facing URL, password-protected and time-limited.

    Synchronous rather than a job: it is a single platform call, the SE is looking at the
    result, and the generated password is returned exactly once in this response. A job
    would have to persist that password somewhere a job row could show it, which is the
    opposite of what this slice is for.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    try:
        result = await pov_share.create(db, env, days=payload.days)
    except pov_share.ShareError as exc:
        raise _share_or_400(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, "publishing the share link") from exc

    job_service.log_audit(
        db, getattr(current_user, "username", "") or "", "pov_share_created",
        target_vm=env.name,
        details={"environment_id": env.id, "share_id": result.get("share_id", ""),
                 "expires_at": result.get("share_expires_at", "")})
    return {"share": result,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.delete("/managed/{env_id}/share")
async def unshare(env_id: str, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Revoke the link without touching the environment."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    share_id = env.share_id or ""
    try:
        await pov_share.revoke(db, env)
    except pov_share.ShareError as exc:
        raise _share_or_400(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, "revoking the share link") from exc

    if share_id:
        job_service.log_audit(
            db, getattr(current_user, "username", "") or "", "pov_share_revoked",
            target_vm=env.name,
            details={"environment_id": env.id, "share_id": share_id})
    return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.post("/managed/{env_id}/share/reveal")
async def reveal_share_password(env_id: str, db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    """Show the share link's password.

    POST rather than GET, and audited, because this is the one endpoint in the router that
    hands back a live credential. A GET would be logged in referrers and browser history
    and prefetchable; an unaudited one would mean nobody could answer "who has this link's
    password" after the fact, which is the first question asked when a POV URL turns up
    somewhere it should not have.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    password = pov_share.reveal_password(env)
    if not password:
        raise HTTPException(
            status_code=404,
            detail="this POV has no stored share password — re-share it to get a new "
                   "link and password")
    job_service.log_audit(
        db, getattr(current_user, "username", "") or "", "pov_share_password_revealed",
        target_vm=env.name,
        details={"environment_id": env.id, "share_id": env.share_id or ""})
    return {"password": password}


class ExpiryRequest(BaseModel):
    """Move a POV's auto-delete timer.

    Exactly the three shapes ``/api/inventory``'s Extend control offers, and handled by
    the same service call, so a POV can never end up with looser rules than a cloud VM:
    ``extend_hours`` adds to the current deadline, ``absolute`` sets one, ``never`` clears
    it — and ``never`` is refused unless the operator has ``resource_expiry_allow_never``.
    """
    extend_hours: int | None = None
    absolute: str = ""
    never: bool = False


@router.post("/managed/{env_id}/expiry")
async def set_expiry(env_id: str, payload: ExpiryRequest,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Extend, set or clear this POV's auto-delete timer.

    Routed through ``expiry_reaper.set_expiry`` rather than writing ``expires_at`` here.
    That function owns the clamping (``max_total_hours``, the one-hour floor), the
    ``never`` permission, the audit entry and — the part that matters most for a POV —
    clearing BOTH warning latches, so the new deadline gets its own full ladder instead of
    being silenced by a rung crossed against the old one.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    if not expiry_policy.enabled():
        raise HTTPException(
            status_code=409,
            detail="the auto-delete timer is turned off for this instance "
                   "(resource_expiry_enabled).")

    # The same inventory dict the sweep and the /inventory page work from, built by the
    # one function that knows the shape. Constructing a dict by hand here is how a POV
    # would end up eligible under a rule the reaper does not actually apply.
    from ..services import inventory_service
    item = inventory_service._pov_item(env)

    result = expiry_reaper.set_expiry(
        db, [item],
        extend_hours=payload.extend_hours,
        absolute=payload.absolute or None,
        never=payload.never,
        is_admin=bool(getattr(current_user, "is_admin", False)),
        actor=getattr(current_user, "username", "") or "")
    if result["failed"]:
        raise HTTPException(status_code=400, detail=result["failed"][0]["error"])
    db.refresh(env)
    return {"expiry": result["updated"][0] if result["updated"] else None,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


# ── use cases ────────────────────────────────────────────────────────────────
#
# The one place in the persona/use-case stack that WRITES. That is worth naming, because
# services/personas opens by saying a card navigates and never starts work — a card that
# POSTed a deploy would make "curation only" false. A tick is not that: it spends nothing,
# builds nothing and touches no tenant. It also does not live in the persona layer at all;
# the registry stays a pure read, and the write lands here, behind the same auth every
# other POV action already carries.


class UseCaseRequest(BaseModel):
    # `done` by default, so the common case is an empty body.
    state: str = "done"
    # None means "leave the note alone", which is what an absent field has to mean now that
    # the customer writes notes on these rows too: an SE pressing Done sends no note, and
    # treating that as "" would erase what the customer had just written. "" still clears.
    note: str | None = None


def _env_or_404(db: Session, env_id: str) -> PovEnvironment:
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    return env


class ScheduleRequest(BaseModel):
    """A suspend schedule. Every field blank clears it."""
    suspend_at_local: str = ""
    resume_at_local: str = ""
    schedule_timezone: str = ""
    schedule_days: str = ""


@router.post("/managed/{env_id}/schedule")
async def set_schedule(env_id: str, payload: ScheduleRequest,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Set or clear this POV's suspend schedule.

    Refused on a platform that has its own idle timer. Skytap's `suspend_on_idle` and this
    answer the same question, and a POV carrying both would be one where neither is
    clearly in charge — the platform would suspend on idle, the dashboard would resume on
    a boundary, and the pair would fight quietly for the length of the evaluation.

    Setting a schedule CLEARS the evaluation latch, so the first pass after it records the
    time and acts on nothing. Without that, changing 19:00 to 22:00 at 20:00 would look
    back over a window in which the old boundary had already passed.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    if lab_platforms.supports(env.platform, "idle_suspend"):
        label = lab_platforms.capabilities(env.platform)["label"]
        raise HTTPException(
            status_code=409,
            detail=(f"{label} suspends an environment on its own idle timer, so a "
                    f"dashboard schedule would fight it. Set the idle timeout instead."))
    if not lab_platforms.supports(env.platform, "scheduled_suspend"):
        raise HTTPException(
            status_code=409,
            detail=f"{env.platform} has no scheduled suspend.")

    try:
        fields = pov_schedule.validate(
            suspend_at=payload.suspend_at_local, resume_at=payload.resume_at_local,
            tz_name=payload.schedule_timezone, days=payload.schedule_days)
    except pov_schedule.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    for field, value in fields.items():
        setattr(env, field, value)
    env.schedule_last_checked_at = None
    db.commit()
    logger.info("POV %s: schedule set by %s", env.name,
                getattr(current_user, "username", "?"))
    return {"schedule": pov_schedule.describe(env)}


class SpendCapRequest(BaseModel):
    """A spend cap in US dollars. Blank or zero clears it."""
    cap_usd: float | None = None


@router.post("/managed/{env_id}/spend-cap")
async def set_spend_cap(env_id: str, payload: SpendCapRequest,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Set or clear this POV's estimated-spend cap.

    **Raising the cap clears both latches**, so a POV that was warned — or suspended — at
    the old figure gets a fresh warning and a fresh action at the new one. Without that,
    "give it another fifty dollars" would leave the row permanently flagged and the cap
    unable to fire again. The same rule the auto-delete timer follows when an expiry is
    extended.

    The accrued total is deliberately NOT reset. It is what this POV has cost so far, and
    zeroing it on every edit would make the cap mean "another $X from now", which is a
    different and much weaker promise than the one the page makes.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    if not pov_cloud_cost.priced(env.platform):
        raise HTTPException(
            status_code=409, detail=pov_cloud_cost.no_price_reason(env.platform))

    try:
        cap = pov_spend.validate_cap(payload.cap_usd)
    except pov_spend.SpendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    previous = float(env.spend_cap_usd or 0.0)
    env.spend_cap_usd = cap
    if cap is None or cap > previous:
        env.spend_warned_at = None
        env.spend_capped_at = None
    db.commit()
    logger.info("POV %s: spend cap set to %s by %s", env.name, cap,
                getattr(current_user, "username", "?"))
    return {"spend": pov_reconcile.describe(env).get("spend", {})}


@router.get("/managed/{env_id}/use-cases")
async def list_use_cases(env_id: str, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """This POV's catalog, every role and every card, with each card's own state.

    Complete for every product mix. A POV wired into one product still gets all eight
    groups and all their cards — the mix decides each card's state, never its presence,
    which is the same promise /use-cases makes about the persona axis.
    """
    env = _env_or_404(db, env_id)
    return pov_use_cases.describe(db, env)


@router.post("/managed/{env_id}/use-cases/{card_id}")
async def set_use_case(env_id: str, card_id: str, payload: UseCaseRequest,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Tick a card off, or mark it skipped. Idempotent.

    A 400 on an unknown card id rather than a stored row: ``services/personas`` is the
    allowlist, and these rows deliberately outlive registry edits, so an unvalidated id
    would outlive the typo that made it.
    """
    env = _env_or_404(db, env_id)
    try:
        progress = pov_use_cases.set_state(
            db, env, card_id,
            state=payload.state, note=payload.note,
            by=getattr(current_user, "username", "") or "",
            by_kind=pov_use_cases.KIND_SE)
    except pov_use_cases.UseCaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"card_id": card_id, "progress": progress,
            "summary": pov_use_cases.summary_for(db, env)}


@router.delete("/managed/{env_id}/use-cases/{card_id}")
async def clear_use_case(env_id: str, card_id: str, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """Un-tick a card. Removing a row nobody wrote is success, not a 404 — the button is a
    toggle, and a double-click must not be an error."""
    env = _env_or_404(db, env_id)
    try:
        removed = pov_use_cases.clear(db, env, card_id)
    except pov_use_cases.UseCaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"card_id": card_id, "cleared": removed,
            "summary": pov_use_cases.summary_for(db, env)}


@router.get("/managed/{env_id}/summary")
async def get_summary(env_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """One evaluation's account. Serves a destroyed POV as readily as a live one.

    No status filter, deliberately: this is the endpoint written for after a POV is over,
    and refusing the finished ones would leave it describing only the POVs whose story is
    not finished being told.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    return pov_summary.build(db, env)


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
