"""The BeyondTrust tenant registry — the properties whose absence is silent.

The whole feature exists because "which tenant?" must have exactly one answer per POV,
and the wrong answer never raises. So what is pinned here is mostly *refusals*:

  * **An explicit tenant id never falls back.** Resolving a missing, inactive or
    wrong-kind id to "the default instead" is precisely how a customer's POV would
    onboard into the demo tenant with nothing going wrong on the way.
  * **The legacy singletons still answer when the table is empty.** That is the
    compatibility contract — every existing install, and every demo instance, keeps
    working without knowing this table exists.
  * **A tenant in use cannot be deleted.** The FK is SET NULL, so deleting one would
    quietly blank a POV's tenant and the next wire-up would resolve the default.
  * **A blank secret keeps the stored one.** The form cannot render what is stored, so
    any other rule means saving a Jump Group name wipes a credential.
  * **The secret never leaves the service.** `serialize` reports that one exists, never
    the value and never its length.
  * **`api_base` matches what the real clients already do**, or a verified tenant's calls
    go somewhere the verify never touched.

Uses a real SQLite database. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_bt_tenants.py
"""
import os
import pathlib
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-bt-tenants")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (bt_tenant_service as t,  # noqa: E402
                                    bt_tenant_verify, config_service, pov_env_service)


