"""Minting a POV's Password Safe functional accounts — what stops one being wrong.

A functional account is the identity Password Safe authenticates to a guest AS in order to
rotate its managed account, and Password Safe derives the managed system's platform from
it. So creating one is not a convenience: it is putting a working login into a customer's
tenant and pointing N managed systems at it. What is pinned here is what keeps that from
going quietly wrong:

  * **A NAME on the tenant is the customer's account.** It is resolved and used as-is,
    nothing is created, and teardown never touches it. The blank field is the opt-in.
  * **The credential is never stored, and never named.** It is read live from the lab
    platform per run, and no refusal, log line or job message carries any part of it.
  * **Agreement is checked, not assumed.** One functional account serves every managed
    system that names it, so guests that report different logins are a refusal — minting
    from one of them would rotate that guest and fail the others days later, on a
    schedule, which is the failure this whole path is shaped to avoid.
  * **Every call names the POV's tenant.** A mint against the install's own Password Safe
    succeeds, and the POV's managed system then references an id its tenant does not have.
    This is the cross-tenant mistake the tenant registry exists to prevent.
  * **The id is recorded before anything else happens**, and only recorded ids are ever
    deleted — which is what keeps "the dashboard cleaned up after itself" from meaning
    "the dashboard deleted the account your whole tenant uses".
  * **Teardown never mints**, and never deletes an account a managed system still holds.
  * **A per-family failure costs that family only.** Linux guests that agree onboard while
    Windows guests that disagree say why.

Uses a real SQLite database and fakes for both Password Safe services and the lab
platform. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_functional_account.py
"""
import asyncio
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-fa")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (bt_tenant_service, pov_env_service,  # noqa: E402
                                    pov_functional_account as fa, pov_wireup as w)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _env(db, **kw):
    """A POV with a PRA tenant, a Password Safe tenant and a Resource Broker."""
    pra = bt_tenant_service.create(
        db, kind="pra", name=_name("pra"), base_url="acme.beyondtrustcloud.com",
        client_id="cid", secret="sekrit", created_by="t",
        options={"jump_group_name": "POV", "jumpoint_name": "gw"})
    options = {"api_account_name": "svc", "workgroup": "POV",
               "linux_functional_account": "", "windows_functional_account": ""}
    options.update(kw.pop("options", {}))
    ps = bt_tenant_service.create(
        db, kind="password_safe", name=_name("ps"),
        base_url="acme.ps.beyondtrustcloud.com", client_id="ps-cid", secret="ps-sekrit",
        created_by="t", options=options)
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE, gateway_name="pov-gw",
                           pra_tenant_id=pra["id"], ps_tenant_id=ps["id"],
                           ps_application_host_id=4242, **kw)
    db.add(env)
    db.commit()
    return env


def _vm(db, env, *, name="web01", os_family="linux", ip="10.9.0.10", vm_id=None):
    row = d.PovEnvironmentVM(environment_id=env.id,
                             platform_vm_id=vm_id or _name("vm"),
                             name=name, os_family=os_family, private_ip=ip)
    db.add(row)
    db.commit()
    return row


# The password every fake guest reports. Named once so a test can assert it appears
# NOWHERE in a message — the whole point of pov_credentials' no-quoting rule.
_SECRET = "Sup3rS3cret-Passw0rd"


class _FakePlatform:
    """The lab platform's stored-credential box, keyed by platform_vm_id."""

    def __init__(self, creds, *, raises=()):
        self.creds = creds            # {vm_id: "root / pw"} or {vm_id: [entries]}
        self.raises = set(raises)
        self.read = []

    async def stored_credentials(self, env_id, vm_id):
        self.read.append((env_id, vm_id))
        if vm_id in self.raises:
            raise RuntimeError("skytap says no")
        value = self.creds.get(vm_id, [])
        if isinstance(value, str):
            return [{"text": value, "notes": ""}]
        return value


