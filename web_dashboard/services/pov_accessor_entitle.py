"""Registering this POV's accessor adapter with its own Entitle tenant.

``api/pov_accessor_rest`` is the endpoint Entitle calls to mint and delete accessors. This
is the other half: telling **which Entitle tenant** about it, and removing that telling
again at teardown.

One integration per POV, not one per instance. The asset is the POV, and the tenant is the
POV's — an instance-wide integration would have to name every customer's environment as an
asset inside one tenant, which is exactly the cross-tenant shape ``install_profile`` and the
BeyondTrust tenant registry exist to prevent.

**Everything is checked before anything is created.** A registration that half-succeeds
leaves an integration in a customer's Entitle tenant that this dashboard has no state for
and therefore cannot remove, so each prerequisite is refused up front with the remedy in
the message rather than discovered by a terraform apply thirty seconds later:

  * the POV must name an Entitle tenant, and that tenant an owner and a workflow;
  * ``pov_accessor_rest_secret`` must be set — without it the adapter answers 503 to
    everything, so the integration would be created and then reject every call Entitle
    made to it, which looks like an Entitle fault and is not;
  * this instance must know its own public URL, and it must be HTTPS. Entitle calls the
    adapter from its own cloud, so an endpoint of ``localhost`` or ``http://`` is an
    integration that can never work — the same refusal, for the same reason, that the
    broker enrolment makes about the agent endpoint.

⚠️  **Entitle's Ephemeral-mode discriminator is still unconfirmed against a live tenant.**
    ``entitle_registration_service.register_rest`` records the detail: whether the API
    infers the mode from ``allow_creating_accounts`` and the route key set, as this
    assumes, or takes a discriminator of its own. After the first real registration, open
    the integration's Settings in Entitle and check the **Connection** dropdown says an
    Ephemeral option. If it says Standing, the discriminator is real and belongs in
    ``register_rest``. Until somebody has looked, the SE-driven mint on the POV page is
    the path that does not depend on the answer.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..config import settings
from ..database import PovEnvironment
from . import (config_service, entitle_registration_service, job_service, pov_wireup,
               public_url)

logger = logging.getLogger(__name__)


class AccessorEntitleError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# The path api/pov_accessor_rest is mounted at. Entitle appends /create_actor and friends
# to it, so it is the prefix and never a full route.
ADAPTER_PATH = "/api/pov/accessor/rest"

# What the integration is called in the customer's tenant. The POV's name, which is a slug
# an operator chose — deliberately not a customer name, which this database does not hold.
_NAME_FMT = "pov-accessor-{name}"


def _secret() -> str:
    return (config_service.get("pov_accessor_rest_secret")
            or getattr(settings, "pov_accessor_rest_secret", "") or "").strip()


def endpoint(request=None) -> str:
    """The adapter's public base URL, or "" when this instance does not know its own.

    ``public_url.join`` is the one reader of that origin, so this cannot disagree with the
    URL the rest of the app hands out.
    """
    return public_url.join(ADAPTER_PATH, request)


def blocker(db: Session, env: PovEnvironment, request=None) -> str:
    """Why this POV cannot register its adapter, or "".

    Reported rather than raised so the page can show the reason next to a disabled button —
    the same shape ``lab_platforms`` uses to "degrade visibly rather than offering a button
    that fails".
    """
    if not env.entitle_tenant_id:
        return ("this POV is not wired into an Entitle tenant, so there is nowhere to "
                "register the adapter. Choose one in the Tenants column first.")
    if not _secret():
        return ("pov_accessor_rest_secret is not set, so the adapter answers 503 to "
                "everything. Set it in Settings and give Entitle the same value.")
    url = endpoint(request)
    if not url:
        return ("this instance does not know its own public URL, and Entitle calls the "
                "adapter from its own cloud. Set the public base URL in Settings.")
    if not url.lower().startswith("https://"):
        return (f"the adapter would be registered at {url}, which is not HTTPS. Entitle "
                f"will not call a plaintext endpoint.")
    return ""


def describe(db: Session, env: PovEnvironment, request=None) -> dict:
    """The integration's state for one POV row. No network calls."""
    return {
        "accessor_integration_id": env.accessor_integration_id or "",
        "accessor_registered": bool(env.accessor_integration_id),
        "accessor_endpoint": endpoint(request),
        "accessor_blocker": blocker(db, env, request),
    }


# ── register ─────────────────────────────────────────────────────────────────

