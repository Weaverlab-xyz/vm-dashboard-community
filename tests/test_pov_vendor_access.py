"""A POV's PRA vendor group — the refusals, the scoping, and the two safety prefixes.

What is pinned here is almost entirely about NOT over-granting and NOT losing track:

  * **A POV on the tenant's appliance-wide Jump Group is refused**, and the refusal names
    the consequence. This is the whole reason a per-POV Jump Group had to land first: a
    Group Policy grants BY JUMP GROUP, so a vendor attached to the shared one reaches
    every POV on that appliance. An over-granting button is worse than no button.
  * **The policy is created, committed, and only then referenced.** An object in a
    customer's appliance with no id on this row is one this dashboard cannot remove — and
    the whole point of the feature is that it removes them.
  * **Deregister goes group, then policy.** A Group Policy a Vendor Group still names as
    its ``default_policy`` cannot be deleted; the other order fails every time.
  * **The two prefixes.** ``pov-`` guards the Vendor Group delete — a Group Policy is
    appliance-wide and a customer's real users depend on theirs. ``povvnd_`` guards the
    user delete, because a vendor's identity is an email the customer chose and nothing
    else distinguishes an account this dashboard made from one added by hand.
  * **Expiry is clamped to the POV's own**, then converted to the day count PRA takes.
  * **``describe`` makes no network call.** It runs once per row on the POV list endpoint,
    so a round trip there is one per POV on every page load.
  * **Teardown never raises and keeps the ids**, so a re-run can finish what a broken
    appliance interrupted.

Uses a real SQLite database and a fake PRA client. No network, no FastAPI.

Runs under pytest, or standalone:  python tests/test_pov_vendor_access.py
"""
import asyncio
import io
import os
import sys
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-vendor")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (bt_tenant_service, pov_env_service,  # noqa: E402
                                    pov_vendor_access as pv)
from web_dashboard.services.pra_tenant_api import PRATenantError  # noqa: E402


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _tenant(db, **opts):
    options = {"jump_group_name": "POV", "jumpoint_name": "appliance-default"}
    options.update(opts)
    return bt_tenant_service.create(
        db, kind="pra", name=_name("pra"), base_url="acme.beyondtrustcloud.com",
        client_id="cid", secret="sekrit", created_by="t", options=options)


def _env(db, *, tenant=None, jump_group="pov-lab", jump_group_id="44",
         status=pov_env_service.STATUS_ACTIVE, **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=status,
                           gateway_name="pov-gw",
                           pra_jump_group_name=jump_group,
                           pra_jump_group_id=jump_group_id,
                           pra_tenant_id=(tenant or {}).get("id"), **kw)
    db.add(env)
    db.commit()
    return env


def _wire(db, env, *, os_family="linux"):
    """A VM with a jump item, which is what `blocker` counts."""
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"),
                             name="web01", os_family=os_family, private_ip="10.9.0.10",
                             pra_jump_id="101")
    db.add(row)
    db.commit()
    return row