class _FakePS:
    """Both Password Safe services, recording the tenant every call was pointed at."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.created = []
        self.deleted = []
        self.platform_lookups = []
        self.registered = []
        self.deregistered = []
        self.accounts = dict(behaviour.get("accounts") or {})

    # — REST —
    async def get_workgroup_id(self, name, tenant=None):
        return "77"

    async def get_functional_account(self, name, tenant=None):
        if str(name) in self.accounts:
            return dict(self.accounts[str(name)], tenant=tenant)
        if self.b.get("fa_missing"):
            raise RuntimeError("not found")
        return {"id": 5, "platform_id": 11, "platform_name": "Linux",
                "account_name": str(name), "tenant": tenant}

    async def get_platform_id(self, name_or_id, tenant=None):
        self.platform_lookups.append((name_or_id, tenant))
        if self.b.get("platform_missing"):
            raise RuntimeError("platform not found")
        return 11 if str(name_or_id).lower() == "linux" else 12

    async def create_functional_account_on_platform(self, **kw):
        self.created.append(kw)
        if self.b.get("create_raises"):
            raise RuntimeError(self.b["create_raises"])
        return 900 + len(self.created)

    async def delete_functional_account(self, account_id, tenant=None):
        self.deleted.append((int(account_id), tenant))
        if self.b.get("delete_raises"):
            raise RuntimeError(self.b["delete_raises"])

    # — terraform —
    async def register_managed_system(self, **kw):
        self.registered.append(kw)
        if self.b.get("register_raises"):
            raise RuntimeError(self.b["register_raises"])
        return {"managed_system_id": "800", "managed_account_id": "801",
                "tf_state_json": '{"resources":[]}', "initial_password_seeded": True}

    async def deregister(self, state, tenant=None):
        self.deregistered.append((state, tenant))
        if self.b.get("deregister_raises"):
            raise RuntimeError(self.b["deregister_raises"])


class _Installed:
    """Swaps the fakes in for the duration of a `with` block, and puts them all back."""

    def __init__(self, ps, platform=None):
        self.ps, self.platform = ps, platform

    def __enter__(self):
        self.saved = {
            "api_wg": w.ps_api_service.get_workgroup_id,
            "api_fa": w.ps_api_service.get_functional_account,
            "api_pid": fa.ps_api_service.get_platform_id,
            "api_create": fa.ps_api_service.create_functional_account_on_platform,
            "api_delete": fa.ps_api_service.delete_functional_account,
            "reg": w.ps_resource_service.register_managed_system,
            "dereg": w.ps_resource_service.deregister,
            "adapter": fa.lab_platforms.adapter,
        }
        w.ps_api_service.get_workgroup_id = self.ps.get_workgroup_id
        w.ps_api_service.get_functional_account = self.ps.get_functional_account
        fa.ps_api_service.get_platform_id = self.ps.get_platform_id
        fa.ps_api_service.create_functional_account_on_platform = \
            self.ps.create_functional_account_on_platform
        fa.ps_api_service.delete_functional_account = self.ps.delete_functional_account
        w.ps_resource_service.register_managed_system = self.ps.register_managed_system
        w.ps_resource_service.deregister = self.ps.deregister
        if self.platform is not None:
            fa.lab_platforms.adapter = lambda name: self.platform
        return self

    def __exit__(self, *exc):
        w.ps_api_service.get_workgroup_id = self.saved["api_wg"]
        w.ps_api_service.get_functional_account = self.saved["api_fa"]
        fa.ps_api_service.get_platform_id = self.saved["api_pid"]
        fa.ps_api_service.create_functional_account_on_platform = self.saved["api_create"]
        fa.ps_api_service.delete_functional_account = self.saved["api_delete"]
        w.ps_resource_service.register_managed_system = self.saved["reg"]
        w.ps_resource_service.deregister = self.saved["dereg"]
        fa.lab_platforms.adapter = self.saved["adapter"]
        return False


# ── minting ──────────────────────────────────────────────────────────────────

def test_a_blank_field_mints_an_account_from_the_guests_own_login():
    """The whole point: on a fresh POV tenant neither account exists, and the guests
    already hold the credential one would be built from."""
    db = d.SessionLocal()
    env = _env(db)
    one = _vm(db, env, name="web01")
    two = _vm(db, env, name="web02", ip="10.9.0.11")
    ps = _FakePS()
    platform = _FakePlatform({one.platform_vm_id: f"root / {_SECRET}",
                              two.platform_vm_id: f"root / {_SECRET}"})
    with _Installed(ps, platform):
        ctx = asyncio.run(w.ps_context(db, env))
    assert len(ps.created) == 1, ps.created
    created = ps.created[0]
    assert created["account_name"] == "root"
    assert created["password"] == _SECRET
    assert created["platform_id"] == 11
    # Per-POV, and that IS the uniqueness — `root` is the account name on every POV in
    # the tenant, so the display name is what distinguishes them.
    assert created["display_name"] == f"pov-{env.name}-linux"
    assert ctx["accounts"]["linux"]["id"] == 901
    assert ctx["account_problems"] == {}
    db.refresh(env)
    assert env.ps_linux_functional_account_id == 901
    db.close()


def test_the_mint_is_pointed_at_the_povs_tenant_and_not_the_singleton():
    """A create against the install's own Password Safe succeeds and puts the account in
    the wrong tenant; the POV's managed system then references an id its tenant does not
    have. The cross-tenant mistake the registry exists to prevent."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}"})):
        asyncio.run(w.ps_context(db, env))
    # Both REST calls carry it, not just the create: a platform id read from the wrong
    # tenant is a number that means something else in this one.
    for call in (ps.created[0], dict(zip(("name", "tenant"), ps.platform_lookups[0]))):
        creds = call["tenant"]
        assert creds, "a Password Safe call was made with no tenant credentials"
        assert "acme.ps.beyondtrustcloud.com" in str(creds)
        assert "ps-cid" in str(creds)
    db.close()