async def register(db: Session, env: PovEnvironment, *, by: str = "",
                   request=None) -> dict:
    """Create (or replace) this POV's accessor integration in its own Entitle tenant.

    Re-registering deregisters the old one first. Leaving it would give the POV two live
    integrations and one stored state, so the older could never be removed from here — the
    orphan shape the tenant registry and the Gateway registry both learned to avoid.
    """
    refusal = blocker(db, env, request)
    if refusal:
        raise AccessorEntitleError(refusal)

    try:
        tenant = await pov_wireup.entitle_tenant_ctx(db, env)
    except pov_wireup.WireupError as exc:
        raise AccessorEntitleError(str(exc)) from None
    if not tenant:
        raise AccessorEntitleError("this POV is not wired into an Entitle tenant")

    if env.accessor_integration_id:
        # Best-effort: a deregister that fails must not stop the new registration, or a POV
        # whose integration was deleted in the Entitle UI could never be re-registered.
        try:
            await deregister(db, env)
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not remove the previous accessor integration "
                           "before re-registering", env.id, exc_info=True)

    try:
        result = await entitle_registration_service.register_rest(
            name=_NAME_FMT.format(name=env.name)[:50],
            base_url=endpoint(request),
            shared_secret=_secret(),
            # The adapter is an internet-reachable endpoint Entitle calls directly, even
            # though the POV behind it is private. It is not the target that has to be
            # reachable — this dashboard is — so no agent, and no agent token needed.
            private=False,
            ephemeral=True,
            ctx=tenant["ctx"])
    except entitle_registration_service.EntitleRegistrationError as exc:
        raise AccessorEntitleError(
            f"Entitle refused the registration: {exc}") from None

    # The id before the state, and both before returning: an integration that exists in a
    # customer's tenant with no state here is the one this dashboard cannot remove.
    env.accessor_integration_id = str(result.get("integration_id") or "")
    env.accessor_tf_state = result.get("tf_state_json") or ""
    db.commit()

    job_service.log_audit(
        db, by or "system", "pov_accessor_integration_registered", target_vm=env.name,
        details={"environment_id": env.id,
                 "integration_id": env.accessor_integration_id,
                 "tenant": tenant["label"]})
    logger.info("POV %s: registered accessor integration %s in tenant %s",
                env.id, env.accessor_integration_id, tenant["label"])
    return describe(db, env, request)


# ── deregister ───────────────────────────────────────────────────────────────

async def deregister(db: Session, env: PovEnvironment, *, by: str = "") -> None:
    """Destroy the integration and clear what this row knows about it.

    The tenant context has to be the SAME one it was registered against — a destroy
    pointed at another authenticates fine, removes nothing, and reports success. So the
    row is cleared only after the destroy returns.
    """
    if not env.accessor_tf_state:
        # Nothing to destroy. Clear a dangling id so the row stops claiming a registration.
        if env.accessor_integration_id:
            env.accessor_integration_id = None
            db.commit()
        return

    try:
        tenant = await pov_wireup.entitle_tenant_ctx(db, env)
    except pov_wireup.WireupError as exc:
        raise AccessorEntitleError(
            f"this POV's Entitle tenant could not be resolved, so its accessor "
            f"integration cannot be removed from here: {exc}") from None

    await entitle_registration_service.deregister(
        env.accessor_tf_state, (tenant or {}).get("ctx"))

    was = env.accessor_integration_id
    env.accessor_integration_id = None
    env.accessor_tf_state = None
    db.commit()
    job_service.log_audit(
        db, by or "system", "pov_accessor_integration_removed", target_vm=env.name,
        details={"environment_id": env.id, "integration_id": was or ""})
    logger.info("POV %s: removed accessor integration %s", env.id, was)


async def teardown(db: Session, env: PovEnvironment) -> str:
    """Remove the integration on the destroy path. Returns a job-log line, or "".

    Never raises, and it runs **before** the accessor logins are deleted. That order is the
    point rather than housekeeping: while the integration is live Entitle can mint a NEW
    accessor, so removing the logins first would race a destroy against a grant and could
    leave a login created after the sweep that removed them. Shut the tap, then drain.
    """
    if not (env.accessor_integration_id or env.accessor_tf_state):
        return ""
    was = env.accessor_integration_id or "(no id recorded)"
    try:
        await deregister(db, env, by="system")
        return f"removed the accessor integration ({was}) from the customer's Entitle tenant"
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: accessor integration teardown failed", env.id,
                       exc_info=True)
        # The state is KEPT so a re-run can finish it — the same rule the per-VM wire-up
        # teardown follows. An integration left in a customer's tenant is untidy; one this
        # dashboard can no longer find is worse.
        return (f"WARNING: the accessor integration ({was}) could not be removed from the "
                f"customer's Entitle tenant ({exc}). It is standing access — remove it in "
                f"Entitle, or fix the tenant and re-run the destroy.")
