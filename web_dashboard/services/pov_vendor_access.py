"""PRA Vendor Onboarding for one POV: a third party's way into the lab, reaped with it.

A POV has been able to put its VMs *behind* PRA since slice 6 — ``pov_wireup`` builds a
jump item per VM, through that POV's own Gateway, in the customer's own appliance. What it
could not do was give an outsider a way to **launch** one. The two paths that existed
grant something else:

* an **accessor** (``pov_accessor_service``) is a login into THIS DASHBOARD — the POV's
  checklist and nothing else;
* **Entitle** needs an Entitle tenant wired to the POV, and for the per-VM SSH path an
  agent inside the customer's network that this dashboard does not install.

So on a POV wired only to PRA the jump items were correct and unreachable. PRA already has
the mechanism — Vendor Onboarding — and this module drives it.

**Four objects, all named from the POV, all reaped with it:**

    Jump Group    pov-<name>                  ← created by pov_wireup (the scoping boundary)
    Group Policy  pov-<name>-vendor-access    ← grants sessions on that group and nothing else
    Vendor Group  pov-<name>-vendors          ← default_policy = the policy above
      └ vendor users  povvnd_<name>_<rand>

**The Jump Group is the whole reason this could not be bolted on.** A PRA Group Policy
grants access BY JUMP GROUP, and until now a POV's jump items went into the tenant's
appliance-wide group. A vendor scoped to that would reach every POV on the appliance. So a
POV whose items are still in the shared group is REFUSED here, with the reason, rather than
quietly given a policy that over-grants — see :func:`blocker`.

**The safety prefix, twice.** ``pov-`` on the two objects this module may delete, and
``povvnd_`` on the users. Same rule as ``pov_accessor_service.USERNAME_PREFIX``: a
destructive call that takes a name from a request must be unable to name somebody else's
object. A Group Policy is appliance-wide and a customer's real users depend on theirs.

**This dashboard keeps its own clock even though PRA owns expiry.** PRA takes a day count
on the Vendor Group and enforces it; ``env.pra_vendor_expires_at`` is a copy, so the row can
say when without a round trip and so :func:`sweep` can reap a group whose POV was forgotten.
Identical reasoning to ``PovAccessor.expires_at``, and for the sharper artifact.
"""
from __future__ import annotations

import logging
import math
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM, PovVendorUser
from . import config_service, job_service, pov_gateway, pov_share, pra_vendor_api
from .pra_tenant_api import PRATenantError

logger = logging.getLogger(__name__)


class VendorAccessError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# The prefix every object this module creates carries, and the only thing standing between
# `deregister` and a Group Policy the customer's own users depend on. Load-bearing.
OBJECT_PREFIX = "pov-"

# A POV name is already `^[a-z0-9][a-z0-9-]{1,62}$` (api/pov._NAME_RE), which is a subset of
# PRA's CodeName pattern `^[a-zA-Z0-9_\-]+$`. So these names need no slugging and cannot
# carry a character the appliance rejects — but see `_code_name` for the one length that
# still bites.
JUMP_GROUP_FMT = OBJECT_PREFIX + "{name}"
POLICY_NAME_FMT = OBJECT_PREFIX + "{name}-vendor-access"
VENDOR_NAME_FMT = OBJECT_PREFIX + "{name}-vendors"

# The username prefix, and the guard on every vendor-user delete. `_` rather than `-`
# matches `povguest_` and keeps the two reading as siblings in an audit log.
USERNAME_PREFIX = "povvnd_"
_RAND_CHARS = 8
_SLUG_RE = re.compile(r"[^a-z0-9]+")

DEFAULT_DAYS = 7
MAX_DAYS = 90

# What PRA is told to do with a user after their account expires. A choice, not a
# discovery: the account stops working on day `account_expiration` either way, and this
# only decides how long the dead row sits in the appliance afterwards. Seven days so an SE
# can still see who had access during a post-mortem.
DELETE_AFTER_EXPIRY_DAYS = 7


def _now() -> datetime:
    return datetime.utcnow()