def _name(prefix="acme"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mk(db, kind="pra", **kw):
    kw.setdefault("name", _name())
    kw.setdefault("base_url", "acme.beyondtrustcloud.com")
    kw.setdefault("client_id", "cid")
    kw.setdefault("secret", "sekrit")
    kw.setdefault("created_by", "tester")
    return t.create(db, kind=kind, **kw)


def _clear(kind):
    """Remove every row of a kind, so a test can exercise the empty-table fallback."""
    db = d.SessionLocal()
    db.query(d.BeyondTrustTenant).filter(d.BeyondTrustTenant.kind == kind).delete()
    db.commit()
    db.close()


class _singletons:
    """Pretend the legacy config keys hold exactly these values, and nothing else.

    Patches the module's ``_cfg`` rather than writing ``app_config`` rows, which matters
    for two reasons and cost one debugging session to learn:

    * ``_cfg`` falls back to ``settings``, so a value in the developer's ``.env`` answers
      even when no row exists. A test that "cleared" a key by deleting the row would still
      see it, and the singleton-fallback tests would pass or fail depending on whose
      machine they ran on.
    * Deleting rows to clean up means a test suite that mutates real configuration in a
      shared database. A test should never be able to unset an operator's credential.
    """

    def __init__(self, module, **values):
        self.module = module
        self.values = values

    def __enter__(self):
        self._original = self.module._cfg
        self.module._cfg = lambda key: self.values.get(key, "")
        return self

    def __exit__(self, *exc):
        self.module._cfg = self._original
        return False


# ── resolution ───────────────────────────────────────────────────────────────

def test_an_explicit_id_never_falls_back_to_the_default():
    """The property the whole registry exists for. A wrong id must be an error, because
    resolving it to 'the default instead' is a POV onboarding into the wrong tenant."""
    db = d.SessionLocal()
    _mk(db, kind="pra", name=_name("default"), is_default=True)
    try:
        t.resolve(db, "pra", "no-such-tenant-id")
        raise AssertionError("a missing id fell back")
    except t.BTTenantError as exc:
        assert "no longer exists" in str(exc)
    finally:
        db.close()


def test_the_wrong_kind_is_refused_by_name():
    db = d.SessionLocal()
    ps = _mk(db, kind="password_safe", base_url="acme.ps.beyondtrustcloud.com",
             options={"api_account_name": "svc"})
    try:
        t.resolve(db, "pra", ps["id"])
        raise AssertionError("a password_safe tenant resolved as pra")
    except t.BTTenantError as exc:
        assert "Password Safe" in str(exc) and "Privileged Remote Access" in str(exc)
    finally:
        db.close()


def test_a_disabled_tenant_is_refused_rather_than_skipped():
    db = d.SessionLocal()
    row = _mk(db, kind="entitle", base_url="https://api.entitle.io/v1", client_id="")
    t.update(db, row["id"], is_active=False)
    try:
        t.resolve(db, "entitle", row["id"])
        raise AssertionError("a disabled tenant resolved")
    except t.BTTenantError as exc:
        assert "disabled" in str(exc)
    finally:
        db.close()


def test_the_legacy_singletons_answer_when_the_table_is_empty():
    """The compatibility contract, and what lets a demo instance ignore this table."""
    _clear("pra")
    db = d.SessionLocal()
    with _singletons(t, bt_api_host="demo.beyondtrustcloud.com", bt_client_id="demo-id",
                     bt_client_secret="demo-secret", bt_jump_group_name="Demo",
                     bt_jumpoint_name="jp"):
        try:
            got = t.resolve(db, "pra")
            assert got.id == "", "the singleton fallback has no row behind it"
            assert got.base_url == "demo.beyondtrustcloud.com"
            assert got.secret == "demo-secret"
            assert got.option("jump_group_name") == "Demo"
        finally:
            db.close()


def test_a_half_configured_singleton_is_not_a_tenant():
    """A URL with no secret fails at the first call. 'None configured' is the useful
    message; a Tenant that cannot authenticate is not."""
    _clear("pra")
    db = d.SessionLocal()
    with _singletons(t, bt_api_host="demo.beyondtrustcloud.com"):
        try:
            t.resolve(db, "pra")
            raise AssertionError("a half-configured singleton resolved")
        except t.BTTenantError as exc:
            assert "no Privileged Remote Access tenant is configured" in str(exc)
        finally:
            db.close()


def test_a_row_beats_the_singletons():
    """Once a row exists the singletons stop being consulted for that kind — a seed, not
    a sync, so editing the old key does nothing."""
    _clear("pra")
    db = d.SessionLocal()
    with _singletons(t, bt_api_host="demo.beyondtrustcloud.com", bt_client_id="demo-id",
                     bt_client_secret="demo-secret"):
        try:
            _mk(db, kind="pra", base_url="customer.beyondtrustcloud.com", secret="real")
            got = t.resolve(db, "pra")
            assert got.base_url == "customer.beyondtrustcloud.com"
            assert got.secret == "real"
        finally:
            db.close()


def test_two_tenants_and_no_default_is_a_refusal_not_a_guess():
    _clear("pra")
    db = d.SessionLocal()
    with _singletons(t):
        try:
            _mk(db, kind="pra", name=_name("one"))
            _mk(db, kind="pra", name=_name("two"))
            t.resolve(db, "pra")
            raise AssertionError("it guessed between two customers' appliances")
        except t.BTTenantError as exc:
            assert "none is the default" in str(exc)
        finally:
            db.close()


def test_the_default_is_scoped_to_its_kind():
    """One flag across all three would let choosing a PRA appliance silently repoint
    Password Safe."""
    _clear("pra")
    _clear("password_safe")
    db = d.SessionLocal()
    pra = _mk(db, kind="pra", is_default=True)
    ps = _mk(db, kind="password_safe", base_url="acme.ps.beyondtrustcloud.com",
             is_default=True, options={"api_account_name": "svc"})
    assert t.resolve(db, "pra").id == pra["id"]
    assert t.resolve(db, "password_safe").id == ps["id"]
    db.close()


# ── secrets ──────────────────────────────────────────────────────────────────

def test_the_secret_never_appears_in_the_serialized_row():
    db = d.SessionLocal()
    row = _mk(db, secret="super-secret-value")
    blob = repr(row)
    assert "super-secret-value" not in blob
    assert row["has_secret"] is True
    assert "secret" not in row and "secret_enc" not in row
    db.close()


def test_a_blank_secret_on_update_keeps_the_stored_one():
    """The form cannot render what is stored, so blank has to mean keep — otherwise
    saving a Jump Group name wipes the credential."""
    db = d.SessionLocal()
    row = _mk(db, secret="keep-me")
    t.update(db, row["id"], name=_name("renamed"), secret="")
    assert t.resolve(db, "pra", row["id"]).secret == "keep-me"
    db.close()


def test_moving_to_a_vault_reference_clears_the_local_copy():
    """Leaving the ciphertext would mean the credential an operator believes they removed
    is still in this database."""
    db = d.SessionLocal()
    row = _mk(db, secret="local-copy")
    t.update(db, row["id"], secret_ref="aws_sm://some/path")
    stored = db.query(d.BeyondTrustTenant).filter(
        d.BeyondTrustTenant.id == row["id"]).first()
    assert stored.secret_enc is None
    assert stored.secret_ref == "aws_sm://some/path"
    db.close()


def test_a_secret_and_a_reference_together_are_refused():
    db = d.SessionLocal()
    try:
        _mk(db, secret="a", secret_ref="aws_sm://b")
        raise AssertionError("both were accepted")
    except t.BTTenantError as exc:
        assert "not both" in str(exc)
    finally:
        db.close()


def test_a_reference_that_is_not_one_is_refused():
    """Otherwise the literal string is stored as a reference and resolves to nothing."""
    db = d.SessionLocal()
    try:
        _mk(db, secret="", secret_ref="just-a-password")
        raise AssertionError("a literal was accepted as a reference")
    except t.BTTenantError as exc:
        assert "external secret reference" in str(exc)
    finally:
        db.close()


def test_changing_the_target_invalidates_a_previous_verify():
    """Stale-green is worse than unknown: the tenant that passed no longer exists."""
    db = d.SessionLocal()
    row = _mk(db)
    t.record_result(db, row["id"])
    assert t.serialize(db, t._get(db, row["id"]))["last_ok_at"]
    after = t.update(db, row["id"], base_url="somewhere-else.beyondtrustcloud.com")
    assert after["last_ok_at"] is None and after["last_checked_at"] is None
    db.close()


def test_a_failed_verify_keeps_the_previous_success():
    """'worked an hour ago, fails now' and 'never worked' are different problems."""
    db = d.SessionLocal()
    row = _mk(db)
    t.record_result(db, row["id"])
    failed = t.record_result(db, row["id"], error="401 from the appliance")
    assert failed["last_ok_at"], "the previous success was thrown away"
    assert "401" in failed["last_error"]
    db.close()


# ── options ──────────────────────────────────────────────────────────────────

def test_options_outside_the_allowlist_are_dropped():
    """This is a free-form blob on a row that holds a credential."""
    db = d.SessionLocal()
    row = _mk(db, options={"jump_group_name": "POV", "password": "oops"})
    assert row["options"] == {"jump_group_name": "POV"}
    db.close()


def test_every_kind_has_an_option_allowlist_and_a_required_set():
    for kind in t.VALID_KINDS:
        assert kind in t.OPTION_KEYS, f"{kind} has no option allowlist"
        assert kind in t.REQUIRED_OPTIONS, f"{kind} has no required-option set"
        assert set(t.REQUIRED_OPTIONS[kind]) <= set(t.OPTION_KEYS[kind]), \
            f"{kind} requires an option it does not allow"


def test_a_missing_required_option_is_reported_not_hidden():
    """A tenant with valid credentials and no Jump Group is configured-but-unusable, and
    finding that out inside a provision job is finding it out too late."""
    db = d.SessionLocal()
    row = _mk(db, kind="pra", options={"jumpoint_name": "gw"})
    assert row["missing_options"] == ["jump_group_name"]
    db.close()


def test_an_option_no_caller_reads_is_not_required():
    """`jumpoint_name` is allowed and unread: a POV's jump items route through the Gateway
    installed INSIDE the environment, never the tenant's appliance-wide one. Requiring it
    made a correctly configured tenant report a gap nothing would ever consume."""
    db = d.SessionLocal()
    row = _mk(db, kind="pra", options={"jump_group_name": "POV"})
    assert row["missing_options"] == []
    db.close()


def test_the_entitle_options_are_only_ones_a_caller_reads():
    """A field an operator fills in and nothing consumes reads as configured, which is
    worse than an absent one. `machine_identity_email` was exactly that: every reader of
    it takes the instance-wide setting, never the tenant's copy."""
    assert "machine_identity_email" not in t.OPTION_KEYS["entitle"]
    assert "machine_identity_email" not in t.OPTION_LABELS
    src = pathlib.Path(_ROOT, "web_dashboard", "services", "pov_wireup.py").read_text(
        encoding="utf-8")
    for key in t.OPTION_KEYS["entitle"]:
        assert f'option("{key}")' in src, (
            f"the Entitle tenant option {key!r} is offered on the form but no caller in "
            f"pov_wireup reads it")


def test_entitle_owner_and_workflow_are_required_because_the_wireup_refuses_without_them():
    """`pov_wireup.entitle_tenant_ctx` refuses before ANY integration is created — the
    REST accessor adapter included, which needs neither an agent nor a key. So the gap
    belongs on the row, not inside a job."""
    db = d.SessionLocal()
    row = _mk(db, kind="entitle", client_id="", options={"ssh_sudo_user": "btadmin"})
    assert row["missing_options"] == ["owner_id", "workflow_id"]
    db.close()


# ── the URL that is not a per-tenant fact ────────────────────────────────────

def test_an_entitle_tenant_needs_no_url_because_there_is_only_one():
    """Entitle is one multi-tenant service behind one API host. Asking an SE to type it
    per POV is asking for a typo in a field with exactly one correct value."""
    db = d.SessionLocal()
    row = _mk(db, kind="entitle", base_url="", client_id="")
    assert row["base_url"] == t.default_base_url("entitle")
    assert row["base_url"], "the Entitle default resolved empty"
    db.close()


def test_the_default_url_follows_the_configured_one_not_a_hardcoded_string():
    """An install on a non-standard Entitle region has already moved `entitle_api_url`.
    A constant here would send that install's calls to the wrong host."""
    config_service.set("entitle_api_url", "https://api.eu.entitle.io/v1")
    try:
        assert t.default_base_url("entitle") == "https://api.eu.entitle.io/v1"
    finally:
        config_service.set("entitle_api_url", "")


def test_only_entitle_gets_a_default_url():
    """A PRA or Password Safe hostname IS the customer — defaulting one would point a
    POV's jump items at whatever appliance this instance happens to know about."""
    for kind in ("pra", "password_safe"):
        assert t.default_base_url(kind) == ""
    db = d.SessionLocal()
    try:
        _mk(db, kind="pra", base_url="")
        raise AssertionError("a PRA tenant was accepted with no appliance hostname")
    except t.BTTenantError as exc:
        assert "URL or appliance hostname" in str(exc)
    finally:
        db.close()


def test_clearing_an_entitle_url_restores_the_default_rather_than_refusing():
    db = d.SessionLocal()
    row = _mk(db, kind="entitle", base_url="https://api.eu.entitle.io/v1", client_id="")
    after = t.update(db, row["id"], base_url="")
    assert after["base_url"] == t.default_base_url("entitle")
    db.close()


# ── naming ───────────────────────────────────────────────────────────────────

def test_the_name_is_a_slug_so_it_is_not_where_a_company_gets_typed():
    db = d.SessionLocal()
    try:
        _mk(db, name="Acme Corporation Inc")
        raise AssertionError("free text was accepted as a name")
    except t.BTTenantError as exc:
        assert "not a company name" in str(exc)
    finally:
        db.close()


def test_names_are_unique_per_kind_not_globally():
    """The same customer legitimately has a PRA tenant and a Password Safe tenant."""
    db = d.SessionLocal()
    shared = _name("acme")
    _mk(db, kind="pra", name=shared)
    _mk(db, kind="password_safe", name=shared, base_url="acme.ps.beyondtrustcloud.com",
        options={"api_account_name": "svc"})
    try:
        _mk(db, kind="pra", name=shared)
        raise AssertionError("a duplicate within one kind was accepted")
    except t.BTTenantError as exc:
        assert "already exists" in str(exc)
    finally:
        db.close()


# ── deletion ─────────────────────────────────────────────────────────────────

def test_a_tenant_a_live_pov_points_at_cannot_be_deleted():
    """The FK is SET NULL, so this would silently blank the POV's tenant and the next
    wire-up would resolve the default — a POV changing customer without saying so."""
    db = d.SessionLocal()
    row = _mk(db)
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           status=pov_env_service.STATUS_ACTIVE,
                           pra_tenant_id=row["id"])
    db.add(env)
    db.commit()
    try:
        t.delete(db, row["id"])
        raise AssertionError("a tenant in use was deleted")
    except t.BTTenantError as exc:
        assert "still reference" in str(exc)
    assert t.serialize(db, t._get(db, row["id"]))["in_use_by"] == 1
    db.close()


