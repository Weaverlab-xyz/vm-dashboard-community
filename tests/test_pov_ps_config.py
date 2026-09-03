"""Reading a POV's Password Safe tenant back against the runbook, and re-running a rule.

The reason this module is a READ and not a bootstrap is the API, not a scoping choice.
From BeyondTrust's own endpoint tables: resource zones and brokers, discovery credentials,
discovery scans and directory queries have no documented endpoint at all; password
policies are read-only; access policies are read plus a Test; and Smart Rule creation is
one narrow shape that is not the runbook's. What IS exposed is every collection's GET and
``POST SmartRules/{id}/Process`` — a read, and the one action a demo repeats.

So what is pinned here:

  * **A collection read has three outcomes.** "Your Password Safe does not serve that
    endpoint" (404) and "the read failed" send an operator to different places, and
    collapsing them sends them to the wrong one. The probe never raises.
  * **One missing collection does not cost the others.** The panel's whole job is to say
    which answered.
  * **`short` is not `missing`.** One access policy where the runbook wants two is a step
    half done; none is a step not started. Only one of those is a conversation.
  * **Readiness is a COUNT, never a name match.** The runbook's names are per-customer,
    and a POV that renamed them has still done the step.
  * **The steps with no endpoint are reported anyway.** A step absent from a checklist
    reads as a step nobody thought about.
  * **Nothing here writes to the customer's tenant**, and no refusal ever carries a
    stringified foreign exception — a Password Safe error body can quote tenant data.

No network: `ps_api_service` is faked at the module boundary.

Runs under pytest, or standalone:
    python tests/test_pov_ps_config.py
"""
import asyncio
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-ps-config")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (bt_tenant_service, pov_env_service,  # noqa: E402
                                    pov_ps_config as c, ps_api_service)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _tenant(db):
    return bt_tenant_service.create(
        db, kind="password_safe", name=_name("ps"),
        base_url="acme.ps.beyondtrustcloud.com",
        client_id="cid", secret="shh",
        options={"api_account_name": "psapi", "workgroup": "POV"},
        created_by="t")


def _env(db, *, with_tenant=True):
    tenant = _tenant(db) if with_tenant else None
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE,
                           ps_tenant_id=tenant["id"] if tenant else None)
    db.add(env)
    db.commit()
    return env


def _full(**over):
    """An inventory where every collection answered with enough rows."""
    base = {"reachable": True, "detail": ""}
    counts = {"workgroups": 1, "access_policies": 2, "password_policies": 2,
              "functional_accounts": 3, "directories": 1, "managed_systems": 5,
              "user_groups": 2, "smart_rules": 9}
    for key, n in counts.items():
        base[key] = {"state": ps_api_service.PROBE_OK,
                     "rows": [{"ID": i} for i in range(n)], "detail": ""}
    base.update(over)
    return base


def _install(inventory=None, *, process=None):
    original = (ps_api_service.read_config_inventory,
                ps_api_service.process_smart_rule)

    async def _read(tenant=None):
        return inventory if inventory is not None else _full()

    async def _proc(rule_id, tenant=None, *, queue=True):
        if isinstance(process, Exception):
            raise process
        return {"smart_rule_id": int(rule_id), "queued": queue}

    ps_api_service.read_config_inventory = _read
    ps_api_service.process_smart_rule = _proc
    return original


def _restore(original):
    (ps_api_service.read_config_inventory,
     ps_api_service.process_smart_rule) = original


def _by_key(report):
    return {i["key"]: i for i in report["items"]}


# ── the tenant gate ──────────────────────────────────────────────────────────

def test_a_pov_with_no_password_safe_tenant_is_refused_with_the_remedy():
    db = d.SessionLocal()
    env = _env(db, with_tenant=False)
    try:
        asyncio.run(c.read(db, env))
        raise AssertionError("a POV with no PS tenant was accepted")
    except c.PsConfigError as exc:
        assert "names no Password Safe tenant" in str(exc)
        assert "Tenants column" in str(exc)
    db.close()