def _ttl_days() -> int:
    """The configured default, clamped into range. A bad row must not grant a longer one."""
    try:
        raw = int(config_service.get("pov_vendor_ttl_days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(raw, MAX_DAYS))


# ── naming ───────────────────────────────────────────────────────────────────

def jump_group_code_name(env: PovEnvironment) -> str:
    """The Jump Group's ``code_name``.

    ``pov-`` plus a 63-character POV name is 67, and PRA's CodeName caps at 64 — so the
    long tail gets trimmed. A trim can collide with another long POV name, and that is
    deliberately left to the appliance: PRA answers "must be unique" and
    :func:`ensure_jump_group` surfaces it, which is a legible failure. Inventing a hash
    suffix here would make the group's name unreadable in the appliance to avoid a case
    that needs two POVs whose first 60 characters match.
    """
    return JUMP_GROUP_FMT.format(name=env.name or env.id)[:pra_vendor_api.CODE_NAME_MAX]


def _slug(value: str) -> str:
    out = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return out[:32] or "pov"


def _username_for(env: PovEnvironment) -> str:
    """A name that says which POV it belongs to, and cannot collide.

    Capped at PRA's ``VendorUser.username`` limit of 64 — the slug is the part that varies,
    so it is what gets trimmed, exactly as ``pov_accessor_service._username_for`` does.
    """
    rand = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789")
                   for _ in range(_RAND_CHARS))
    return f"{USERNAME_PREFIX}{_slug(env.name or env.id)}_{rand}"[:pra_vendor_api.USERNAME_MAX]


def is_vendor_username(username: str) -> bool:
    """Whether a name is one this module minted. Checked on every delete that takes a name."""
    return str(username or "").startswith(USERNAME_PREFIX)


def _ours(name: str) -> bool:
    """Whether a Vendor Group or Group Policy is one this module created."""
    return str(name or "").startswith(OBJECT_PREFIX)


# ── expiry ───────────────────────────────────────────────────────────────────

def _expiry_for(env: PovEnvironment, days: int | None) -> datetime:
    """When this POV's vendor logins stop working.

    ``pov_share._expiry_for``'s order, which ``pov_accessor_service`` also follows: an
    explicit request wins, then the default, then the POV's own expiry clamps it. The
    clamp is the step that matters — standing access into a customer's network that
    outlives the environment it reaches is a credential nobody associates with anything
    any more.
    """
    if days is not None:
        if days < 1 or days > MAX_DAYS:
            raise VendorAccessError(
                f"vendor access must last between 1 and {MAX_DAYS} days; asked for {days}")
        wanted = _now() + timedelta(days=int(days))
    else:
        wanted = _now() + timedelta(days=_ttl_days())
    if env.expires_at and env.expires_at > _now():
        # Clamped rather than refused: asking for 30 days on a POV with 5 left is a normal
        # thing to do, and the answer is 5.
        return min(wanted, env.expires_at)
    return wanted


def _days_until(when: datetime) -> int:
    """PRA takes a DAY COUNT, not a timestamp. Rounded up and floored at 1, because the
    alternative — sending 0 for a POV expiring this afternoon — is a 422 about a field
    nobody typed."""
    delta = when - _now()
    return max(pra_vendor_api.EXPIRATION_MIN,
               min(pra_vendor_api.EXPIRATION_MAX,
                   int(math.ceil(delta.total_seconds() / 86400.0))))


# ── read ─────────────────────────────────────────────────────────────────────

def _wired_vms(db: Session, env_id: str) -> int:
    return (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env_id,
                      PovEnvironmentVM.pra_jump_id.isnot(None)).count())