def test_a_destroyed_pov_does_not_pin_its_tenant_forever():
    """A destroyed POV's row is inventory, not a live reference."""
    db = d.SessionLocal()
    row = _mk(db)
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           status=pov_env_service.STATUS_DESTROYED,
                           pra_tenant_id=row["id"])
    db.add(env)
    db.commit()
    assert t.environments_using(db, row["id"]) == 0
    t.delete(db, row["id"])
    db.close()


# ── the POV selection ────────────────────────────────────────────────────────

def test_a_blank_selection_is_allowed():
    """A POV is created before its wire-up runs; requiring all three would make the
    registry a gate on a step that does not need it."""
    db = d.SessionLocal()
    assert t.validate_selection(db) == {"pra_tenant_id": "", "ps_tenant_id": "",
                                        "entitle_tenant_id": ""}
    db.close()


def test_a_selection_pointing_at_the_wrong_kind_is_refused_at_the_request():
    """Inside the job this surfaces minutes later, against an environment that already
    exists — and correcting a dropdown would mean destroying it first."""
    db = d.SessionLocal()
    pra = _mk(db, kind="pra")
    try:
        t.validate_selection(db, ps_tenant_id=pra["id"])
        raise AssertionError("a pra tenant was accepted as the Password Safe one")
    except t.BTTenantError as exc:
        assert "not Password Safe" in str(exc)
    finally:
        db.close()