def test_describe_makes_no_network_call_and_says_whether_there_is_anything_to_read():
    """Spread onto every row of the POV list, so a call here would be one per row."""
    db = d.SessionLocal()
    assert c.describe(_env(db))["ps_config_readable"] is True
    assert c.describe(_env(db, with_tenant=False))["ps_config_readable"] is False
    db.close()


# ── the readiness read ───────────────────────────────────────────────────────

def test_a_fully_configured_tenant_reports_every_step_ok():
    db = d.SessionLocal()
    env = _env(db)
    original = _install()
    try:
        report = asyncio.run(c.read(db, env))
    finally:
        _restore(original)
    assert report["reachable"] is True
    assert len(report["items"]) == len(c._ITEMS)
    assert {i["state"] for i in report["items"]} == {"ok"}
    db.close()


def test_an_empty_collection_is_missing_and_a_thin_one_is_short():
    """One access policy where the runbook wants two is a step half done; none is a step
    not started. Only one of those is a conversation with the customer."""
    db = d.SessionLocal()
    env = _env(db)
    inv = _full(
        access_policies={"state": ps_api_service.PROBE_OK,
                         "rows": [{"ID": 1}], "detail": ""},
        user_groups={"state": ps_api_service.PROBE_OK, "rows": [], "detail": ""})
    original = _install(inv)
    try:
        items = _by_key(asyncio.run(c.read(db, env)))
    finally:
        _restore(original)
    assert items["access_policies"]["state"] == "short"
    assert items["access_policies"]["found"] == 1
    assert items["access_policies"]["expected"] == 2
    assert items["user_groups"]["state"] == "missing"
    db.close()


def test_an_endpoint_this_tenant_does_not_serve_is_unavailable_not_an_error():
    """A 404 is a fact about Password Safe. Reporting it as a failure sends an operator to
    check permissions they already have."""
    db = d.SessionLocal()
    env = _env(db)
    inv = _full(password_policies={"state": ps_api_service.PROBE_UNAVAILABLE,
                                   "rows": [], "detail": "does not serve that endpoint"})
    original = _install(inv)
    try:
        items = _by_key(asyncio.run(c.read(db, env)))
    finally:
        _restore(original)
    assert items["password_policies"]["state"] == ps_api_service.PROBE_UNAVAILABLE
    assert "endpoint" in items["password_policies"]["detail"]
    db.close()


def test_one_failed_collection_does_not_cost_the_others():
    db = d.SessionLocal()
    env = _env(db)
    inv = _full(smart_rules={"state": ps_api_service.PROBE_ERROR, "rows": [],
                             "detail": "the API identity is not permitted to read it"})
    original = _install(inv)
    try:
        items = _by_key(asyncio.run(c.read(db, env)))
    finally:
        _restore(original)
    assert items["smart_rules"]["state"] == ps_api_service.PROBE_ERROR
    assert items["workgroups"]["state"] == "ok"
    assert len([i for i in items.values() if i["state"] == "ok"]) == len(c._ITEMS) - 1
    db.close()


def test_an_unreachable_tenant_says_so_without_pretending_the_steps_are_missing():
    """Reporting eight missing steps for a credential problem would send an SE to the
    customer's console to redo work that is already there."""
    db = d.SessionLocal()
    env = _env(db)
    original = _install({"reachable": False, "detail": "could not sign in"})
    try:
        report = asyncio.run(c.read(db, env))
    finally:
        _restore(original)
    assert report["reachable"] is False
    assert report["items"] == []
    assert "sign in" in report["detail"]
    # …and the unverifiable steps are still listed, so the panel is never blank.
    assert len(report["unverifiable"]) == len(c.UNVERIFIABLE)
    db.close()