def blocker(db: Session, env: PovEnvironment) -> str:
    """Why this POV cannot have a Vendor Group, or "".

    Reported rather than raised so the card can show the reason where the button would be —
    the "degrade visibly rather than offering a button that fails" rule ``lab_platforms``
    and the Entitle card both follow.

    Cheapest and most likely first, and the third one is the reason this function exists.
    """
    if not env.pra_tenant_id:
        return ("this POV is not wired into a PRA tenant, so there is nowhere to create a "
                "Vendor Group. Choose one in the Tenants column first.")
    if env.status in ("destroying", "destroyed"):
        return (f"this POV is {env.status}; a vendor login for it would be access into an "
                f"environment that is going away.")
    wired = _wired_vms(db, env.id)
    if not wired:
        return ("this POV has no jump items yet, and a Vendor Group grants access to its "
                "Jump Group — which would be empty. Press Wire up first.")
    if not env.pra_jump_group_name:
        # The compatibility case, and the whole reason a per-POV Jump Group had to come
        # first. Saying it here costs a sentence; not saying it costs a vendor who can see
        # another customer's lab.
        return ("this POV's jump items are in the PRA tenant's appliance-wide Jump Group, "
                "which every POV on that appliance shares — a vendor scoped to it would "
                "reach all of them. This POV was wired before POVs had their own Jump "
                "Group. Tear down its wiring and press Wire up again to move it, or use "
                "the dashboard login below instead.")
    return ""


def describe_user(row: PovVendorUser) -> dict:
    """One vendor user, as the Access tab reads it. **Never a password.**

    The password is returned exactly once, by the call that created it, and nothing stores
    it — PRA holds only its hash and this table holds none of it. A vendor who has lost
    theirs is replaced, which is one click and leaves a trail.
    """
    return {
        "id": row.id,
        "username": row.username,
        "email": row.email or "",
        "full_name": row.full_name or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "created_by": row.created_by or "",
        "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        "expired": bool(row.expires_at and row.expires_at <= _now()),
    }


def _live_users(db: Session, env_id: str) -> list:
    return (db.query(PovVendorUser)
              .filter(PovVendorUser.environment_id == env_id,
                      PovVendorUser.revoked_at.is_(None))
              .order_by(PovVendorUser.created_at.desc()).all())


def _portal_url(db: Session, env: PovEnvironment) -> str:
    """A self-registration portal an SE built by hand, if the tenant names one.

    Read, never written. The PRA Configuration API (v1.10) has no portal, slug or
    email-domain-allow-list endpoint at all — Portal Settings live on a ``/login`` screen —
    so this dashboard cannot create one, and pretending otherwise would be a button that
    fails. See the hint on ``bt_tenant_service.OPTION_HINTS["vendor_portal_url"]``.
    """
    if not env.pra_tenant_id:
        return ""
    try:
        tenant = pov_gateway.pra_tenant(db, env)
    except Exception:  # noqa: BLE001 — describe() must never fail a page render
        return ""
    return str(tenant.option("vendor_portal_url") or "")


def describe(db: Session, env: PovEnvironment) -> dict:
    """This POV's vendor state for one row. **No network calls.**

    Merged into the POV payload beside the Entitle keys, and that endpoint renders every
    POV in the list — so one PRA round trip here would be one per row on every page load.
    """
    rows = _live_users(db, env.id)
    return {
        "vendor_group_id": env.pra_vendor_group_id or "",
        "vendor_policy_id": env.pra_vendor_policy_id or "",
        "vendor_registered": bool(env.pra_vendor_group_id),
        "vendor_jump_group": env.pra_jump_group_name or "",
        "vendor_expires_at": (env.pra_vendor_expires_at.isoformat()
                              if env.pra_vendor_expires_at else ""),
        "vendor_portal_url": _portal_url(db, env),
        "vendor_blocker": blocker(db, env),
        "vendor_users": [describe_user(r) for r in rows],
        "vendor_user_count": len(rows),
    }


# ── register ─────────────────────────────────────────────────────────────────

