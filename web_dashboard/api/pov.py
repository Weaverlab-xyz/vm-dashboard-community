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
  POST   /api/pov/managed/{id}/jump-group/move
                                        — rebuild the jump items of a POV wired before
                                          per-POV Asset Groups in one of its own
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
import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM, User, get_db
from ..config import settings
from ..services import (bt_tenant_service, config_service, expiry_policy,
                        job_service,
                        lab_platforms, pov_blueprint_service, pov_broker, pov_env_service,
                        pov_accessor_entitle, pov_cell_roles, pov_gateway,
                        pov_platform_cache,
                        pov_reconcile,
                        pov_cloud_cost, pov_engagement, pov_entitle_agent,
                        pov_guest_step,
                        pov_ot_adapter,
                        pov_pra_ps_link, pov_ps_config, pov_ready,
                        pov_resource_broker,
                        pov_setup_steps,
                        pov_share, spend_policy,
                        pov_summary,
                        pov_use_case_scope, pov_use_cases, pov_vendor_access,
                        pov_wireup)
from . import pov_gates
from .auth import (get_current_user, has_explicit_permission, has_permission,
                   pov_env_scope, require_permission, require_pov_env_access,
                   require_pov_read)

logger = logging.getLogger(__name__)

# Two router-level guards, so neither is a thing a new route has to remember:
#
#   pov:read          -- the feature-area gate. Every route here needs at least read;
#                        mutating routes add their own level below. `pov_own:read` passes
#                        it too, and narrows what the instance gate lets through to the
#                        caller's OWN POVs -- see auth.pov_owns_only.
#   require_pov_env_access -- the INSTANCE gate. Refuses (404) any {env_id} this user was
#                        not granted. See User.pov_env_ids.
#
# Before this, every route in this module was `get_current_user` only: any authenticated
# user could read, tick, wire, power, share, mint accessors for and destroy every POV in
# the install. `env.workgroup` was read to stamp job rows, never to authorize.
router = APIRouter(
    prefix="/api/pov",
    tags=["pov"],
    dependencies=[Depends(require_pov_read),
                  Depends(require_pov_env_access)],
)

# Shorthands for the per-route level bumps. `use` is the POV's own stakeholder: tick a use
# case. It deliberately does NOT include create, destroy, share, power or accessor minting
# -- that is what makes "read access to just this POC's POV, so they can check off use
# cases" expressible. Power stays on `write` because the route takes a runstate and so
# stops and suspends as readily as it starts; the start-only wake is the accessor's
# /self/wake, which names no environment because its session already does.
_POV_WRITE = [Depends(require_permission("pov", "write"))]