class _FakePRA:
    """Records every call, in order, and answers with ids."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.calls = []

    async def find_jump_item_role(self, tenant, name):
        self.calls.append(("find_role", name))
        return self.b.get("role")

    async def create_group_policy(self, tenant, *, name, perms,
                                  default_jump_item_role_id=1):
        self.calls.append(("create_policy", name, perms, default_jump_item_role_id))
        if self.b.get("policy_raises"):
            raise PRATenantError(self.b["policy_raises"])
        return {"id": self.b["policy_id"]} if "policy_id" in self.b else {"id": 12}

    async def add_policy_jump_group(self, tenant, policy_id, jump_group_id,
                                    **kw):
        self.calls.append(("membership", policy_id, jump_group_id))

    async def create_vendor(self, tenant, *, name, policy_id, account_expiration, **kw):
        self.calls.append(("create_vendor", name, policy_id, account_expiration))
        if self.b.get("vendor_raises"):
            raise PRATenantError(self.b["vendor_raises"])
        return {"id": self.b["vendor_id"]} if "vendor_id" in self.b else {"id": 77}

    async def find_vendor(self, tenant, name):
        self.calls.append(("find_vendor", name))
        return self.b.get("existing_vendor")

    async def get_vendor(self, tenant, vendor_id):
        self.calls.append(("get_vendor", vendor_id))
        if "live_vendor" in self.b:
            return self.b["live_vendor"]
        return {"id": vendor_id, "name": "pov-anything-vendors"}

    async def delete_vendor(self, tenant, vendor_id):
        self.calls.append(("delete_vendor", vendor_id))
        if self.b.get("delete_raises"):
            raise PRATenantError(self.b["delete_raises"])

    async def delete_group_policy(self, tenant, policy_id):
        self.calls.append(("delete_policy", policy_id))

    async def create_vendor_user(self, tenant, vendor_id, *, username, password,
                                 email="", display_name=""):
        self.calls.append(("create_user", vendor_id, username, email))
        if self.b.get("user_raises"):
            raise PRATenantError(self.b["user_raises"])
        return self.b.get("user_id", "5")

    async def delete_vendor_user(self, tenant, vendor_id, user_id):
        self.calls.append(("delete_user", vendor_id, user_id))


_PATCHED = ("find_jump_item_role", "create_group_policy", "add_policy_jump_group",
            "create_vendor", "find_vendor", "get_vendor", "delete_vendor",
            "delete_group_policy", "create_vendor_user", "delete_vendor_user")


def _install(fake):
    original = {k: getattr(pv.pra_vendor_api, k) for k in _PATCHED}
    for k in original:
        setattr(pv.pra_vendor_api, k, getattr(fake, k))
    return original


def _restore(original):
    for k, val in original.items():
        setattr(pv.pra_vendor_api, k, val)


def _kinds(fake):
    return [c[0] for c in fake.calls]


# ── the blocker ladder ───────────────────────────────────────────────────────

def test_a_pov_with_no_pra_tenant_is_refused_first():
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=None)
        assert "not wired into a PRA tenant" in pv.blocker(db, env)
    finally:
        db.close()


def test_a_pov_with_no_jump_items_is_refused_naming_wire_up():
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db))
        assert "Wire up" in pv.blocker(db, env)
    finally:
        db.close()


def test_a_pov_on_the_shared_jump_group_is_refused_and_the_reason_names_the_leak():
    """The load-bearing one. A Group Policy grants BY JUMP GROUP, so a vendor attached to
    the tenant's appliance-wide group reaches every POV on that appliance."""
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db), jump_group=None, jump_group_id=None)
        _wire(db, env)
        why = pv.blocker(db, env)
        assert "appliance-wide Jump Group" in why
        assert "reach all of them" in why
        assert "Wire up again" in why
    finally:
        db.close()


def test_a_destroying_pov_is_refused():
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db), status="destroying")
        _wire(db, env)
        assert "going away" in pv.blocker(db, env)
    finally:
        db.close()


def test_a_wired_pov_with_its_own_jump_group_is_not_blocked():
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        assert pv.blocker(db, env) == ""
    finally:
        db.close()


# ── register ─────────────────────────────────────────────────────────────────

