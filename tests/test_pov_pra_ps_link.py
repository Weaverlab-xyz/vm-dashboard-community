"""PRA <-> Password Safe for one POV: check what PRA sees, link what it sees.

The Config API cannot create PRA's Password Safe integration, so what is pinned here is
the half it can do, and the honesty around the half it cannot:

  * **Matched by the managed system's NAME**, the one `pov_wireup.onboard_vm` wrote --
    `pov_wireup.ps_system_name`, shared so the two cannot drift.
  * **Nothing seen is a refusal with the checklist**, never a link of zero reported as
    done.
  * **Idempotent.** An account already granted to the policy is not granted twice.
  * **An account a customer admin excluded stays excluded** (`no_jump_items`).
  * **Counts, not calls**, are what the ladder reads.

Uses a real SQLite database and a fake PRA. No network.

Runs under pytest, or standalone:
    python tests/test_pov_pra_ps_link.py
"""
import asyncio
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
# Its own file: create_all never adds a column to a table that already exists, so a
# shared database left by an older run would lack this feature's columns.
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-pra-ps-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-pra-ps-link")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (bt_tenant_service, pov_env_service,  # noqa: E402
                                    pov_pra_ps_link as link, pov_wireup,
                                    pra_ps_vault_api)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _tenant(db):
    return bt_tenant_service.create(
        db, kind="pra", name=_name("pra"), base_url="acme.beyondtrustcloud.com",
        client_id="cid", secret="sekrit", created_by="t", options={})


def _env(db, **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE,
                           pra_tenant_id=_tenant(db)["id"], ps_tenant_id="t-ps", **kw)
    db.add(env)
    db.commit()
    return env


def _vm(db, env, name, *, os_family="linux", jump="101", onboarded=True):
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"), name=name,
                             os_family=os_family, private_ip="10.9.0.10",
                             pra_jump_id=jump,
                             ps_managed_system_id="55" if onboarded else None)
    db.add(row)
    db.commit()
    return row


class _FakePRA:
    def __init__(self, accounts=(), granted=(), association=None):
        self.accounts = list(accounts)
        self.granted = set(granted)
        self.association = association or {}
        self.calls = []

    def install(self):
        api = pra_ps_vault_api
        self._orig = {n: getattr(api, n) for n in
                      ("list_ps_accounts", "policy_account_ids", "grant_to_policy",
                       "associate_with_jump_item")}

        async def list_ps_accounts(tenant):
            return self.accounts

        async def policy_account_ids(tenant, policy_id):
            return set(self.granted)

        async def grant_to_policy(tenant, policy_id, account_id):
            self.calls.append(("grant", policy_id, account_id))
            self.granted.add(account_id)

        async def associate_with_jump_item(tenant, account_id, jump_id, jump_type):
            self.calls.append(("associate", account_id, jump_id, jump_type))
            return self.association.get(account_id, "added")

        for n, fn in (("list_ps_accounts", list_ps_accounts),
                      ("policy_account_ids", policy_account_ids),
                      ("grant_to_policy", grant_to_policy),
                      ("associate_with_jump_item", associate_with_jump_item)):
            setattr(api, n, fn)
        return self

    def restore(self):
        for n, fn in self._orig.items():
            setattr(pra_ps_vault_api, n, fn)


def _acct(acct_id, env, vm):
    return {"id": acct_id, "name": f"adminuser@{vm.name}", "username": "adminuser",
            "system": pov_wireup.ps_system_name(env, vm), "workgroup": "POV"}


def test_check_matches_this_povs_accounts_by_managed_system_name():
    db = d.SessionLocal()
    env = _env(db)
    web = _vm(db, env, "web01")
    other = d.PovEnvironment(name="someone-elses")       # a different POV's system name
    pra = _FakePRA(accounts=[_acct(1, env, web),
                             {"id": 2, "name": "x", "username": "x",
                              "system": f"{other.name}-web01", "workgroup": ""}]).install()
    try:
        got = asyncio.run(link.check(db, env))
    finally:
        pra.restore()
    assert got["matched"] == 1 and got["onboarded"] == 1
    assert env.pra_ps_matched_count == 1 and env.pra_ps_checked_at is not None
    assert "checklist" not in got
    db.close()


def test_check_that_sees_nothing_returns_the_checklist_and_records_zero():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, "web01")
    pra = _FakePRA(accounts=[]).install()
    try:
        got = asyncio.run(link.check(db, env))
    finally:
        pra.restore()
    assert got["matched"] == 0
    assert got["checklist"] and "Password Safe integration" in got["checklist"][0]
    assert env.pra_ps_checked_at is not None and env.pra_ps_matched_count == 0
    assert link.describe(env)["pra_ps_checked"] is True
    db.close()


def test_link_refuses_rather_than_reporting_zero_as_done():
    db = d.SessionLocal()
    env = _env(db, pra_vendor_policy_id="9")
    _vm(db, env, "web01")
    pra = _FakePRA(accounts=[]).install()
    try:
        try:
            asyncio.run(link.link(db, env))
            raise AssertionError("a link of nothing was accepted")
        except link.LinkError as exc:
            assert "Password Safe integration" in str(exc)
    finally:
        pra.restore()
    assert pra.calls == []
    db.close()