# Create, and write/destroy on ONE POV: the general level, or `pov_own` on a POV the caller
# owns (created by them, or assigned in their picker). See api/pov_gates.py. Routes that act
# on no single POV -- reconcile, the raw platform listing -- stay on plain `_POV_WRITE`: a
# presenter's own POVs are not a licence to operate on the whole platform.
_POV_CREATE = [Depends(pov_gates.require_create)]
_POV_WRITE_OWN = [Depends(pov_gates.require_write_on_env)]
_POV_DELETE_OWN = [Depends(pov_gates.require_delete_on_env)]
_POV_USE = [Depends(require_permission("pov", "use"))]

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
    # it differently.
    #
    # Blank means AUTO-DETECT -- the only Linux VM in the template, see
    # pov_broker.resolve_broker_candidate -- and is stored as blank rather than coerced to
    # the conventional name, because a stored default is indistinguishable from a typed
    # one and would switch auto-detection off for every POV.
    broker_vm_name: str = ""
    # Which BeyondTrust tenants this POV is wired into. Blank means "not chosen yet",
    # which is allowed when building by hand: the wire-up slices are what consume these,
    # and refusing to create a POV until all three are picked would make the registry a
    # gate on a step that does not need it. A BLUEPRINT build is the exception -- it must
    # name one for every product it tests, see `provision`. What is never allowed is a
    # wrong one — see bt_tenant_service.
    pra_tenant_id: str = ""
    ps_tenant_id: str = ""
    entitle_tenant_id: str = ""
    # Which use cases this POV tests: a subset of pra / password_safe / entitle, plus any
    # card ids unticked within it. None = not chosen yet, which the Setup tab's first step
    # then asks for -- see services/pov_use_case_scope.
    use_case_products: list[str] | None = None
    use_case_excluded: list[str] = []

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
        # What was TYPED, so "" on the wire means auto-detect. The defaulted reader
        # would report "broker" for a POV nobody named, and the page would then render a
        # value the operator never chose as though they had.
        "broker_vm_name": pov_broker.stored_broker_vm_name(env),
        "pra_tenant_id": env.pra_tenant_id or "",
        "ps_tenant_id": env.ps_tenant_id or "",
        "entitle_tenant_id": env.entitle_tenant_id or "",
    }
    # The row carries its platform's answer so the page can hide the Add-VMs panel
    # instead of offering it and refusing. `/api/pov/platforms` serves the whole
    # capability map, but the detail page reads one environment, not the registry.
    out["vm_add"] = lab_platforms.supports(env.platform, "vm_add")
    # Likewise for the per-VM Login column, which means two different things: on a
    # platform that holds VM credentials it chooses between them (a username only); on one
    # that holds none (AWS, GCP, OCI) it IS the login, and the page adds a password box.
    out["stored_credentials"] = lab_platforms.supports(env.platform, "stored_credentials")
    manual_login = not out["stored_credentials"]
    if broker is not None:
        out.update(broker)
    out.update(pov_gateway.describe(_db_of(env), env))
    out.update(pov_resource_broker.describe(_db_of(env), env))
    out.update(pov_entitle_agent.describe(_db_of(env), env))
    out.update(pov_ot_adapter.describe(env))
    out.update(pov_guest_step.describe(_db_of(env), env))
    out.update(pov_ps_config.describe(env))
    out.update(pov_pra_ps_link.describe(env))
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
    # Which use cases this POV tests, for the Setup tab's first step and its picker. The
    # raw column travels too, because the ladder reads the serialised row, not the model.
    out["use_case_scope"] = pov_use_case_scope.describe(env)
    out["use_case_products"] = env.use_case_products or ""
    # No Request here, so the endpoint falls back to the configured public URL. That is
    # the right answer for a list rendered for an operator: the URL Entitle would be given
    # is the configured one, not whichever host this particular request arrived on.
    out.update(pov_accessor_entitle.describe(_db_of(env), env))
    # The PRA vendor group, beside it. No Request argument and no network call: this runs
    # once per row on the list endpoint, so a round trip here would be one per POV on every
    # page load — see the service's describe.
    out.update(pov_vendor_access.describe(_db_of(env), env))
    if vms is not None:
        out["vms"] = [{
            "id": v.platform_vm_id,
            "name": v.name or "",
            # EFFECTIVE, so every existing reader — the opt-in checkbox, the accessor
            # page, the wire-up panel — sees an operator's override without knowing it
            # exists. The platform's own answer and the override travel beside it so the
            # VMs tab can render the control and say what it is overriding.
            "os_family": v.guest_os,
            "os_family_reported": v.os_family or "",
            "os_family_override": v.os_family_override or "",
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
            # Which stored platform credential this guest's runs use when its box holds
            # several. Blank is the normal state and means "choose by guest OS". A
            # username, never a credential — see PovEnvironmentVM.login_username.
            "login_username": v.login_username or "",
            # Whether an operator-set password is stored, on a platform with no stored
            # credentials. A boolean, never the value -- the page shows "set", not it.
            "login_password_set": (manual_login
                                   and pov_resource_broker.has_login_password(
                                       env, v.platform_vm_id)),
            # The guest's demo-cell role and how many of its extra PRA items exist.
            # Counts only: the items' Terraform state stays server-side, like
            # pra_jump_tf_state above.
            **pov_cell_roles.describe(v),
        } for v in vms]
    # LAST, and that is load-bearing: this reads the keys every `update()` above
    # contributed. Computed any earlier it would see a half-built row and report every step
    # blocked -- which would look like a POV problem rather than an ordering one.
    out["setup"] = pov_setup_steps.describe(out)
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