def test_guests_that_disagree_are_a_refusal_and_nothing_is_created():
    """One functional account serves every managed system that names it. Minting from one
    of two logins rotates that guest and fails the rest on a schedule."""
    db = d.SessionLocal()
    env = _env(db)
    one = _vm(db, env, name="web01")
    two = _vm(db, env, name="web02", ip="10.9.0.11")
    ps = _FakePS()
    platform = _FakePlatform({one.platform_vm_id: f"root / {_SECRET}",
                              two.platform_vm_id: "ubuntu / other-pw"})
    with _Installed(ps, platform):
        ctx = asyncio.run(w.ps_context(db, env))
        line = asyncio.run(w.onboard_vm(db, env, one, ps=ctx))
    assert ps.created == [], "an account was minted from guests that disagree"
    problem = ctx["account_problems"]["linux"]
    assert "2 different logins" in problem
    assert "web01" in problem and "web02" in problem
    # The VM's own line carries the specific reason, not the generic "none was named".
    assert "skipped" in line and "2 different logins" in line
    assert ps.registered == []
    db.refresh(env)
    assert env.ps_linux_functional_account_id is None
    db.close()


def test_no_refusal_ever_carries_the_credential():
    """pov_credentials' rule, which this module inherits: the parsed username is safe to
    name once parsing succeeded, and nothing else ever is."""
    db = d.SessionLocal()
    env = _env(db)
    one = _vm(db, env, name="web01")
    two = _vm(db, env, name="web02", ip="10.9.0.11")
    ps = _FakePS()
    platform = _FakePlatform({one.platform_vm_id: f"root / {_SECRET}",
                              two.platform_vm_id: f"ubuntu / {_SECRET}-two"})
    with _Installed(ps, platform):
        ctx = asyncio.run(w.ps_context(db, env))
        line = asyncio.run(w.onboard_vm(db, env, one, ps=ctx))
    for text in (ctx["account_problems"]["linux"], line):
        assert _SECRET not in text, "a refusal quoted the password"
    db.close()