def _session_perms(db: Session, env: PovEnvironment) -> dict[str, bool]:
    """Which jump types the vendor policy allows, from what this POV actually built.

    Granting all of them would be simpler and would also hand a third party permission to
    start session types this POV has no jump item for — a permission that does nothing
    today and everything the moment somebody adds a Web Jump by hand.

    A blank ``os_family`` means UNKNOWN, exactly as it does in ``pov_wireup.wireable``, and
    an unknown guest grants nothing rather than guessing.
    """
    families = {
        (v.os_family or "").strip().lower()
        for v in db.query(PovEnvironmentVM).filter(
            PovEnvironmentVM.environment_id == env.id,
            PovEnvironmentVM.pra_jump_id.isnot(None)).all()}
    return {
        "perm_shell_jump": any(f and f != "windows" for f in families),
        "perm_remote_rdp": "windows" in families,
    }


async def _jump_item_role_id(tenant) -> int:
    """The Jump Item Role vendor sessions get.

    Blank on the tenant means "User's Default" (1), which is what an appliance that has
    never been customised serves. A role that is NAMED but missing is a refusal rather
    than a silent fall back to the default — the operator asked for a specific permission
    model and quietly giving them another one is the bug class this codebase calls a guard
    that fails open.
    """
    wanted = str(tenant.option("vendor_jump_item_role_name") or "").strip()
    if not wanted:
        return 1
    row = await pra_vendor_api.find_jump_item_role(tenant, wanted)
    if not row:
        raise VendorAccessError(
            f"the PRA tenant {tenant.name!r} names the Jump Item Role {wanted!r}, and the "
            f"appliance has no role by that name. Fix it on the tenant, or clear it to "
            f"use the User's Default role.")
    return int(row.get("id") or 1)