def test_every_step_with_no_endpoint_is_reported_anyway():
    """A step absent from a checklist reads as a step nobody thought about. These four are
    the ones the API cannot answer for at all."""
    db = d.SessionLocal()
    env = _env(db)
    original = _install()
    try:
        report = asyncio.run(c.read(db, env))
    finally:
        _restore(original)
    steps = " ".join(u["step"] for u in report["unverifiable"])
    for expected in ("Resource Broker", "Discovery Credentials", "Discovery Scan",
                     "Directory Queries"):
        assert expected in steps, f"{expected} is not accounted for"
    for u in report["unverifiable"]:
        assert u["detail"], f"{u['step']} names no reason"
    db.close()


def test_readiness_is_a_count_and_never_a_name_match():
    """The runbook's names are per-customer -- btpoc.upm.academy, PSDirBrowser,
    ServiceDesk_Users -- and a POV that renamed them has still done the step. Pinned as
    BEHAVIOUR rather than by grepping the source, because the copy legitimately quotes
    those names where it explains the rule."""
    probe = {"state": ps_api_service.PROBE_OK, "detail": "",
             "rows": [{"Title": "nothing like the runbook"},
                      {"Title": "wRoNg NaMe EnTiReLy"}]}
    # Two rows where two are wanted is `ok` whatever they are called.
    assert c._state_for(probe, 2) == "ok"
    assert c._state_for(probe, 3) == "short"
    empty = {"state": ps_api_service.PROBE_OK, "rows": [], "detail": ""}
    assert c._state_for(empty, 1) == "missing"


def test_every_item_names_a_runbook_step_and_a_remedy():
    for key, step, expect, remedy in c._ITEMS:
        assert key and step and remedy, f"{key} is incompletely declared"
        assert expect >= 1, f"{key} expects {expect}"
        assert "—" in step or "-" in step, f"{step!r} does not name its runbook step"


def test_every_item_key_is_a_collection_the_api_service_reads():
    """The two lists drift silently otherwise: an item keyed on a collection nobody reads
    renders as a permanent error."""
    for key, *_rest in c._ITEMS:
        assert key in ps_api_service.CONFIG_KEYS, \
            f"{key} is not read by ps_api_service.read_config_inventory"


# ── the smart rule picker ────────────────────────────────────────────────────

def test_the_picker_keeps_only_the_fields_it_shows_and_sorts_by_title():
    """The raw rows carry an organisation id and a description that can quote customer
    naming, and a picker needs neither."""
    db = d.SessionLocal()
    env = _env(db)
    inv = _full(smart_rules={"state": ps_api_service.PROBE_OK, "detail": "", "rows": [
        {"SmartRuleID": 7, "Title": "5 - Map and Access", "Category": "Map",
         "RuleType": "ManagedAccount", "LastProcessedDate": "2026-09-01",
         "OrganizationID": "org-secret", "Description": "customer naming here"},
        {"SmartRuleID": 3, "Title": "1 - List", "Category": "List",
         "RuleType": "Asset", "LastProcessedDate": ""},
    ]})
    original = _install(inv)
    try:
        rules = asyncio.run(c.smart_rules(db, env))
    finally:
        _restore(original)
    assert [r["title"] for r in rules] == ["1 - List", "5 - Map and Access"]
    assert set(rules[0]) == {"id", "title", "category", "rule_type", "last_processed"}
    blob = repr(rules)
    assert "org-secret" not in blob and "customer naming" not in blob
    db.close()


def test_a_rule_with_no_id_is_skipped_rather_than_offered():
    db = d.SessionLocal()
    env = _env(db)
    inv = _full(smart_rules={"state": ps_api_service.PROBE_OK, "detail": "", "rows": [
        {"Title": "no id here"}, {"SmartRuleID": 4, "Title": "fine"}]})
    original = _install(inv)
    try:
        rules = asyncio.run(c.smart_rules(db, env))
    finally:
        _restore(original)
    assert [r["id"] for r in rules] == [4]
    db.close()