def test_an_unparseable_credential_box_is_a_refusal_not_a_guess():
    """A wrong username comes back from the wire as an authentication failure, which reads
    as a bad password and sends an SE to reset one that was fine."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    ps = _FakePS()
    platform = _FakePlatform({vm.platform_vm_id: "ask dave about this one"})
    with _Installed(ps, platform):
        ctx = asyncio.run(w.ps_context(db, env))
    assert ps.created == []
    assert "linux" in ctx["account_problems"]
    db.close()


def test_a_family_with_no_guests_mints_nothing():
    """A Linux-only POV has no use for a Windows account, and minting one would leave an
    unused credential in a customer's tenant."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env, os_family="linux")
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}"})):
        ctx = asyncio.run(w.ps_context(db, env))
    assert list(ctx["accounts"]) == ["linux"]
    assert len(ps.created) == 1
    assert ctx["account_problems"] == {}
    db.refresh(env)
    assert env.ps_windows_functional_account_id is None
    db.close()


def test_a_blank_os_family_is_not_sorted_into_a_family():
    """Slice 3's rule: blank means unknown, and a confident wrong answer here would build
    a functional account from a guest that is not of that OS at all."""
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, os_family="")
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({})):
        ctx = asyncio.run(w.ps_context(db, env))
    assert ps.created == [] and ctx["accounts"] == {}
    db.close()


def test_one_familys_failure_does_not_cost_the_other():
    """Linux guests that agree should onboard while Windows guests that disagree say why.
    The same rule that makes the whole Password Safe half optional."""
    db = d.SessionLocal()
    env = _env(db)
    linux = _vm(db, env, name="web01", os_family="linux")
    win_a = _vm(db, env, name="dc01", os_family="windows", ip="10.9.0.20")
    win_b = _vm(db, env, name="dc02", os_family="windows", ip="10.9.0.21")
    ps = _FakePS()
    platform = _FakePlatform({linux.platform_vm_id: f"root / {_SECRET}",
                              win_a.platform_vm_id: f"administrator / {_SECRET}",
                              win_b.platform_vm_id: "admin2 / different"})
    with _Installed(ps, platform):
        ctx = asyncio.run(w.ps_context(db, env))
        linux_line = asyncio.run(w.onboard_vm(db, env, linux, ps=ctx))
        win_line = asyncio.run(w.onboard_vm(db, env, win_a, ps=ctx))
    assert list(ctx["accounts"]) == ["linux"]
    assert "windows" in ctx["account_problems"]
    assert "skipped" not in linux_line and "FAILED" not in linux_line
    assert "skipped" in win_line
    assert len(ps.registered) == 1
    db.close()


def test_the_managed_systems_platform_still_comes_from_the_account():
    """Minted or named, the platform is read off the functional account and never chosen
    here — the rule ps_vm_hook follows and the reason the accounts are split by OS."""
    db = d.SessionLocal()
    env = _env(db)
    linux = _vm(db, env, name="web01", os_family="linux")
    win = _vm(db, env, name="dc01", os_family="windows", ip="10.9.0.20")
    ps = _FakePS()
    platform = _FakePlatform({linux.platform_vm_id: f"root / {_SECRET}",
                              win.platform_vm_id: f"administrator / {_SECRET}"})
    with _Installed(ps, platform):
        ctx = asyncio.run(w.ps_context(db, env))
        asyncio.run(w.onboard_vm(db, env, linux, ps=ctx))
        asyncio.run(w.onboard_vm(db, env, win, ps=ctx))
    assert ps.registered[0]["platform_id"] == 11
    assert ps.registered[0]["port"] == w.SSH_PORT
    assert ps.registered[1]["platform_id"] == 12
    assert ps.registered[1]["port"] == w.RDP_PORT
    db.close()