async def register(db: Session, env: PovEnvironment, *, by: str = "",
                   days: int | None = None,
                   network_restrictions: list[str] | None = None) -> dict:
    """Create (or replace) this POV's Group Policy and Vendor Group in its PRA appliance.

    **Each id is committed before the call that needs it is made.** An object that exists
    in a customer's appliance with no id on this row is one this dashboard cannot remove,
    and the whole point of the feature is that it removes them.

    **Policy before vendor, on the way in.** A Vendor Group's ``default_policy`` is
    required, so there is no other order. On the way out :func:`deregister` reverses it,
    because a policy a vendor group still references cannot be deleted.
    """
    refusal = blocker(db, env)
    if refusal:
        raise VendorAccessError(refusal)

    tenant = pov_gateway.pra_tenant(db, env)   # the canonical resolver, not a second copy

    if env.pra_vendor_group_id or env.pra_vendor_policy_id:
        # Best-effort: a deregister that fails must not stop the new registration, or a POV
        # whose group was deleted in the PRA UI could never be re-registered. Same reasoning
        # and same shape as pov_accessor_entitle.register.
        try:
            await deregister(db, env, by=by)
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not remove the previous vendor group before "
                           "re-registering", env.id, exc_info=True)

    if not env.pra_jump_group_id:
        # blocker() proved the POV has a Jump Group NAME; the id is what deletes and what
        # a membership references, so a name with no id is a wire-up that half-landed.
        raise VendorAccessError(
            f"this POV's Jump Group {env.pra_jump_group_name!r} has no id recorded, so a "
            f"Group Policy cannot be scoped to it. Re-run Wire up.")

    # Same rule as `pov_wireup.ensure_jump_group`: a same-named group in the appliance
    # that this row knows nothing about is refused, never adopted. It matters here for a
    # case the Jump Group one does not have — a POV row restored from a backup, or a
    # register that created the group and died before its commit, both leave an appliance
    # holding a live vendor group that this dashboard would otherwise duplicate or,
    # worse, quietly start handing logins to.
    #
    # Compared against the id on the row rather than merely "is there one?", because the
    # deregister above is best-effort: when it fails, the POV's OWN group is still live and
    # a bare existence check would refuse it as a stranger — telling the operator this POV
    # has no record of a group whose id is on this very row. Same comparison
    # `pov_wireup.ensure_jump_group` makes, for the same reason.
    wanted_name = VENDOR_NAME_FMT.format(name=env.name)
    existing = await pra_vendor_api.find_vendor(tenant, wanted_name)
    if existing is not None:
        found_id = str(existing.get("id") or "")
        if found_id and found_id == (env.pra_vendor_group_id or ""):
            raise VendorAccessError(
                f"this POV's vendor group {wanted_name!r} (id {found_id}) is still live in "
                f"PRA and could not be removed before re-creating it. Try again, or remove "
                f"it in PRA first.")
        raise VendorAccessError(
            f"PRA already holds a vendor group named {wanted_name!r} (id {found_id}) that "
            f"this POV has no record of. It will not adopt one — whoever is already in it "
            f"would get access. Remove it in PRA, or rename this POV.")

    role_id = await _jump_item_role_id(tenant)
    expires = _expiry_for(env, days)

    policy = await pra_vendor_api.create_group_policy(
        tenant,
        name=POLICY_NAME_FMT.format(name=env.name),
        perms=_session_perms(db, env),
        default_jump_item_role_id=role_id)
    # An id that came back blank is checked BEFORE the column is written. Committing `""`
    # and then raising is worse than not committing at all: `""` is falsy, so the retry
    # path above does not see this POV as registered, skips the deregister, and creates a
    # SECOND policy — leaving the first orphaned in the customer's appliance with nothing
    # referencing it.
    policy_id = str(policy.get("id") or "")
    if not policy_id:
        raise VendorAccessError(
            "PRA created the Group Policy and returned no id for it, so it cannot be "
            "scoped or removed from here. Remove it in PRA before trying again.")
    env.pra_vendor_policy_id = policy_id
    db.commit()

    # The single call that IS the scoping: one membership, one Jump Group, one POV.
    await pra_vendor_api.add_policy_jump_group(
        tenant, policy_id, env.pra_jump_group_id)

    vendor = await pra_vendor_api.create_vendor(
        tenant,
        name=wanted_name,
        policy_id=policy_id,
        account_expiration=_days_until(expires),
        deletion_days_after_expiration=DELETE_AFTER_EXPIRY_DAYS,
        network_restrictions=network_restrictions)
    # The same check, and here it is the sharper one: a blank id stored silently would
    # leave `describe` reporting "not registered" and `teardown` removing nothing, while a
    # live vendor group sits in the customer's appliance with a policy pointed at this
    # POV's Jump Group. That is precisely the standing third-party access this module
    # exists to reap, created invisibly.
    vendor_id = str(vendor.get("id") or "")
    if not vendor_id:
        raise VendorAccessError(
            f"PRA created the vendor group {wanted_name!r} and returned no id for it, so "
            f"this dashboard cannot remove it later. Remove it in PRA before trying "
            f"again — the group policy it uses is left in place for you to find it by.")
    env.pra_vendor_group_id = vendor_id
    env.pra_vendor_expires_at = expires
    db.commit()

    job_service.log_audit(
        db, by or "system", "pov_vendor_group_registered", target_vm=env.name,
        details={"environment_id": env.id,
                 "vendor_group_id": env.pra_vendor_group_id,
                 "policy_id": env.pra_vendor_policy_id,
                 "jump_group": env.pra_jump_group_name or "",
                 "expires_at": expires.isoformat(),
                 "tenant": tenant.name})
    logger.info("POV %s: registered vendor group %s (policy %s) in tenant %s",
                env.id, env.pra_vendor_group_id, env.pra_vendor_policy_id, tenant.name)
    return describe(db, env)


# ── deregister ───────────────────────────────────────────────────────────────