# ── the lab platform's own inventory ─────────────────────────────────────────
#
# `/platforms` is the REGISTRY: which lab platforms this instance may use and what each
# one can do. It names no environment, no template and no customer -- only this install's
# own configuration -- so it stays on the router's `pov:read` rather than joining the
# three below. That is not a nicety. It is the FIRST call templates/pov/index.html makes,
# and `init()` returns early on any non-ok answer, so gating it on write meant a
# stakeholder holding {"pov": ["read","use"]} -- the persona this whole change exists for
# -- got "Could not read the lab platform registry" and never reached `load()`: no POV
# list, no use cases, nothing. The API said 403 to one call and the page died whole.
#
# The three below ARE gated on pov:WRITE, and both reasons matter.
#
# 1. They are the create form's pickers -- what templates exist, what is already on the
#    platform -- so they are only useful to somebody who may provision. `/environments`
#    deliberately includes environments this dashboard did not create, which on a shared
#    Skytap account means other people's POVs; a read-only stakeholder listing them is a
#    cross-customer name leak.
# 2. `/environments/{platform_env_id}` takes a PLATFORM environment id, which is a
#    different namespace from the PovEnvironment uuids `require_pov_env_access` holds. The
#    path param is deliberately NOT called `env_id`: the instance gate is router-level and
#    keys on `request.path_params["env_id"]`, so that name here would compare the two
#    namespaces and 404 every platform environment for any narrowed user, at any level.
#    The gate cannot tell which kind of id it is looking at, so the name is what tells it.

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
        # Whether this caller may provision, so the page can skip the two create-form
        # pickers below instead of firing them and wearing a 403 as a page error. Sent
        # from here because this is the call the page already makes before anything
        # else; asking the user endpoint would be a second round trip to learn the same
        # thing. It hides UI, it does not grant anything -- every write route still
        # checks for itself.
        "can_provision": pov_gates.may_create(current_user),
        # The platform-wide controls -- "Re-check platform" and the table of EVERY
        # environment on the platform account -- are `pov:write` only. A presenter may create
        # and run their own POVs; that is not a licence to list or reconcile everyone's.
        "can_manage_platform": has_permission(current_user, "pov", "write"),
        # Blueprints live on a DIFFERENT permission from POVs -- authoring a recipe is a
        # `pov_templates` write, and plenty of users who may build a POV may not write
        # one. Sent for the same reason and with the same caveat as `can_provision`: it
        # hides the Save-as-blueprint control rather than granting anything, and the route
        # still checks for itself. Without it that button renders for everyone and 403s
        # for some of them, which reads as a broken page rather than as a permission.
        #
        # **`has_explicit_permission`, matching the form the ROUTE is gated with.**
        # `pov_templates:write` was admin-only before the permission catalog, so
        # `api/pov_templates.py` uses `require_explicit_permission` and `write` is
        # deliberately absent from `_BACKFILL_V1_SCOPES`. The permissive predicate answers
        # True for a legacy NULL-map user -- so it would draw this button for precisely
        # the users the route then refuses, which is the failure the flag exists to stop,
        # inverted. `test_permission_catalog` pins the two forms agreeing.
        "can_save_blueprints": has_explicit_permission(
            current_user, "pov_templates", "write"),
    }