def test_a_recorded_account_is_resolved_and_not_minted_again():
    """A re-wire must be free, and Password Safe would accept a second account for the
    same POV — which this database would then have no record of."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    env.ps_linux_functional_account_id = 555
    db.commit()
    ps = _FakePS(accounts={"555": {"id": 555, "platform_id": 11,
                                   "platform_name": "Linux", "account_name": "root"}})
    platform = _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}"})
    with _Installed(ps, platform):
        ctx = asyncio.run(w.ps_context(db, env))
    assert ps.created == []
    assert ctx["accounts"]["linux"]["id"] == 555
    # And the guests were never asked, which is what makes it free.
    assert platform.read == []
    db.close()


def test_an_account_deleted_tenant_side_is_forgotten_rather_than_refused_forever():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env)
    env.ps_linux_functional_account_id = 555
    db.commit()
    ps = _FakePS(fa_missing=True)
    with _Installed(ps, _FakePlatform({})):
        ctx = asyncio.run(w.ps_context(db, env))
    assert "linux" in ctx["account_problems"]
    db.refresh(env)
    assert env.ps_linux_functional_account_id is None, \
        "a vanished account id was kept, so every future wire-up would refuse over it"
    db.close()


def test_an_unresolvable_platform_names_the_config_key():
    """The one thing an operator can do about it, in the message rather than in a doc."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    ps = _FakePS(platform_missing=True)
    with _Installed(ps, _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}"})):
        ctx = asyncio.run(w.ps_context(db, env))
    problem = ctx["account_problems"]["linux"]
    assert "pov_ps_functional_account_platform_linux" in problem
    assert ps.created == []
    db.close()


# ── a name on the tenant ─────────────────────────────────────────────────────

def test_a_named_account_is_used_as_is_and_never_minted():
    """The blank field is the opt-in. A name is the customer's own account."""
    db = d.SessionLocal()
    env = _env(db, options={"linux_functional_account": "fa-linux"})
    vm = _vm(db, env)
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}"})):
        ctx = asyncio.run(w.ps_context(db, env))
    assert ps.created == []
    assert ctx["accounts"]["linux"]["account_name"] == "fa-linux"
    db.refresh(env)
    assert env.ps_linux_functional_account_id is None
    db.close()


def test_a_named_account_is_never_deleted_at_teardown():
    """"The dashboard cleaned up after itself" must not mean "the dashboard deleted the
    functional account your whole tenant uses"."""
    db = d.SessionLocal()
    env = _env(db, options={"linux_functional_account": "fa-linux"})
    vm = _vm(db, env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    vm.ps_managed_system_id = "800"
    db.commit()
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({})):
        line = asyncio.run(w.teardown(db, env))
    assert ps.deleted == [], "a functional account named on the tenant was deleted"
    assert "Off-boarded 1" in line
    db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_deletes_only_what_this_pov_minted_and_clears_it():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    vm.ps_managed_system_id = "800"
    env.ps_linux_functional_account_id = 901
    db.commit()
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({})):
        line = asyncio.run(w.teardown(db, env))
    assert [i for i, _ in ps.deleted] == [901]
    # Pointed at the POV's tenant — a delete against the wrong one 404s and reports the
    # account "already gone" while it is still sitting in the customer's tenant.
    assert "acme.ps.beyondtrustcloud.com" in str(ps.deleted[0][1])
    assert "Deleted the linux functional account" in line
    db.refresh(env)
    assert env.ps_linux_functional_account_id is None
    db.close()


def test_teardown_never_mints():
    """A creating read at teardown would mint an account seconds before deleting
    everything, from guests already on their way out."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    db.commit()
    ps = _FakePS()
    platform = _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}"})
    with _Installed(ps, platform):
        asyncio.run(w.teardown(db, env))
    assert ps.created == [], "teardown minted a functional account"
    assert platform.read == [], "teardown read the guests' credentials"
    db.close()


def test_a_still_referenced_account_is_left_in_place_and_said_so():
    """Password Safe refuses to delete an account a managed system holds, and that refusal
    is the correct outcome — deleting it would orphan the system's credential."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    env.ps_linux_functional_account_id = 901
    db.commit()
    ps = _FakePS(deregister_raises="still in use")
    with _Installed(ps, _FakePlatform({})):
        line = asyncio.run(w.teardown(db, env))
    assert ps.deleted == []
    assert "left in place" in line
    db.refresh(env)
    assert env.ps_linux_functional_account_id == 901, \
        "an account that was not deleted was forgotten, so nothing can clean it up"
    db.close()


def test_a_pov_with_a_minted_account_and_no_managed_system_is_still_cleaned_up():
    """The shape a wire-up that minted and then failed every register leaves behind."""
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env)
    env.ps_linux_functional_account_id = 901
    db.commit()
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({})):
        line = asyncio.run(w.teardown(db, env))
    assert [i for i, _ in ps.deleted] == [901]
    assert "Deleted the linux functional account" in line
    db.close()