# ── the verify contract ──────────────────────────────────────────────────────

def test_the_verifiable_kinds_match_what_the_verifier_implements():
    """The capability table and the code behind it, pinned together — the same rule
    lab_platforms.CAPABILITIES follows."""
    assert set(t.VERIFIABLE_KINDS) == set(bt_tenant_verify._VERIFIERS)
    assert set(t.VERIFIABLE_KINDS) <= set(t.VALID_KINDS)


def test_an_unverifiable_kind_refuses_rather_than_reporting_success():
    """'we did not check' and 'we checked and it was fine' must never be one answer on a
    page an operator uses to decide a POV is ready.

    Raised rather than returned as `(False, …)` on purpose: a refusal about the REQUEST
    is not a check outcome, and recording it against the row would make an Entitle tenant
    read as broken when nothing was ever checked."""
    import asyncio
    tenant = t.Tenant(id="x", kind="entitle", name="e", base_url="https://api.entitle.io/v1",
                      client_id="", secret="tok")
    try:
        asyncio.run(bt_tenant_verify.verify(tenant))
        raise AssertionError("an unverifiable kind reported success")
    except t.BTTenantError as exc:
        assert "cannot be verified" in str(exc)


def test_a_failed_check_is_a_return_value_not_an_exception():
    """The shape CodeQL kept flagging. "These credentials do not work" is the EXPECTED
    outcome of a credential check; modelling it as an exception meant the endpoint caught
    one and put its `str()` — transport text, local paths, a chained cause — into an HTTP
    response body."""
    import asyncio
    # No client id or secret: a refusal the verifier can reach without any network.
    tenant = t.Tenant(id="x", kind="pra", name="p", base_url="acme.beyondtrustcloud.com",
                      client_id="", secret="")
    ok, message = asyncio.run(bt_tenant_verify.verify(tenant))
    assert ok is False
    assert "API account" in message, "the remedy must survive the shape change"