async def deregister(db: Session, env: PovEnvironment, *, by: str = "") -> None:
    """Remove the Vendor Group and its Group Policy, in that order.

    **The order is a constraint, not a preference:** a Group Policy that a Vendor Group
    still names as its ``default_policy`` cannot be deleted.

    Each column is cleared only after its delete returns. Clearing optimistically is how an
    object in a customer's appliance becomes unreachable from here — and this one is
    standing access for a third party, so "untidy" and "dangerous" are the same word.
    """
    if not (env.pra_vendor_group_id or env.pra_vendor_policy_id):
        return

    tenant = pov_gateway.pra_tenant(db, env)
    was_group = env.pra_vendor_group_id or ""
    was_policy = env.pra_vendor_policy_id or ""

    if was_group:
        # The prefix guard. A row edited by hand, or a future bug that writes an
        # operator's group id into this column, must not be able to delete a Vendor Group
        # the customer's own third parties depend on.
        live = await pra_vendor_api.get_vendor(tenant, was_group)
        if live is None:
            logger.info("POV %s: vendor group %s already gone from PRA", env.id, was_group)
        elif not _ours(str(live.get("name") or "")):
            raise VendorAccessError(
                f"PRA's vendor group {was_group} is named {live.get('name')!r}, which this "
                f"dashboard did not create. It will not delete it. Clear the reference by "
                f"hand if this POV should no longer claim it.")
        else:
            await pra_vendor_api.delete_vendor(tenant, was_group)
        env.pra_vendor_group_id = None
        env.pra_vendor_expires_at = None
        db.commit()

    if was_policy:
        await pra_vendor_api.delete_group_policy(tenant, was_policy)
        env.pra_vendor_policy_id = None
        db.commit()

    # Deleting the vendor group deletes its users on PRA's side ("all users associated
    # with the vendor group will also be deleted"), so the rows here are stamped rather
    # than left claiming live logins.
    stamped = 0
    for row in _live_users(db, env.id):
        row.revoked_at = _now()
        row.revoke_reason = "vendor group removed"
        stamped += 1
    if stamped:
        db.commit()

    job_service.log_audit(
        db, by or "system", "pov_vendor_group_removed", target_vm=env.name,
        details={"environment_id": env.id, "vendor_group_id": was_group,
                 "policy_id": was_policy, "users_stamped": stamped})
    logger.info("POV %s: removed vendor group %s and policy %s",
                env.id, was_group, was_policy)


async def teardown(db: Session, env: PovEnvironment) -> str:
    """Remove the vendor group on the destroy path. Returns a job-log line, or "".

    Never raises, and it runs **first** among the removals — ahead of even the Entitle
    integration. The destroy already orders its steps by how sharp the credential is, and
    of the doors a POV holds open this is the only one whose holder is neither in this
    account nor in this dashboard: it is a live session into the customer's network.
    """
    if not (env.pra_vendor_group_id or env.pra_vendor_policy_id):
        return ""
    was = env.pra_vendor_group_id or "(no id recorded)"
    try:
        await deregister(db, env, by="system")
        return (f"removed the PRA vendor group ({was}) and its group policy from the "
                f"customer's appliance")
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: vendor group teardown failed", env.id, exc_info=True)
        # The ids are KEPT so a re-run can finish. The same rule the per-VM wire-up
        # teardown follows, and the stakes are higher here.
        return (f"WARNING: the PRA vendor group ({was}) could not be removed from the "
                f"customer's appliance ({type(exc).__name__}). It is standing access for a "
                f"third party — delete it in PRA, or fix the tenant and re-run the destroy.")


# ── vendor users ─────────────────────────────────────────────────────────────