def test_a_failed_delete_keeps_the_id_and_says_remove_it_by_hand():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    env.ps_linux_functional_account_id = 901
    db.commit()
    ps = _FakePS(delete_raises="nope")
    with _Installed(ps, _FakePlatform({})):
        line = asyncio.run(w.teardown(db, env))
    assert "by hand" in line
    db.refresh(env)
    assert env.ps_linux_functional_account_id == 901
    db.close()


# ── the run says what it created ─────────────────────────────────────────────

def test_a_mint_is_reported_as_created_so_the_run_can_say_so():
    """An account minted in a customer's Password Safe is a side effect. One that cannot be
    seen in the run that caused it is the shape this codebase treats as a bug — so the
    context carries it and `run_env_wireup` writes it to the job log."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}"})):
        ctx = asyncio.run(w.ps_context(db, env))
    assert ctx["minted"] == {"linux": 901}
    db.close()


def test_resolving_an_existing_account_is_not_reported_as_created():
    """Neither a name on the tenant nor a re-wire creates anything, and a run that claimed
    otherwise would have an SE looking for an account to clean up that is not theirs."""
    db = d.SessionLocal()
    named = _env(db, options={"linux_functional_account": "fa-linux"})
    vm = _vm(db, named)
    rewired = _env(db)
    rewired_vm = _vm(db, rewired)
    rewired.ps_linux_functional_account_id = 555
    db.commit()
    ps = _FakePS(accounts={"555": {"id": 555, "platform_id": 11,
                                   "platform_name": "Linux", "account_name": "root"}})
    platform = _FakePlatform({vm.platform_vm_id: f"root / {_SECRET}",
                              rewired_vm.platform_vm_id: f"root / {_SECRET}"})
    with _Installed(ps, platform):
        assert asyncio.run(w.ps_context(db, named))["minted"] == {}
        assert asyncio.run(w.ps_context(db, rewired))["minted"] == {}
    assert ps.created == []
    db.close()


def test_an_unresolvable_tenant_says_the_accounts_were_left_too():
    """A teardown that cannot reach the tenant leaves the functional accounts behind as
    surely as the managed systems. A message accounting for only half of what is still
    over there sends an SE looking for half."""
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    env.ps_linux_functional_account_id = 901
    # A workgroup is required, so clearing it is how the tenant stops resolving.
    bt_tenant_service.update(db, env.ps_tenant_id, options={"api_account_name": "svc",
                                                            "workgroup": ""})
    db.commit()
    ps = _FakePS()
    with _Installed(ps, _FakePlatform({})):
        line = asyncio.run(w.teardown(db, env))
    assert "Could not resolve" in line, line
    assert "functional account(s) this POV created" in line, line
    assert ps.deleted == []
    db.refresh(env)
    assert env.ps_linux_functional_account_id == 901
    db.close()


# ── the hint on the form ─────────────────────────────────────────────────────

def test_every_hint_names_a_real_option():
    """A hint keyed on something that is not an option renders under nothing, and the
    field it was written for keeps reading as a required box."""
    every = {k for keys in bt_tenant_service.OPTION_KEYS.values() for k in keys}
    unknown = set(bt_tenant_service.OPTION_HINTS) - every
    assert not unknown, f"OPTION_HINTS names non-options: {sorted(unknown)}"


def test_the_two_functional_accounts_say_that_blank_creates_one():
    """The question this whole change answers, answered on the form that asks it."""
    for key in ("linux_functional_account", "windows_functional_account"):
        hint = bt_tenant_service.OPTION_HINTS.get(key, "")
        assert "blank" in hint.lower(), f"{key} does not say what blank means"


def _tests():
    return [(n, o) for n, o in sorted(globals().items())
            if n.startswith("test_") and callable(o)]


if __name__ == "__main__":
    failed = 0
    for name, func in _tests():
        try:
            func()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
    total = len(_tests())
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