def test_register_creates_the_policy_then_the_membership_then_the_vendor_group():
    """Policy first because a Vendor Group's default_policy is required, and the
    membership before the vendor so nothing points at an unscoped policy."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        out = asyncio.run(pv.register(db, env, by="se", days=5))
        assert _kinds(fake) == ["find_vendor", "create_policy", "membership",
                               "create_vendor"]
        assert env.pra_vendor_policy_id == "12"
        assert env.pra_vendor_group_id == "77"
        assert out["vendor_registered"] is True
    finally:
        _restore(original)
        db.close()


def test_the_policy_id_is_committed_before_the_membership_is_created():
    """An object in a customer's appliance with no id on this row is one this dashboard
    cannot remove. Proven by failing the NEXT call and re-reading the row."""
    db = d.SessionLocal()

    class _Boom(_FakePRA):
        async def add_policy_jump_group(self, tenant, policy_id, jump_group_id, **kw):
            self.calls.append(("membership", policy_id, jump_group_id))
            raise PRATenantError("the appliance said no")

    fake = _Boom()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        try:
            asyncio.run(pv.register(db, env, by="se"))
            raise AssertionError("the membership failure was swallowed")
        except PRATenantError:
            pass
        db.expire_all()
        fresh = pov_env_service.get(db, env.id)
        assert fresh.pra_vendor_policy_id == "12", "the policy id was not committed first"
    finally:
        _restore(original)
        db.close()


def test_the_membership_names_this_povs_jump_group_and_no_other():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), jump_group="pov-lab", jump_group_id="44")
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        membership = [c for c in fake.calls if c[0] == "membership"][0]
        assert membership[2] == "44"
        assert len([c for c in fake.calls if c[0] == "membership"]) == 1
    finally:
        _restore(original)
        db.close()


def test_the_policy_grants_only_the_jump_types_this_pov_actually_built():
    """Granting all of them would hand a third party permission to start session types
    this POV has no item for — inert today, live the moment somebody adds a Web Jump."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env, os_family="windows")
        asyncio.run(pv.register(db, env, by="se"))
        perms = [c for c in fake.calls if c[0] == "create_policy"][0][2]
        assert perms["perm_remote_rdp"] is True
        assert perms["perm_shell_jump"] is False
    finally:
        _restore(original)
        db.close()


def test_an_unknown_guest_os_grants_nothing():
    """Blank os_family means UNKNOWN everywhere else in this feature, and a guess here is
    a permission granted for a reason nobody chose."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env, os_family="")
        asyncio.run(pv.register(db, env, by="se"))
        perms = [c for c in fake.calls if c[0] == "create_policy"][0][2]
        assert perms["perm_remote_rdp"] is False and perms["perm_shell_jump"] is False
    finally:
        _restore(original)
        db.close()


def test_a_named_jump_item_role_that_does_not_exist_is_refused_never_defaulted():
    """The operator asked for a specific permission model; quietly giving them another one
    is a guard that fails open."""
    db = d.SessionLocal()
    fake = _FakePRA(role=None)
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db, vendor_jump_item_role_name="Start Sessions"))
        _wire(db, env)
        try:
            asyncio.run(pv.register(db, env, by="se"))
            raise AssertionError("a missing Jump Item Role was accepted")
        except pv.VendorAccessError as exc:
            assert "Start Sessions" in str(exc)
            assert "no role by that name" in str(exc)
    finally:
        _restore(original)
        db.close()


def test_a_blank_jump_item_role_uses_the_users_default_and_asks_the_appliance_nothing():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        assert "find_role" not in _kinds(fake)
        assert [c for c in fake.calls if c[0] == "create_policy"][0][3] == 1
    finally:
        _restore(original)
        db.close()


def test_registering_twice_removes_the_first_group_before_creating_the_second():
    """Two live groups and one stored id is the orphan shape the tenant registry and the
    Gateway registry both learned to avoid."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        fake.calls.clear()
        asyncio.run(pv.register(db, env, by="se"))
        kinds = _kinds(fake)
        assert kinds.index("delete_vendor") < kinds.index("create_vendor")
        assert kinds.index("delete_policy") < kinds.index("create_policy")
    finally:
        _restore(original)
        db.close()


def test_a_vendor_group_of_the_same_name_this_row_knows_nothing_about_is_refused():
    """A POV row restored from a backup, or a register that died before its commit, both
    leave the appliance holding a live vendor group. Adopting it would hand this POV's
    checklist to whoever is already in it."""
    db = d.SessionLocal()
    fake = _FakePRA(existing_vendor={"id": 55, "name": "pov-x-vendors"})
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        try:
            asyncio.run(pv.register(db, env, by="se"))
            raise AssertionError("a pre-existing vendor group was adopted")
        except pv.VendorAccessError as exc:
            assert "will not adopt" in str(exc)
        assert "create_policy" not in _kinds(fake)
        assert env.pra_vendor_group_id is None
    finally:
        _restore(original)
        db.close()