def test_unreadable_smart_rules_are_a_refusal_naming_the_tenant():
    db = d.SessionLocal()
    env = _env(db)
    inv = _full(smart_rules={"state": ps_api_service.PROBE_ERROR, "rows": [],
                             "detail": "the API identity is not permitted to read it"})
    original = _install(inv)
    try:
        asyncio.run(c.smart_rules(db, env))
        raise AssertionError("unreadable Smart Rules were accepted")
    except c.PsConfigError as exc:
        assert "not permitted" in str(exc)
    finally:
        _restore(original)
    db.close()


# ── re-processing ────────────────────────────────────────────────────────────

def test_processing_a_rule_is_queued_so_the_button_returns():
    """Synchronous processing would hold an HTTP request open for however long the
    estate takes, which is why this needs no job type."""
    db = d.SessionLocal()
    env = _env(db)
    original = _install()
    try:
        result = asyncio.run(c.process(db, env, 7))
    finally:
        _restore(original)
    assert result["smart_rule_id"] == 7 and result["queued"] is True
    assert result["tenant"]
    db.close()


def test_a_refused_process_names_what_to_check_and_carries_no_foreign_exception():
    db = d.SessionLocal()
    env = _env(db)
    original = _install(process=RuntimeError("<html>tenant response body</html>"))
    try:
        asyncio.run(c.process(db, env, 7))
        raise AssertionError("a failed process was reported as success")
    except c.PsConfigError as exc:
        assert "Smart Rule Management" in str(exc)
        assert "RuntimeError" in str(exc), "the failure class is not named"
        assert "tenant response body" not in str(exc), \
            "the refusal quotes the tenant's response body"
    finally:
        _restore(original)
    db.close()


def test_a_purpose_built_api_refusal_keeps_its_own_words():
    db = d.SessionLocal()
    env = _env(db)
    original = _install(process=ps_api_service.PSApiError("a Smart Rule id is a number"))
    try:
        asyncio.run(c.process(db, env, "abc"))
        raise AssertionError("a bad id was accepted")
    except c.PsConfigError as exc:
        assert "a Smart Rule id is a number" in str(exc)
    finally:
        _restore(original)
    db.close()


# ── the probe, against the real helper ───────────────────────────────────────

def test_the_probe_tells_a_404_from_a_403_from_anything_else():
    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "body"

        def json(self):
            return []

    class _Client:
        def __init__(self, code):
            self.code = code

        async def get(self, path, **kw):
            return _Resp(self.code)

    async def _go(code):
        return await ps_api_service._probe(_Client(code), "Widgets")

    assert asyncio.run(_go(404))["state"] == ps_api_service.PROBE_UNAVAILABLE
    assert asyncio.run(_go(405))["state"] == ps_api_service.PROBE_UNAVAILABLE
    assert asyncio.run(_go(403))["state"] == ps_api_service.PROBE_ERROR
    assert "not permitted" in asyncio.run(_go(403))["detail"]
    assert asyncio.run(_go(500))["state"] == ps_api_service.PROBE_ERROR
    assert asyncio.run(_go(200))["state"] == ps_api_service.PROBE_OK


def test_the_probe_never_carries_the_response_body_outward():
    """A Password Safe error body can quote tenant data, so the detail is this
    codebase's own words and the exception is logged instead."""
    class _Resp:
        status_code = 500
        text = "SECRET-TENANT-DATA"

        def json(self):
            return []

    class _Client:
        async def get(self, path, **kw):
            return _Resp()

    out = asyncio.run(ps_api_service._probe(_Client(), "Widgets"))
    assert "SECRET-TENANT-DATA" not in out["detail"]


def test_a_paged_read_can_be_aimed_at_a_povs_own_tenant():
    """It was singleton-only, so a POV's readiness read would have reported the
    install's estate."""
    import inspect
    src = inspect.getsource(ps_api_service._list_client)
    assert "tenant=None" in src and "_base_url(tenant)" in src


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