@router.get("/templates", dependencies=_POV_CREATE)
async def list_templates(platform: str = Query(_DEFAULT_PLATFORM),
                         refresh: bool = Query(False),
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """Templates a POV environment could be created from.

    Served from ``pov_platform_cache``, not from the platform — see the note above
    ``list_environments``.

    Except on a cloud: a cloud's templates are our own rows, not a platform round trip,
    and served through the cache a template saved on POV → Templates stayed missing from
    the create form and the blueprint picker until the next reconcile.
    """
    name, _mod = _adapter(platform)
    if name in lab_platforms.CLOUD_PLATFORMS:
        from ..services import pov_cloud_env
        return {"platform": name, "as_of": None, "stale": False, "note": "",
                "templates": await asyncio.to_thread(pov_cloud_env.list_templates_sync,
                                                     name)}
    return {"platform": name,
            **await _cached_listing(db, pov_platform_cache.KIND_TEMPLATES, name,
                                    "templates", refresh=refresh)}


@router.get("/templates/{template_id}/vms", dependencies=_POV_CREATE)
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


async def _cached_listing(db: Session, kind: str, platform: str, key: str, *,
                          refresh: bool) -> dict:
    """One platform listing, out of the table rather than off the platform.

    Reads through the REQUEST's session. ``get_current_user`` already depends on
    ``get_db`` and FastAPI caches that within a request, so opening a ``SessionLocal``
    here would take a second connection out of a pool of ten while the first is still
    held — the hazard ``api/dashboard.dashboard_snapshot`` carries the same note about.
    ``refresh_one`` does open its own, because it must not hold one across the listing.

    Through ``asyncio.to_thread``, like ``api/cloud_identity.sweep_now``, and not because
    one indexed SELECT is slow. These two routes are ``async def`` — they have to be, they
    await the cold-start listing — so any synchronous SQLAlchemy in the body lands on the
    event loop, and a query that is merely BLOCKED on a lock is as bad there as a slow
    one. That is the failure ``tests/test_sync_db_routes_off_the_event_loop.py`` is a
    ratchet against; it exempts a genuinely-async route like this one, which makes doing
    it by hand the only thing that covers it.

    The cold case — a row with no payload at all, on a fresh install or the first load
    after a deploy — does list synchronously, once. ``claim`` is what makes that safe: the
    first caller takes the lease and the rest are told ``in-flight`` and get the note, so
    ten SEs opening the page in the same second are one platform call and not ten. Every
    later load is one indexed query.
    """
    snap = await asyncio.to_thread(pov_platform_cache.read, db, kind, platform)

    # `have` and not `fresh`: a stale listing is served as-is and refreshed by the
    # reconcile pass. Fetching here on staleness would put a platform round trip back in
    # the request path at every TTL boundary, which is the dead stale-serve branch
    # `cache_service`'s docstring is written about.
    if refresh or not snap["have"]:
        snap = await pov_platform_cache.refresh_one(kind, platform, refresh=refresh)

    # Nothing to serve AND the platform just refused: report it, with its own words, the
    # way this router did before there was a cache. This is the one case the cache has
    # nothing better to offer, and it is the case that most needs a real answer — an empty
    # table where the truth is "Skytap rejected this API token" sends an SE to debug the
    # dashboard. The 400/409/502 ladder at the top of this section is only useful while
    # 502 still means what it says.
    if not snap["have"] and snap.get("error"):
        raise _platform_error(Exception(snap["error"]), f"listing {platform} {key}")

    return {key: snap["items"], "as_of": snap["as_of"], "stale": not snap["fresh"],
            "note": snap["note"]}


# Both listings below are served from `services/pov_platform_cache` and NOT from the
# platform. They are the two collection reads templates/pov/index.html makes, they go out
# on the ONE username and API token in Settings, and the answer is identical for every SE
# on the install — so a live read here made the call volume scale with how many people had
# the page open, against an account that answers saturation with 423 and a Retry-After.
#
# What that means for anyone reading these rows: they are last-known-good plus an `as_of`,
# refreshed by the POV reconcile pass, and a platform that is unreachable now serves the
# previous listing with a `note` rather than an error. Deliberately — a 423 must not be
# able to empty a table that was correct a minute ago.
#
# `reconcile` still lists environments LIVE for itself. Do not point it here: it decides
# whether a POV is gone from the platform, and a cached answer would flag an environment
# created since the last pass as missing.

@router.get("/environments", dependencies=_POV_WRITE)
async def list_environments(platform: str = Query(_DEFAULT_PLATFORM),
                            refresh: bool = Query(False),
                            db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    """Environments visible on the platform.

    Includes ones the dashboard did not create. That is the point of a read-only view: an
    SE's existing hand-built POVs are exactly what they want to see next to the managed
    ones.
    """
    name, _mod = _adapter(platform)
    return {"platform": name,
            **await _cached_listing(db, pov_platform_cache.KIND_ENVIRONMENTS, name,
                                    "environments", refresh=refresh)}


@router.get("/environments/{platform_env_id}", dependencies=_POV_WRITE)
async def get_environment(platform_env_id: str,
                          platform: str = Query(_DEFAULT_PLATFORM),
                          current_user: User = Depends(get_current_user)):
    """One environment, with its VMs, private IPs and published services.

    ``platform_env_id`` is the LAB PLATFORM's id (a Skytap environment id), not a
    ``PovEnvironment.id``. The name is load-bearing — see the note above the section.
    """
    name, mod = _adapter(platform)
    try:
        return {"platform": name,
                "environment": await mod.get_environment(platform_env_id)}
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(
            exc, f"reading {name} environment {platform_env_id}") from exc


# ── managed POVs: this dashboard's own inventory ─────────────────────────────

@router.get("/managed")
def list_managed(db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    q = (db.query(PovEnvironment)
           .filter(PovEnvironment.status != pov_env_service.STATUS_DESTROYED))
    # The instance gate in require_pov_env_access covers routes that NAME a POV; a list
    # has no env_id to inspect, so it filters here. Without this a narrowed user still saw
    # every POV on the page and only discovered the limit by clicking one.
    scope = pov_env_scope(current_user, db)
    if scope is not None:
        q = q.filter(PovEnvironment.id.in_(sorted(scope)))
    rows = q.order_by(PovEnvironment.created_at.desc()).all()
    # One pair of queries for the page, not a pair per row. `describe` is still the right
    # call for a single environment; this list is the one caller where the per-row cost is
    # multiplied by the inventory AND by every SE loading the page at once.
    brokers = pov_broker.describe_many(db, rows)
    # Batched for the same reason: three queries for the page, whatever its length.
    engagement = pov_engagement.describe_many(db, rows)
    return {"environments": [{**_serialize(e, broker=brokers.get(e.id)),
                              "engagement": engagement.get(e.id, {})}
                             for e in rows]}


# DECLARED BEFORE /managed/{env_id}, AND THAT IS LOAD-BEARING. Routes match in
# declaration order, so below it "archive" is captured as an environment id and this
# endpoint answers "No such POV environment" — the same trap /pov/templates and
# /pov/access carry a comment about, and the same silence when it is wrong.
@router.get("/managed/archive")
def list_archive(limit: int = Query(pov_summary.DEFAULT_LIMIT),
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Evaluations that are over.

    ``list_managed`` above filters destroyed rows out, which is right for a page of things
    you can act on and is why a finished POV had become unreachable without its uuid. The
    record was kept and the way to it was not.
    """
    return pov_summary.archive(db, limit=limit, env_ids=pov_env_scope(current_user, db))


@router.get("/managed/{env_id}")
def get_managed(env_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    vms = (db.query(PovEnvironmentVM)
             .filter(PovEnvironmentVM.environment_id == env.id).all())
    out = _serialize(env, vms, broker=pov_broker.describe(db, env))
    out["engagement"] = pov_engagement.describe(db, env)
    return {"environment": out}


@router.get("/managed/{env_id}/ready")
def get_ready(env_id: str, demo_at: str = Query(""),
              db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Is this POV ready to demo? Served from what is stored, so it never waits on a tenant.

    ``demo_at`` (ISO 8601; an offset is honoured, none means UTC) moves the window a share
    link or auto-delete date is judged against. The live refresh is ``POST .../ready/run``.
    """
    env = _env_or_404(db, env_id)
    vms = (db.query(PovEnvironmentVM)
             .filter(PovEnvironmentVM.environment_id == env.id).all())
    row = _serialize(env, vms, broker=pov_broker.describe(db, env))
    health, areas = pov_ready.health_inputs(db, env)
    when = None
    if demo_at.strip():
        from ..services.health import core as health_core
        when = health_core.parse_when(demo_at.strip())
        if when is None:
            raise HTTPException(status_code=400, detail="demo_at is not an ISO 8601 time")
    out = pov_ready.describe(row, health, areas, pov_ready.last_run(env), demo_at=when)
    out["retry_in_seconds"] = pov_ready.throttled(env)
    out["running"] = _ready_job_in_flight(db, env.id)
    # A check a warm-up booked for just before the demo (services/pov_reconcile).
    out["booked_for"] = pov_ready.booked_for(db, env.id)
    return {"ready": out}


def _ready_job_in_flight(db: Session, environment_id: str) -> str:
    """The id of a ready check for this POV that is queued or running NOW, or ""."""
    return pov_ready.job_in_flight(db, environment_id)


@router.post("/managed/{env_id}/ready/run", status_code=202, dependencies=_POV_WRITE_OWN)
def run_ready(env_id: str, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Refresh every input the ready check reads, as a job.

    A job rather than an inline await: it reads a PRA appliance, Password Safe, Entitle and
    the platform, and the slowest of those is a customer's network. The page polls the job
    and re-reads ``GET .../ready`` when it ends. Refused while one is already in flight, and
    within ``RUN_MIN_SECONDS`` of the last, so a mashed button is one run.
    """
    env = _env_or_404(db, env_id)
    if env.status == pov_env_service.STATUS_DESTROYED:
        raise HTTPException(status_code=409, detail="this POV has been destroyed")
    running = _ready_job_in_flight(db, env.id)
    if running:
        return {"job_id": running, "already_running": True}
    wait = pov_ready.throttled(env)
    if wait:
        raise HTTPException(status_code=429,
                            detail=f"checked moments ago; try again in {wait}s",
                            headers={"Retry-After": str(wait)})
    job = job_service.create_job(
        db, job_type=pov_ready.JOB_TYPE,
        created_by=getattr(current_user, "username", None),
        workgroup=env.workgroup,
        metadata={"environment_id": env.id})
    return {"job_id": job.id, "already_running": False}


@router.post("/managed/reconcile", dependencies=_POV_WRITE)
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
    # Re-check is also the operator's way to force the two cached listings, which is what
    # makes "I just fixed the token in Settings" a button rather than a ten-minute wait
    # for the next reconcile pass. Subject to `claim` like every other path — including
    # the cooldown, so mashing it at a rate-limited account does not compound the problem
    # — and never raising, because the listings are not what was asked for here.
    await pov_platform_cache.warm(name, refresh=True)
    return {"reconciled": summary}


@router.post("/managed", status_code=202, dependencies=_POV_CREATE)
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

    # The use cases, checked now for the same reason: a bad card id is a form error.
    try:
        scope = pov_use_case_scope.validate(payload.use_case_products,
                                            payload.use_case_excluded)
    except pov_use_case_scope.ScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # A blueprint build names its tenants. A blueprint holds none -- which tenant a POV
    # wires into is the per-build choice, not the recipe's -- so a build from one that left
    # a product it tests without a tenant would wire up whichever is the default, which is
    # how a customer's POV onboards into somebody else's appliance. Building by hand keeps
    # "choose later": that form is where an SE deliberately defers the choice.
    if blueprint is not None:
        missing = bt_tenant_service.missing_for_build(
            db, scope[0], {"pra_tenant_id": payload.pra_tenant_id,
                           "ps_tenant_id": payload.ps_tenant_id,
                           "entitle_tenant_id": payload.entitle_tenant_id})
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"choose a tenant for {', '.join(missing)} — a blueprint build "
                       "names the tenants it wires into")

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
    #
    # Through the service, which MERGES. Assigning the dict here was safe only because
    # this is the first thing ever written to it; the same line run second would drop
    # `rb_vm_name`, `rb_zone`, `rb_asset`, the Entitle host and `broker_error`, and there
    # is now a post-create writer for this same key.
    #
    # Blank is stored as blank, and that is the point: an empty field means AUTO-DETECT
    # (the only Linux VM), so a coerced default here would switch inference off for every
    # POV this dashboard creates.
    pov_broker.set_broker_vm_name(env, payload.broker_vm_name)
    pov_use_case_scope.write(env, *scope)
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
            env.spend_cap_usd = spend_policy.validate_cap(chosen)
        except spend_policy.SpendError:
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
    # A creator whose POV access picker is NARROWED sees only the POVs it names, so the
    # one they just made would vanish from their own list and 404 on their next click.
    # Add it. An empty picker already means "every POV", and adding one id would narrow
    # them to it, so that case is left alone. Visibility then stays in ONE place: the
    # picker, which the router gate, the list, the archive and the dashboard all read.
    picker = list(current_user.pov_env_ids_list or [])
    if picker and not current_user.is_effective_admin and env.id not in picker:
        creator = db.merge(current_user)
        creator.pov_env_ids_list = picker + [env.id]
    db.commit()
    return {"environment": _serialize(env), "job_id": job.id,
            "blueprint": blueprint.name if blueprint else "",
            "prefilled": prefilled}


@router.post("/managed/{env_id}/power", status_code=202, dependencies=_POV_WRITE_OWN)
def power(env_id: str, payload: PowerRequest,
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


def _env_or_404(db: Session, env_id: str) -> PovEnvironment:
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    return env


@router.delete("/managed/{env_id}", status_code=202, dependencies=_POV_DELETE_OWN)
def destroy(env_id: str, db: Session = Depends(get_db),
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


# The routes under /managed/{env_id}/ for wiring, VMs, handoff, scheduling and close-out live
# in sibling modules. They register on this router, so its gates cover them, and are imported
# last because each one imports names defined above.
from . import pov_closeout, pov_handoff, pov_schedule, pov_vms, pov_wiring  # noqa: E402,F401