def test_a_vendor_group_create_that_returns_no_id_is_refused_not_stored_blank():
    """The sharpest of the blank-id cases: stored as "", describe() would report "not
    registered" and teardown would remove nothing, while a live vendor group sits in the
    customer's appliance with a policy pointed at this POV's Jump Group."""
    db = d.SessionLocal()
    fake = _FakePRA(vendor_id=None)
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        try:
            asyncio.run(pv.register(db, env, by="se"))
            raise AssertionError("a blank vendor group id was accepted")
        except pv.VendorAccessError as exc:
            assert "returned no id" in str(exc)
        assert not env.pra_vendor_group_id
        assert pv.describe(db, env)["vendor_registered"] is False
    finally:
        _restore(original)
        db.close()


def test_a_policy_create_that_returns_no_id_does_not_commit_a_blank():
    """`""` is falsy, so a committed blank makes the retry skip its own deregister and
    create a SECOND policy, orphaning the first."""
    db = d.SessionLocal()
    fake = _FakePRA(policy_id=None)
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        try:
            asyncio.run(pv.register(db, env, by="se"))
            raise AssertionError("a blank policy id was accepted")
        except pv.VendorAccessError as exc:
            assert "returned no id" in str(exc)
        assert not env.pra_vendor_policy_id
        assert "create_vendor" not in _kinds(fake), "it carried on to the vendor group"
    finally:
        _restore(original)
        db.close()


def test_the_povs_own_live_group_is_not_reported_as_a_stranger():
    """The deregister inside register is best-effort. When it fails, the POV's OWN group is
    still live -- and a bare existence check would refuse it as somebody else's, telling
    the operator this POV has no record of a group whose id is on this very row."""
    db = d.SessionLocal()
    fake = _FakePRA(existing_vendor={"id": 77, "name": "pov-x-vendors"},
                    delete_raises="the appliance is unreachable")
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77",
                   pra_vendor_policy_id="12")
        _wire(db, env)
        try:
            asyncio.run(pv.register(db, env, by="se"))
            raise AssertionError("the still-live group was accepted")
        except pv.VendorAccessError as exc:
            assert "this POV's vendor group" in str(exc)
            assert "no record of" not in str(exc)
    finally:
        _restore(original)
        db.close()


def test_the_expiry_is_clamped_to_the_povs_own():
    """Asking for 30 days on a POV with 2 left is a normal thing to do, and the answer
    is 2 — standing access that outlives what it reaches is a credential nobody
    associates with anything."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db),
                   expires_at=datetime.utcnow() + timedelta(days=2))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se", days=30))
        days = [c for c in fake.calls if c[0] == "create_vendor"][0][3]
        assert days == 2, days
        assert env.pra_vendor_expires_at <= env.expires_at
    finally:
        _restore(original)
        db.close()


def test_a_pov_that_already_expired_still_sends_a_legal_day_count():
    """PRA's floor is 1; sending 0 is a 422 about a field nobody typed."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db),
                   expires_at=datetime.utcnow() - timedelta(hours=1))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se", days=1))
        assert [c for c in fake.calls if c[0] == "create_vendor"][0][3] >= 1
    finally:
        _restore(original)
        db.close()


def test_an_out_of_range_day_count_is_refused_in_front_of_the_operator():
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db))
        try:
            pv._expiry_for(env, 500)
            raise AssertionError("500 days was accepted")
        except pv.VendorAccessError as exc:
            assert "between 1 and" in str(exc)
    finally:
        db.close()


# ── deregister ───────────────────────────────────────────────────────────────