def test_the_verify_endpoint_builds_no_message_from_a_caught_exception():
    """Static, and the counterpart to the branch check below. `verify` returns its
    outcome, so there is no exception at that boundary to echo — this pins that nothing
    reintroduces one."""
    src = (pathlib.Path(_ROOT) / "web_dashboard" / "api" / "bt_tenants.py").read_text(
        encoding="utf-8")
    handler = src.split("async def verify_tenant", 1)[1].split("@router", 1)[0]
    assert "str(exc)" not in handler, (
        "verify_tenant builds a response message from a caught exception again")


def test_a_transport_failure_never_carries_the_exception_text():
    """A caught exception's `str()` is written for a developer reading a traceback, not
    for an HTTP response body: it can carry local paths, the resolved address behind a
    hostname, or a chained cause from somewhere unrelated. CodeQL flagged exactly this
    flow, and it was right to."""
    import httpx

    leaky = httpx.ConnectError(
        "[Errno -2] Name or service not known while connecting to "
        "/home/runner/work/secret-path — resolved 10.1.2.3")
    reason = bt_tenant_verify._http_reason(leaky)
    assert "secret-path" not in reason and "10.1.2.3" not in reason
    assert "DNS" in reason, "the useful part of the diagnosis must survive"

    # The fallback is the one that used to be a bare str(exc).
    class Weird(Exception):
        pass
    fallback = bt_tenant_verify._http_reason(Weird("/var/lib/dashboard/token=abc123"))
    assert "abc123" not in fallback
    assert "Weird" in fallback, "the exception TYPE is the diagnostic part, and is safe"