def test_link_grants_inject_to_the_vendor_policy_and_associates_the_jump_item():
    db = d.SessionLocal()
    env = _env(db, pra_vendor_policy_id="9")
    web = _vm(db, env, "web01", os_family="linux", jump="101")
    dc = _vm(db, env, "dc01", os_family="windows", jump="202")
    pra = _FakePRA(accounts=[_acct(1, env, web), _acct(2, env, dc)]).install()
    try:
        got = asyncio.run(link.link(db, env))
    finally:
        pra.restore()
    assert ("associate", 1, 101, "shell_jump") in pra.calls
    assert ("associate", 2, 202, "remote_rdp") in pra.calls
    assert ("grant", "9", 1) in pra.calls and ("grant", "9", 2) in pra.calls
    assert got["linked"] == 2 and env.pra_ps_linked_count == 2
    db.close()


def test_link_is_idempotent_on_an_account_the_policy_already_grants():
    db = d.SessionLocal()
    env = _env(db, pra_vendor_policy_id="9")
    web = _vm(db, env, "web01")
    pra = _FakePRA(accounts=[_acct(1, env, web)], granted={1},
                   association={1: "already"}).install()
    try:
        got = asyncio.run(link.link(db, env))
    finally:
        pra.restore()
    assert not any(c[0] == "grant" for c in pra.calls)
    assert got["linked"] == 1
    db.close()


def test_an_account_an_admin_excluded_is_not_counted_as_linked():
    db = d.SessionLocal()
    env = _env(db, pra_vendor_policy_id="9")
    web = _vm(db, env, "web01")
    pra = _FakePRA(accounts=[_acct(1, env, web)],
                   association={1: "excluded"}).install()
    try:
        got = asyncio.run(link.link(db, env))
    finally:
        pra.restore()
    assert got["linked"] == 0
    assert any("excluded" in line for line in got["lines"])
    db.close()


def test_with_no_vendor_group_the_link_says_the_grant_is_still_to_come():
    db = d.SessionLocal()
    env = _env(db)
    web = _vm(db, env, "web01")
    pra = _FakePRA(accounts=[_acct(1, env, web)]).install()
    try:
        got = asyncio.run(link.link(db, env))
    finally:
        pra.restore()
    assert not any(c[0] == "grant" for c in pra.calls)
    assert any("No vendor group" in line for line in got["lines"])
    db.close()


def test_a_pov_with_nothing_onboarded_is_refused_naming_the_wireup():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, "web01", onboarded=False)
    try:
        asyncio.run(link.check(db, env))
        raise AssertionError("a POV with nothing onboarded was checked")
    except link.LinkError as exc:
        assert "wire-up" in str(exc)
    finally:
        db.close()


def test_onboarding_and_matching_share_one_name():
    """onboard_vm writes the name, this matches on it; a second copy would drift."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_wireup.py"),
               encoding="utf-8").read()
    body = src.split("async def onboard_vm", 1)[1].split("\nasync def ", 1)[0]
    assert "label = ps_system_name(env, vm)" in body


def test_the_association_leaves_an_explicit_exclusion_alone():
    """The real helper, against a canned GET: no_jump_items is the customer's choice."""
    calls = []

    async def fake_request(tenant, method, path, **kw):
        calls.append((method, path))
        if method == "GET":
            return 200, {"filter_type": "no_jump_items"}, {}
        return 201, {}, {}

    orig = pra_ps_vault_api._request
    pra_ps_vault_api._request = fake_request
    try:
        got = asyncio.run(pra_ps_vault_api.associate_with_jump_item(None, 1, 101,
                                                                    "shell_jump"))
    finally:
        pra_ps_vault_api._request = orig
    assert got == "excluded"
    assert [c[0] for c in calls] == ["GET"]


def test_the_association_adds_to_existing_criteria_and_skips_a_duplicate():
    calls = []
    current = {"filter_type": "criteria", "jump_items": [{"id": 7, "type": "shell_jump"}]}

    async def fake_request(tenant, method, path, **kw):
        calls.append((method, path, kw.get("json")))
        return (200, current, {}) if method == "GET" else (200, {}, {})

    orig = pra_ps_vault_api._request
    pra_ps_vault_api._request = fake_request
    try:
        assert asyncio.run(pra_ps_vault_api.associate_with_jump_item(
            None, 1, 7, "shell_jump")) == "already"
        assert asyncio.run(pra_ps_vault_api.associate_with_jump_item(
            None, 1, 8, "shell_jump")) == "added"
    finally:
        pra_ps_vault_api._request = orig
    posts = [c for c in calls if c[0] == "POST"]
    assert posts == [("POST", "/api/config/v1/vault/account/1/jump-item-association/"
                              "jump-item", {"id": 8, "type": "shell_jump"})]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