def test_deregister_removes_the_group_before_the_policy():
    """A Group Policy a Vendor Group still names as default_policy cannot be deleted."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        fake.calls.clear()
        asyncio.run(pv.deregister(db, env, by="se"))
        kinds = _kinds(fake)
        assert kinds.index("delete_vendor") < kinds.index("delete_policy")
        assert env.pra_vendor_group_id is None and env.pra_vendor_policy_id is None
        assert env.pra_vendor_expires_at is None
    finally:
        _restore(original)
        db.close()


def test_a_vendor_group_this_dashboard_did_not_name_is_never_deleted():
    """The pov- prefix is the only thing between this path and a Vendor Group the
    customer's own third parties depend on."""
    db = d.SessionLocal()
    fake = _FakePRA(live_vendor={"id": 77, "name": "Acme Contractors"})
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77",
                   pra_vendor_policy_id="12")
        try:
            asyncio.run(pv.deregister(db, env, by="se"))
            raise AssertionError("a foreign vendor group was deleted")
        except pv.VendorAccessError as exc:
            assert "did not create" in str(exc)
        assert "delete_vendor" not in _kinds(fake)
        assert "delete_policy" not in _kinds(fake)
        assert env.pra_vendor_group_id == "77", "the reference was cleared anyway"
    finally:
        _restore(original)
        db.close()


def test_a_group_already_gone_from_the_appliance_still_clears_the_row():
    """Otherwise an operator tidying up by hand leaves this dashboard holding an id it can
    never clear."""
    db = d.SessionLocal()
    fake = _FakePRA(live_vendor=None)
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77",
                   pra_vendor_policy_id="12")
        asyncio.run(pv.deregister(db, env, by="se"))
        assert env.pra_vendor_group_id is None
        assert "delete_vendor" not in _kinds(fake)
        assert "delete_policy" in _kinds(fake)
    finally:
        _restore(original)
        db.close()


def test_deregister_stamps_the_user_rows_because_pra_deletes_them_with_the_group():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        asyncio.run(pv.mint_user(db, env, email="a@b.c", by="se"))
        asyncio.run(pv.deregister(db, env, by="se"))
        assert pv.describe(db, env)["vendor_user_count"] == 0
    finally:
        _restore(original)
        db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_never_raises_and_keeps_the_ids_so_a_rerun_can_finish():
    db = d.SessionLocal()
    fake = _FakePRA(delete_raises="the appliance is unreachable")
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77",
                   pra_vendor_policy_id="12")
        line = asyncio.run(pv.teardown(db, env))
        assert line.startswith("WARNING")
        assert "standing access" in line
        assert env.pra_vendor_group_id == "77", "the id was cleared optimistically"
    finally:
        _restore(original)
        db.close()


def test_teardown_says_nothing_for_a_pov_that_never_had_a_vendor_group():
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db))
        assert asyncio.run(pv.teardown(db, env)) == ""
    finally:
        db.close()


def test_a_teardown_warning_carries_no_caught_exception_text():
    """Same CodeQL rule the client follows — the type name, never the message."""
    db = d.SessionLocal()
    fake = _FakePRA(delete_raises="https://acme.internal/leaky-url refused")
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77")
        line = asyncio.run(pv.teardown(db, env))
        assert "leaky-url" not in line
        assert "PRATenantError" in line
    finally:
        _restore(original)
        db.close()


# ── vendor users ─────────────────────────────────────────────────────────────

def test_a_vendor_login_carries_the_prefix_and_its_password_comes_back_once():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        row, password = asyncio.run(pv.mint_user(db, env, email="a@b.c",
                                                 full_name="Dana", by="se"))
        assert row.username.startswith(pv.USERNAME_PREFIX)
        assert len(row.username) <= 64
        assert password and len(password) > 8
        # Nothing anywhere holds it.
        assert password not in str(pv.describe_user(row))
        assert not hasattr(row, "password")
    finally:
        _restore(original)
        db.close()