def test_the_verify_endpoint_does_not_echo_an_unexpected_exception():
    """Static, deliberately. The property is about one branch of one handler, and the
    alternative — standing up FastAPI to force an unexpected exception through it —
    tests the mock more than the code. A BTTenantError above it is an authored refusal
    and DOES carry its message; that is the distinction being pinned."""
    src = (pathlib.Path(_ROOT) / "web_dashboard" / "api" / "bt_tenants.py").read_text(
        encoding="utf-8")
    marker = "except Exception as exc:"
    assert marker in src, "the unexpected-exception branch moved"
    # From that except to the end of the handler, which the next decorator starts.
    branch = src.split(marker, 1)[1].split("@router", 1)[0]
    assert "str(exc)" not in branch, (
        "the unexpected-exception branch echoes the exception into the response")
    assert "type(exc).__name__" in branch, "the type is what should reach the operator"


# ── url normalisation ────────────────────────────────────────────────────────

def test_api_base_matches_the_pra_client():
    """A second copy of this rule that drifts would send a verified tenant's calls
    somewhere the verify never touched."""
    from web_dashboard.services import pra_api_service
    for raw in ("acme.beyondtrustcloud.com", "https://acme.beyondtrustcloud.com",
                "https://acme.beyondtrustcloud.com/"):
        with _singletons(pra_api_service, bt_api_host=raw):
            expected = pra_api_service._host()
        got = t.Tenant(id="", kind="pra", name="n", base_url=raw, client_id="",
                       secret="s").api_base
        assert got == expected, f"{raw!r}: {got!r} != {expected!r}"


def test_api_base_matches_the_password_safe_client():
    """pscli configs store either the bare host or the full /BeyondTrust/api/public/v3
    path, and both are things an operator will paste."""
    from web_dashboard.services import ps_api_service
    for raw in ("acme.ps.beyondtrustcloud.com",
                "https://acme.ps.beyondtrustcloud.com",
                "https://acme.ps.beyondtrustcloud.com/BeyondTrust/api/public/v3"):
        with _singletons(ps_api_service, pscli_api_url=raw):
            expected = ps_api_service._base_url()
        got = t.Tenant(id="", kind="password_safe", name="n", base_url=raw,
                       client_id="", secret="s").api_base
        assert got == expected, f"{raw!r}: {got!r} != {expected!r}"


# ── seeding ──────────────────────────────────────────────────────────────────

def test_the_seed_runs_once_and_is_marked_done():
    """An operator who seeds, deletes the seeded row on purpose and restarts must not
    have it come back.

    The marker is the one thing here read through ``config_service`` rather than ``_cfg``,
    so it is the one thing that touches a real row. Pointed at a throwaway key for the
    duration: deleting an install's real marker would arm a re-seed on its next boot.
    """
    for kind in t.VALID_KINDS:
        _clear(kind)
    original_mark, t._SEED_MARK = t._SEED_MARK, "test_bt_tenant_seed_mark"
    db = d.SessionLocal()
    try:
        with _singletons(t, bt_api_host="seed.beyondtrustcloud.com", bt_client_id="i",
                         bt_client_secret="s"):
            assert t.seed_from_settings(db) == 1, \
                "exactly the one configured kind should have been seeded"
            seeded = db.query(d.BeyondTrustTenant).filter(
                d.BeyondTrustTenant.kind == "pra").all()
            assert len(seeded) == 1 and seeded[0].is_default
            assert t.resolve(db, "pra", seeded[0].id).secret == "s", \
                "the seeded secret must be readable back through the one reader"

            db.delete(seeded[0])
            db.commit()
            assert t.seed_from_settings(db) == 0, \
                "the seed came back after a deliberate delete"
    finally:
        db.close()
        db2 = d.SessionLocal()
        db2.query(d.AppConfig).filter(d.AppConfig.key == t._SEED_MARK).delete(
            synchronize_session=False)
        db2.commit()
        db2.close()
        config_service.invalidate()
        t._SEED_MARK = original_mark


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