async def mint_user(db: Session, env: PovEnvironment, *, email: str = "",
                    full_name: str = "", by: str = "") -> tuple:
    """Create one vendor login. Returns ``(PovVendorUser, password)``.

    **PRA has no invite call.** There is no endpoint that emails a vendor a link — the
    notification settings on a Vendor Group email the PRA-side administrators, not the
    vendor. So the dashboard mints the password and hands it over, exactly as the accessor
    card does, and insists on ``password_reset_next_login`` so it stops working as soon as
    the vendor has used it once.

    The password is returned here and nowhere else; nothing stores it.
    """
    if not env.pra_vendor_group_id:
        raise VendorAccessError(
            "this POV has no PRA vendor group yet. Register one above first.")
    refusal = blocker(db, env)
    if refusal:
        raise VendorAccessError(refusal)

    tenant = pov_gateway.pra_tenant(db, env)
    username = _username_for(env)
    password = pov_share.generate_password()

    try:
        user_id = await pra_vendor_api.create_vendor_user(
            tenant, env.pra_vendor_group_id,
            username=username, password=password,
            email=(email or "").strip()[:256],
            display_name=(full_name or "").strip()[:64])
    except PRATenantError as exc:
        raise VendorAccessError(str(exc)) from None

    row = PovVendorUser(
        environment_id=env.id, pra_vendor_user_id=user_id, username=username,
        email=(email or "").strip()[:256] or None,
        full_name=(full_name or "").strip()[:200] or None,
        # A copy of the group's clock, not a second one — PRA's VendorUser.account_expiration
        # is read-only and derived from the group, so this cannot and must not differ.
        expires_at=env.pra_vendor_expires_at,
        created_by=(by or "")[:100] or None)
    db.add(row)
    db.commit()

    # Audited for the same reason revealing a share password is: this hands back a live
    # credential into a customer's network, and "who could reach this POV" has to be
    # answerable afterwards.
    job_service.log_audit(
        db, by or "system", "pov_vendor_user_minted", target_vm=env.name,
        details={"environment_id": env.id, "vendor_user_id": user_id,
                 "username": username, "email": row.email or "",
                 "expires_at": row.expires_at.isoformat() if row.expires_at else ""})
    logger.info("POV %s: minted vendor user %s in group %s",
                env.id, username, env.pra_vendor_group_id)
    return row, password


def get_user(db: Session, user_row_id: str) -> PovVendorUser | None:
    return db.query(PovVendorUser).filter(PovVendorUser.id == user_row_id).first()


async def revoke_user(db: Session, env: PovEnvironment, row: PovVendorUser, *,
                      by: str = "", reason: str = "") -> None:
    """Delete one vendor login from PRA and stamp the row. Idempotent.

    Two guards, and they are different on purpose. The row must belong to THIS POV — the
    caller checks that and answers 404 — and the username must carry :data:`USERNAME_PREFIX`,
    which is checked here. A vendor user's identity is an email the customer chose, so the
    prefix is the only thing that distinguishes an account this dashboard created from one
    an operator added to the same group by hand.
    """
    if row.revoked_at:
        return
    if not is_vendor_username(row.username):
        raise VendorAccessError(
            f"{row.username!r} does not carry the {USERNAME_PREFIX} prefix, so this "
            f"dashboard did not create it and will not delete it from PRA.")

    if env.pra_vendor_group_id and row.pra_vendor_user_id:
        tenant = pov_gateway.pra_tenant(db, env)
        await pra_vendor_api.delete_vendor_user(
            tenant, env.pra_vendor_group_id, row.pra_vendor_user_id)

    row.revoked_at = _now()
    row.revoke_reason = (reason or "revoked")[:255]
    db.commit()
    job_service.log_audit(
        db, by or "system", "pov_vendor_user_revoked", target_vm=env.name,
        details={"environment_id": env.id, "username": row.username,
                 "vendor_user_id": row.pra_vendor_user_id or ""})
    logger.info("POV %s: revoked vendor user %s", env.id, row.username)


# ── the backstop ─────────────────────────────────────────────────────────────

async def sweep(db: Session) -> int:
    """Remove vendor groups whose clock has run out, or whose POV is gone. Returns a count.

    PRA expires the *users* on its own schedule, so this is not what stops a vendor logging
    in — the appliance does that. What it removes is the GROUP, which otherwise stands in a
    customer's appliance forever naming a POV that no longer exists, with a policy pointing
    at a Jump Group somebody will eventually wonder about.

    Never raises: it rides the reconcile pass, and a pass that fails because one customer's
    appliance is unreachable stops reconciling every other POV.
    """
    rows = (db.query(PovEnvironment)
              .filter(PovEnvironment.pra_vendor_group_id.isnot(None))
              .all())
    done = 0
    for env in rows:
        expired = bool(env.pra_vendor_expires_at
                       and env.pra_vendor_expires_at <= _now())
        if not (expired or env.status == "destroyed"):
            continue
        try:
            await deregister(db, env, by="pov-vendor-sweep")
            done += 1
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: vendor group sweep could not remove %s",
                           env.id, env.pra_vendor_group_id, exc_info=True)
    return done