def test_a_vendor_login_inherits_the_groups_clock_rather_than_keeping_its_own():
    """PRA's VendorUser.account_expiration is read-only and derived from the group, so a
    per-user date here could only ever disagree with the appliance."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se", days=3))
        row, _pw = asyncio.run(pv.mint_user(db, env, email="a@b.c", by="se"))
        assert row.expires_at == env.pra_vendor_expires_at
    finally:
        _restore(original)
        db.close()


def test_minting_before_the_group_exists_is_refused():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        try:
            asyncio.run(pv.mint_user(db, env, email="a@b.c", by="se"))
            raise AssertionError("a user was minted with no vendor group")
        except pv.VendorAccessError as exc:
            assert "Register one" in str(exc)
    finally:
        _restore(original)
        db.close()


def test_a_user_without_the_prefix_is_never_deleted_from_pra():
    """A vendor's identity is an email the customer chose, so the prefix is the only thing
    distinguishing an account this dashboard made from one added by hand."""
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77")
        row = d.PovVendorUser(environment_id=env.id, pra_vendor_user_id="5",
                              username="dana.smith", email="a@b.c")
        db.add(row)
        db.commit()
        try:
            asyncio.run(pv.revoke_user(db, env, row, by="se"))
            raise AssertionError("a foreign vendor user was deleted")
        except pv.VendorAccessError as exc:
            assert pv.USERNAME_PREFIX in str(exc)
        assert "delete_user" not in _kinds(fake)
        assert row.revoked_at is None
    finally:
        _restore(original)
        db.close()


def test_revoking_deletes_from_pra_then_stamps_the_row():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        row, _pw = asyncio.run(pv.mint_user(db, env, email="a@b.c", by="se"))
        asyncio.run(pv.revoke_user(db, env, row, by="se"))
        assert ("delete_user", "77", "5") in fake.calls
        assert row.revoked_at is not None
        assert pv.describe(db, env)["vendor_user_count"] == 0
    finally:
        _restore(original)
        db.close()


def test_revoking_twice_is_a_no_op():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db))
        _wire(db, env)
        asyncio.run(pv.register(db, env, by="se"))
        row, _pw = asyncio.run(pv.mint_user(db, env, email="a@b.c", by="se"))
        asyncio.run(pv.revoke_user(db, env, row, by="se"))
        fake.calls.clear()
        asyncio.run(pv.revoke_user(db, env, row, by="se"))
        assert fake.calls == []
    finally:
        _restore(original)
        db.close()


# ── describe, and the sweep ──────────────────────────────────────────────────

def test_describe_makes_no_network_call():
    """It runs once per row on the POV list endpoint, so a round trip here is one per POV
    on every page load. Passing IS the assertion: every client call raises."""
    db = d.SessionLocal()

    class _NoNetwork(_FakePRA):
        pass

    async def _boom(*a, **kw):
        raise AssertionError("describe reached the appliance")

    original = {k: getattr(pv.pra_vendor_api, k) for k in _PATCHED}
    for k in original:
        setattr(pv.pra_vendor_api, k, _boom)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77")
        _wire(db, env)
        out = pv.describe(db, env)
        assert out["vendor_registered"] is True
        assert out["vendor_jump_group"] == "pov-lab"
    finally:
        _restore(original)
        db.close()


def test_describe_carries_a_hand_built_portal_url_off_the_tenant():
    """Read, never written: the Configuration API has no portal endpoint at all."""
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db, vendor_portal_url="https://pra/portal/acme"))
        assert pv.describe(db, env)["vendor_portal_url"] == "https://pra/portal/acme"
    finally:
        db.close()


def test_the_sweep_removes_an_expired_group_and_one_whose_pov_is_destroyed():
    db = d.SessionLocal()
    fake = _FakePRA()
    original = _install(fake)
    try:
        stale = _env(db, tenant=_tenant(db), pra_vendor_group_id="77",
                     pra_vendor_policy_id="12",
                     pra_vendor_expires_at=datetime.utcnow() - timedelta(days=1))
        dead = _env(db, tenant=_tenant(db), pra_vendor_group_id="78",
                    pra_vendor_policy_id="13", status="destroyed",
                    pra_vendor_expires_at=datetime.utcnow() + timedelta(days=5))
        live = _env(db, tenant=_tenant(db), pra_vendor_group_id="79",
                    pra_vendor_policy_id="14",
                    pra_vendor_expires_at=datetime.utcnow() + timedelta(days=5))
        done = asyncio.run(pv.sweep(db))
        assert done >= 2
        assert stale.pra_vendor_group_id is None
        assert dead.pra_vendor_group_id is None
        assert live.pra_vendor_group_id == "79", "a live group was reaped"
    finally:
        _restore(original)
        db.close()


def test_the_sweep_never_raises_when_one_appliance_is_unreachable():
    db = d.SessionLocal()
    fake = _FakePRA(delete_raises="unreachable")
    original = _install(fake)
    try:
        env = _env(db, tenant=_tenant(db), pra_vendor_group_id="77",
                   pra_vendor_expires_at=datetime.utcnow() - timedelta(days=1))
        assert asyncio.run(pv.sweep(db)) == 0
        assert env.pra_vendor_group_id == "77"
    finally:
        _restore(original)
        db.close()


# -- the page, and the destroy order ------------------------------------------
#
# Source assertions rather than a rendered page, in the style of test_pov_accessor.py:
# what is worth pinning is that the markup and the service agree, and that the two
# orderings a human cannot see from one file are the ones the code has.

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_SVC = os.path.join(_ROOT, "web_dashboard", "services")


def _read(path):
    return io.open(path, encoding="utf-8").read()


def test_every_key_the_card_reads_is_one_describe_returns():
    """A key spelled only in the markup renders blank forever, and nothing fails."""
    db = d.SessionLocal()
    try:
        env = _env(db, tenant=_tenant(db))
        described = pv.describe(db, env)
    finally:
        db.close()
    src = _read(os.path.join(_TPL, "pov", "detail.html"))
    for key in ("vendor_registered", "vendor_blocker", "vendor_jump_group",
                "vendor_portal_url", "vendor_users", "vendor_expires_at"):
        assert key in described, f"describe() omits {key}"
        assert f"env.{key}" in src, f"the card never reads env.{key}"


def test_the_card_shows_the_blocker_instead_of_the_button():
    """This writes into a customer's PRA appliance. A button that fails there costs more
    than a sentence explaining why it is not offered."""
    src = _read(os.path.join(_TPL, "pov", "detail.html"))
    assert 'x-text="env.vendor_blocker"' in src, "the page never shows the blocker"
    assert 'x-show="!env.vendor_blocker"' in src, \
        "the controls are not hidden when something blocks them"


def test_the_card_never_makes_an_anonymous_request():
    """/api authenticates off the Authorization header and this app sets no cookie, so a
    bare fetch() is an anonymous request."""
    src = _read(os.path.join(_TPL, "pov", "detail.html"))
    for fn in ("registerVendor", "removeVendor", "mintVendorUser", "revokeVendorUser"):
        body = src.split(f"async {fn}(", 1)[1].split("\n      },", 1)[0]
        assert "this.apiFetch(" in body, f"{fn} does not go through apiFetch"
        assert "await fetch(" not in body, f"{fn} makes a bare fetch"


def test_the_vendor_group_is_torn_down_first_of_all_the_removals():
    """The destroy orders its steps by how sharp the credential is, and of the doors a POV
    holds open this is the only one whose holder is neither in this account nor in this
    dashboard."""
    src = _read(os.path.join(_SVC, "pov_env_service.py"))
    body = src.split("async def run_env_destroy(", 1)[1]
    vendor = body.index("pov_vendor_access.teardown")
    integration = body.index("pov_accessor_entitle.teardown")
    logins = body.index("pov_accessor_service.teardown")
    wireup = body.index("pov_wireup.teardown")
    assert vendor < integration < logins < wireup, (
        "destroy order is wrong: the PRA vendor group must go first, and the jump items "
        "(which now take the Jump Group with them) last")


def test_a_failed_teardown_keeps_the_ids_so_a_re_run_can_finish_it():
    src = _read(os.path.join(_SVC, "pov_vendor_access.py"))
    code = src.split("async def teardown(", 1)[1].split("\n\n\n", 1)[0]
    assert "pra_vendor_group_id = None" not in code, \
        "teardown clears the vendor group id on the failure path"
    assert "WARNING" in code, "a failed teardown says nothing in the job log"


if __name__ == "__main__":
    fns = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
