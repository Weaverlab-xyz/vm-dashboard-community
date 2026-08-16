"""The remote-agent signing audience: visibility, reset, and the mint-time guard.

Written after a live failure on 2026-08-03. On a split-vhost deployment — the UI on one
hostname, ``/api/agent*`` on another — the Settings field defaults to the origin the admin's
browser is on, which is the *UI* hostname. Minting one enrolment code pinned the audience to
it permanently. Correcting the field afterwards did nothing, because pinning is write-once
and the pin wins. The agent dialled the UI vhost, got a 404 from it, and the console showed
``enrolling`` / ``Last seen: never`` / no policy hash — indistinguishable from a revoked
agent, with no ``/api/agent/enroll`` request in the log at all, because none ever arrived.
Recovery took a shell inside the container calling ``config_service.set`` by hand.

Three properties, and the tension between them is the whole point:

  * **The pin stays unwritable from an unauthenticated request.** Write-once exists so the
    first stranger to reach ``/api/agent/lease`` with a forged ``Host`` cannot pin an
    audience they control — the one thing that would make a signature captured against a
    host of theirs replayable here. Making the pin resettable must not weaken that.
  * **Reset is admin-only**, the same gate as minting a code, and takes no value from the
    caller: it clears the key and leaves the existing write-once path to re-pin.
  * **A mint refuses** when the pin contradicts ``public_base_url``, rather than emitting a
    stale URL, and **warns** when the audience looks like the operator's own UI origin.

Behavioural, against a real SQLite database. The parts of this that no single request can
show — that ``persist=True`` still has exactly two callers, that the reset accepts no value,
that the mint guard runs before anything is mutated, and the route ordering — are static
assertions in ``test_agent_lease_invariants.py``, which needs no dependencies and so still
runs where these skip.

Runs under pytest, or standalone:
    python tests/test_agent_audience.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-audience-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-audience-tests")
# The settings fallback for public_base_url, so a real one in the environment cannot make
# these tests pass or fail for reasons that have nothing to do with the code.
os.environ["PUBLIC_BASE_URL"] = ""


try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from web_dashboard.database import Base, RemoteAgent, SessionLocal, engine, get_db
    from web_dashboard.api import agent as agent_api
    from web_dashboard.api.auth import require_admin
    from web_dashboard.services import agent_signing, config_service, public_url
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

PINNED = "https://agents.test"
ORIGIN = "http://testserver"          # what TestClient's requests look like
_FORBIDDEN = HTTPException(status_code=403, detail="Admin privileges required")


class _Admin:
    username = "tester"
    is_admin = True
    is_effective_admin = True


def _deny():
    raise _FORBIDDEN


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(agent_api.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: _Admin()
    return TestClient(app)


CLIENT = _app()

# A second client whose admin check fails, so "admin-only" is tested through the real
# dependency wiring rather than only by reading the source.
ANON = _app()
ANON.app.dependency_overrides[require_admin] = _deny


# ── state helpers ─────────────────────────────────────────────────────────────

def _pin(value: str = PINNED) -> None:
    config_service.set(agent_api._AUDIENCE_CONFIG, value)


def _unpin() -> None:
    config_service.delete(agent_api._AUDIENCE_CONFIG)


def _state(public_base: str = "", pinned: str = PINNED) -> None:
    """Put the two config keys in a known state. Every test starts by calling this, because
    the config store is process-global and a leaked value from one test changes another."""
    if pinned:
        _pin(pinned)
    else:
        _unpin()
    if public_base:
        config_service.set(public_url.CONFIG_KEY, public_base)
    else:
        config_service.delete(public_url.CONFIG_KEY)


def _register(name: str = ""):
    return CLIENT.post("/api/agent", json={"name": name or f"agent-{uuid.uuid4().hex[:8]}",
                                           "site": "dc1"})


def _enrolled(audience: str = PINNED):
    """A fully enrolled agent signing against ``audience``; returns (id, private key)."""
    resp = _register()
    assert resp.status_code == 201, resp.text
    private, public = agent_signing.generate_keypair()
    enrolled = CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": resp.json()["enrollment_code"], "public_key": public,
        "agent_version": "1.0.0", "policy_hash": "b" * 64})
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json()["audience"] == audience
    return enrolled.json()["agent_id"], private


def _lease(private: str, agent_id: str, audience: str = PINNED):
    body = agent_signing.serialize({})
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=audience, method="POST",
        path="/api/agent/lease", body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.post("/api/agent/lease", content=body, headers=headers)


# ── the pin is not writable from an unauthenticated request ───────────────────

def test_a_successful_signed_lease_does_not_pin_the_audience():
    """The signature path resolves an audience to verify against but must never write it.
    If it did, an agent poll would be enough to freeze whatever ``Host`` reached the app."""
    _state(pinned=PINNED)
    agent_id, private = _enrolled()
    _unpin()                                   # nothing pinned from here on
    assert _lease(private, agent_id, audience=ORIGIN).status_code == 200, \
        "with no pin the audience derives from the request, so this should verify"
    assert agent_api._pinned_audience() == "", \
        "a lease pinned the audience — an unauthenticated caller now controls it"


def test_a_forged_host_on_an_unsigned_lease_does_not_pin_the_audience():
    """The attack write-once exists to stop: reach /api/agent/lease with a Host of your
    choosing and freeze the audience to a value you control, which both 401s every real
    agent and makes a signature captured against your host replayable here."""
    _state(pinned="")
    resp = CLIENT.post("/api/agent/lease", json={}, headers={"Host": "evil.test"})
    assert resp.status_code == 401
    assert agent_api._pinned_audience() == "", "an unsigned request pinned the audience"


def test_a_failed_enrolment_does_not_pin_the_audience():
    """Enrolment is allowed to pin, but only *after* redeeming a valid single-use code.
    A bad code with a forged Host must leave the key untouched."""
    _state(pinned="")
    _private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll",
                       json={"enrollment_code": "agte_" + "0" * 64, "public_key": public},
                       headers={"Host": "evil.test"})
    assert resp.status_code == 400
    assert agent_api._pinned_audience() == "", "a refused enrolment pinned the audience"


def test_reading_or_resetting_the_audience_requires_an_admin():
    """The same gate as minting a code. Without it, the reset would be a way for anything
    that can reach an agent endpoint to un-pin the audience at will."""
    _state(pinned=PINNED)
    assert ANON.get("/api/agent/audience").status_code == 403
    assert ANON.delete("/api/agent/audience").status_code == 403
    assert agent_api._pinned_audience() == PINNED, "a refused reset cleared the pin anyway"


def test_the_audience_cannot_be_set_through_its_own_route():
    """Read and reset only. A POST/PUT/PATCH here would be a value channel into a
    write-once key."""
    _state(pinned=PINNED)
    for method in ("POST", "PUT", "PATCH"):
        resp = CLIENT.request(method, "/api/agent/audience", json={"audience": "https://evil.test"})
        assert resp.status_code in (404, 405), f"{method} /api/agent/audience is routed"
    assert agent_api._pinned_audience() == PINNED


# ── visibility ────────────────────────────────────────────────────────────────

def test_the_pinned_audience_is_readable():
    """The gap this closes: `agent_base_url` had exactly one reader in the codebase and no
    way to see it, so an audience pinned to the wrong hostname was undiagnosable."""
    _state(public_base=PINNED, pinned=PINNED)
    body = CLIENT.get("/api/agent/audience").json()
    assert body["pinned"] == PINNED
    assert body["public_base_url"] == PINNED
    assert body["effective"] == PINNED
    assert body["conflict"] == "" and body["warnings"] == []


def test_the_reset_route_is_not_swallowed_by_the_agent_id_route():
    """`DELETE /{agent_id}` is declared first and a path param matches one segment, so a
    404 here would mean the route ordering silently broke."""
    _state(pinned=PINNED)
    resp = CLIENT.delete("/api/agent/audience")
    assert resp.status_code == 200, resp.text
    assert "Agent not found" not in resp.text


def test_the_unpinned_state_reports_what_a_mint_would_pin():
    _state(pinned="")
    body = CLIENT.get("/api/agent/audience").json()
    assert body["pinned"] == ""
    assert body["effective"] == ORIGIN
    assert any("permanently pins" in w for w in body["warnings"])


def test_the_reset_counts_the_agents_it_will_break():
    """Resetting invalidates every enrolled agent, and the count is what makes that
    concrete in the confirm dialog rather than an abstraction the operator skips."""
    _state(pinned=PINNED)
    _enrolled()
    before = CLIENT.get("/api/agent/audience").json()["agents_enrolled"]
    assert before >= 1
    resp = CLIENT.delete("/api/agent/audience").json()
    assert resp["previous"] == PINNED
    assert resp["agents_affected"] == before
    assert str(before) in resp["detail"], "the detail should name the number affected"


def test_resetting_an_unpinned_audience_is_a_no_op_not_an_error():
    _state(pinned="")
    resp = CLIENT.delete("/api/agent/audience")
    assert resp.status_code == 200
    assert resp.json()["previous"] == ""
    assert "nothing to reset" in resp.json()["detail"]


# ── the reset restores the write-once path ────────────────────────────────────

def test_the_next_mint_after_a_reset_repins_from_public_base_url():
    """The recovery the docs described and did not provide. Reset does not take an audience
    — it clears the key so the ordinary admin mint pins it again from the stated URL."""
    _state(public_base="https://agents.example.test", pinned=PINNED)
    assert CLIENT.delete("/api/agent/audience").status_code == 200
    assert agent_api._pinned_audience() == ""

    resp = _register()
    assert resp.status_code == 201, resp.text
    install = resp.json()["install"]
    assert install["dashboard_url"] == "https://agents.example.test"
    assert "DASHBOARD_URL=https://agents.example.test" in install["docker_run"]
    assert agent_api._pinned_audience() == "https://agents.example.test"


def test_an_enrolled_agent_stops_authenticating_after_a_reset():
    """The consequence the confirm dialog warns about, asserted rather than asserted-about:
    each agent signs against the audience it was handed at enrolment."""
    _state(public_base="", pinned=PINNED)
    agent_id, private = _enrolled()
    assert _lease(private, agent_id).status_code == 200

    CLIENT.delete("/api/agent/audience")
    _pin("https://moved.test")                 # as the next mint would
    resp = _lease(private, agent_id)
    assert resp.status_code == 401, "the old signature must stop verifying"


# ── the mint-time guard ───────────────────────────────────────────────────────

def test_minting_is_refused_when_the_pin_contradicts_public_base_url():
    """The live failure. Correcting the Settings field looked like it worked and changed
    nothing, because the modal built its docker run from the stale pin.

    The two URLs are checked by equality on the structured fields and then looked for in the
    prose *through those fields*, rather than against literals of their own. That is the
    stronger assertion — it ties the sentence to the values it was built from, so rewording
    the prose cannot leave it naming one hostname and reporting another — and it keeps a
    bare-URL ``in`` check out of the file, which CodeQL flags as incomplete URL
    sanitization (rightly, for the authorization checks that query is written for).
    """
    _state(public_base="https://dash.corrected.test", pinned="https://ui.stale.test")
    resp = _register()
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "agent_audience_conflict"
    assert detail["pinned"] == "https://ui.stale.test"
    assert detail["public_base_url"] == "https://dash.corrected.test"
    # An operator cannot act on "these disagree" without being told which is which.
    assert detail["pinned"] in detail["message"]
    assert detail["public_base_url"] in detail["message"]
    assert "reset" in detail["message"].lower(), "the refusal must name the remedy"


def test_a_refused_mint_creates_no_agent_row():
    """The guard runs before create_agent. Otherwise a refusal would leave a row squatting
    its name — create_agent enforces uniqueness across all rows — holding a code nobody
    ever saw."""
    _state(public_base="https://dash.corrected.test", pinned="https://ui.stale.test")
    name = f"blocked-{uuid.uuid4().hex[:8]}"
    assert CLIENT.post("/api/agent", json={"name": name}).status_code == 409

    db = SessionLocal()
    try:
        assert db.query(RemoteAgent).filter(RemoteAgent.name == name).first() is None, \
            "the refused mint left an agent row behind"
    finally:
        db.close()

    # And the name is still free once the conflict is resolved.
    _state(public_base="https://ui.stale.test", pinned="https://ui.stale.test")
    assert CLIENT.post("/api/agent", json={"name": name}).status_code == 201


def test_a_refused_reissue_leaves_the_running_container_working():
    """reissue_enroll_code clears the public key. Refusing after that would revoke a working
    agent as a side effect of an error message."""
    _state(public_base="", pinned=PINNED)
    agent_id, private = _enrolled()
    assert _lease(private, agent_id).status_code == 200

    config_service.set(public_url.CONFIG_KEY, "https://dash.corrected.test")
    resp = CLIENT.post(f"/api/agent/{agent_id}/enrollment-code")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "agent_audience_conflict"

    _state(public_base="", pinned=PINNED)      # the guard is the only thing that ran
    assert _lease(private, agent_id).status_code == 200, \
        "the refused reissue cleared the agent's key and locked out its container"


def test_an_acknowledged_mismatch_mints_against_the_pin_and_says_so():
    """The mismatch is not always a mistake: ``public_base_url`` is also the OAuth callback
    origin, so on a split-vhost install the two legitimately differ and without an override
    this guard would block agent registration permanently. The refusal is about not emitting
    a stale URL *silently* — an admin who asked again gets it, labelled."""
    _state(public_base="https://dash.corrected.test", pinned="https://ui.stale.test")
    resp = CLIENT.post("/api/agent", json={"name": f"ack-{uuid.uuid4().hex[:8]}"},
                       params={"acknowledge_audience": "true"})
    assert resp.status_code == 201, resp.text
    install = resp.json()["install"]
    assert install["dashboard_url"] == "https://ui.stale.test", \
        "the acknowledged mint must use the pin, not public_base_url"
    assert any("NOT the https://dash.corrected.test" in w for w in install["warnings"]), \
        install["warnings"]


def test_the_refusal_names_its_own_override():
    """A dead-end 409 sends the operator to the source to find out it was not one."""
    _state(public_base="https://dash.corrected.test", pinned="https://ui.stale.test")
    detail = CLIENT.post("/api/agent", json={"name": "x"}).json()["detail"]
    assert detail["override"] == "acknowledge_audience=true"


def test_acknowledging_does_not_move_the_pin():
    """The override is permission to *use* the pinned audience once, not to re-pin. Silently
    re-pinning from public_base_url here would invalidate every enrolled agent as a side
    effect of registering one new one."""
    _state(public_base="https://dash.corrected.test", pinned="https://ui.stale.test")
    CLIENT.post("/api/agent", json={"name": f"ack2-{uuid.uuid4().hex[:8]}"},
                params={"acknowledge_audience": "true"})
    assert agent_api._pinned_audience() == "https://ui.stale.test"


def test_the_mint_warns_when_the_audience_is_the_operators_own_origin():
    """Point of the warning: on a split-vhost deployment this value is the UI hostname, and
    an agent pointed at it gets a 404 and never enrols. Correct for a single-host install,
    so it warns rather than refusing."""
    _state(public_base="", pinned=ORIGIN)
    warnings = _register().json()["install"]["warnings"]
    assert any("the same origin you are browsing" in w for w in warnings), warnings


def test_the_mint_warns_before_pinning_for_the_first_time():
    """The moment the live failure was created. Nothing is pinned, so this mint decides the
    audience permanently — from the admin's own browser origin."""
    _state(public_base="", pinned="")
    warnings = _register().json()["install"]["warnings"]
    assert any("permanently pins" in w and ORIGIN in w for w in warnings), warnings


def test_the_mint_warns_about_a_plaintext_audience():
    """The documented http:// trap: the agent refuses to sign over plaintext, and an
    untrusted proxy is the usual reason an https deployment pins an http audience."""
    _state(public_base="", pinned="http://dash.internal.test")
    warnings = _register().json()["install"]["warnings"]
    assert any("AGENT_INSECURE_TLS" in w for w in warnings), warnings


def test_a_matching_pin_and_public_base_url_mints_silently():
    """Guard the guards: warnings that fire on a correct configuration are noise, and noise
    is what gets the real ones ignored."""
    _state(public_base=PINNED, pinned=PINNED)
    body = _register().json()
    assert body["install"]["warnings"] == []
    assert body["install"]["dashboard_url"] == PINNED


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
