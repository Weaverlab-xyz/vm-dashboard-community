"""The read-only POV endpoints, driven through the real router.

Slice 1 is read-only on purpose: it proves auth, the 423/Retry-After retry and the
keep_idle guarantee against a real account before any code path can leave a resource
behind. So what matters here is not the happy path — it is that the three ways this can
fail stay *distinguishable*, because collapsing them is how "Skytap isn't set up" ends up
looking like a dashboard bug:

    400  you asked for a platform that does not exist
    409  the platform exists but has no credentials — fix it in Settings
    502  the platform answered badly — its own words are carried through

Mounts the router on a bare FastAPI app rather than importing `main`, so no setup-complete
middleware and no feature-gate wiring is in the way. The gate itself is covered by
tests/test_install_profile.py.

**It does need a database now, and the 502 above is why.** `/environments` and `/templates`
are served out of `pov_platform_cache` rather than off the platform, so the ladder only
still holds on a COLD row: with a last-known-good listing in the table those two routes
answer 200 and a `note`, deliberately, because a rate-limited account must not be able to
empty a table that was correct a minute ago. `_Fixture` therefore clears the cache on
entry — every test below is testing the cold path, which is the only one that can still
reach the platform and so the only one where these assertions mean anything.

Runs under pytest, or standalone:
    python tests/test_pov_api.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-api")

try:
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP: fastapi/httpx not installed")
    sys.exit(0)

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.api import pov as pov_api  # noqa: E402
from web_dashboard.api.auth import get_current_user  # noqa: E402
from web_dashboard.services import pov_platform_cache, skytap_service  # noqa: E402
from web_dashboard.services.skytap_client import SkytapClient, SkytapCreds  # noqa: E402

_CREDS = SkytapCreds(username="u", api_token="t", base_url="https://skytap.test")


class _Operator:
    """An authenticated SE. A bare ``object()`` used to be enough here, because nothing on
    this router checked a permission -- every route was `get_current_user` only. They are
    gated on the `pov` scope now, so the principal has to be able to answer the question:
    `has_permission` reads `effective_permissions_dict`, and an object without it raises
    AttributeError inside the dependency, which surfaces as a 500 rather than a 403."""
    username = "tester"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}
    accessor_env_id = None
    pov_env_ids_list = []


def _db():
    db = d.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _app():
    app = FastAPI()
    app.include_router(pov_api.router)
    app.dependency_overrides[get_current_user] = lambda: _Operator()
    # The two cached listings take a session. Overridden rather than left to the real
    # `get_db` so the router's other routes cannot reach a half-set-up one.
    app.dependency_overrides[d.get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _clear_cache():
    """Empty `pov_platform_cache`, so the next read takes the cold path and reaches the
    fake transport. Without this the first test to populate a row decides the answer for
    every test after it — and they would pass while testing nothing."""
    db = d.SessionLocal()
    db.query(d.PovPlatformCache).delete(synchronize_session=False)
    db.commit()
    db.close()
    # The project scope is read from config, which this file does not set. Pinned so a
    # developer's own `skytap_project_id` cannot move the row these tests address.
    pov_platform_cache.scope_of = lambda platform: ""


def _age_cache(seconds=None):
    """Put every row's read time far enough back that the minimum-refresh floor and the
    per-platform pacing gap do not block the claim a test is about to make."""
    from datetime import timedelta
    if seconds is None:
        seconds = pov_platform_cache.min_refresh_interval_seconds() + 5
    db = d.SessionLocal()
    db.query(d.PovPlatformCache).update(
        {d.PovPlatformCache.fetched_at:
            pov_platform_cache._utcnow() - timedelta(seconds=seconds),
         d.PovPlatformCache.next_query_allowed_at: None},
        synchronize_session=False)
    db.commit()
    db.close()


class _Fixture:
    """Swap the adapter's credentials + transport for the duration of a test."""

    def __init__(self, handler, configured=True, keep_cache=False):
        self.handler = handler
        self.configured = configured
        # Every test here wants a cold row -- see the module docstring -- except the one
        # whose whole subject is what happens when there IS a listing to serve.
        self.keep_cache = keep_cache

    def __enter__(self):
        if not self.keep_cache:
            _clear_cache()
        self._client = skytap_service._client
        self._conf = skytap_service.configured

        async def _sleep(_s):
            pass

        cli = SkytapClient(_CREDS, transport=httpx.MockTransport(self.handler),
                           sleep=_sleep)
        skytap_service._client = lambda: cli
        skytap_service.configured = lambda: self.configured
        return self

    def __exit__(self, *a):
        skytap_service._client = self._client
        skytap_service.configured = self._conf


def _json(payload, status=200):
    return lambda _req: httpx.Response(status, json=payload)


# ── the registry endpoint ────────────────────────────────────────────────────

def test_platforms_lists_the_registry_with_capabilities():
    """Capabilities go to the UI so a platform that cannot make a share link renders
    'PRA only' rather than a button that 502s."""
    with _Fixture(_json([]), configured=True):
        r = _app().get("/api/pov/platforms")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default"] == "skytap"
    sky = next(p for p in body["platforms"] if p["name"] == "skytap")
    assert sky["configured"] is True
    assert sky["share_link"] is True
    assert sky["bootstrap_injection"] == "metadata"


def test_platforms_reports_unconfigured_without_failing():
    with _Fixture(_json([]), configured=False):
        r = _app().get("/api/pov/platforms")
    assert r.status_code == 200
    sky = next(p for p in r.json()["platforms"] if p["name"] == "skytap")
    assert sky["configured"] is False


# ── the three distinguishable failures ───────────────────────────────────────

def test_an_unknown_platform_is_a_400():
    with _Fixture(_json([])):
        r = _app().get("/api/pov/templates?platform=cloudshare")
    assert r.status_code == 400, r.text
    assert "skytap" in r.json()["detail"], "the 400 should name what IS supported"


def test_an_unconfigured_platform_is_a_409_that_says_where_to_fix_it():
    with _Fixture(_json([]), configured=False):
        r = _app().get("/api/pov/environments")
    assert r.status_code == 409, r.text
    assert "Settings" in r.json()["detail"]


def test_an_upstream_failure_is_a_502_carrying_the_platform_words():
    """502, not 500: the dashboard did not fail, and the distinction is what stops an SE
    debugging the wrong system."""
    with _Fixture(_json({"error": "template 999 is not yours"}, status=500)):
        r = _app().get("/api/pov/templates")
    assert r.status_code == 502, r.text
    assert "not yours" in r.json()["detail"]


def test_a_bad_token_surfaces_as_502_naming_the_token():
    with _Fixture(_json({"error": "unauthorized"}, status=401)):
        r = _app().get("/api/pov/environments")
    assert r.status_code == 502
    assert "API token" in r.json()["detail"], \
        "a 401 should tell the operator it is the token, not the password"


def test_the_502_is_only_for_a_cold_row():
    """The boundary between the ladder above and the cache, and the reason the cache
    exists at all: once there is a listing to serve, a platform that starts refusing must
    not be able to empty a table that was correct a minute ago. Before, every SE's page
    load went to the platform, so one 423 was an error banner for all of them.

    Note what is asserted in the same breath — the row says it is NOT current. Serving an
    old listing is only defensible while the page admits it is old."""
    good = [{"id": "7", "name": "poc-alpha", "runstate": "running"}]
    with _Fixture(_json(good)):                     # cold: this one goes to the platform
        assert _app().get("/api/pov/environments").status_code == 200

    # Now the account starts refusing, and the SE presses Re-check -- the press somebody
    # makes when a page looks wrong, and the one that used to make a throttle worse.
    with _Fixture(_json({"error": "the account is unavailable"}, status=500),
                  keep_cache=True):
        _age_cache()                                # past the minimum-refresh floor
        r = _app().get("/api/pov/environments?refresh=true")

    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["id"] for e in body["environments"]] == ["7"], \
        "a 500 replaced the last good listing"
    assert body["as_of"], "a served listing must say when it was read"
    # `note`, not `stale`. They are different axes and this is the case that shows it:
    # the listing is well inside its TTL (so `stale` is correctly False) while the
    # platform is refusing right now. `note` is the field the page renders, and it is the
    # only one that can carry "current data, dead platform".
    assert "unavailable" in body["note"], \
        "the page was handed a listing with nothing to say the platform is refusing"


# ── the reads ────────────────────────────────────────────────────────────────

def test_environments_are_listed_and_normalised():
    payload = [{"id": 7, "name": "poc-alpha", "runstate": "running",
                "region": "US-West", "rate_limited": True, "suspend_on_idle": 3600}]
    with _Fixture(_json(payload)):
        r = _app().get("/api/pov/environments")
    assert r.status_code == 200, r.text
    env = r.json()["environments"][0]
    assert env["id"] == "7", "ids must be strings — the POV row will key on this"
    assert env["runstate"] == "running"
    assert env["rate_limited"] is True
    assert env["vm_count"] is None, "an unmeasured count must be absent, never 0"


def test_templates_are_listed():
    with _Fixture(_json([{"id": "3", "name": "OT cell", "description": "PLC + HMI"}])):
        r = _app().get("/api/pov/templates")
    assert r.status_code == 200, r.text
    t = r.json()["templates"][0]
    assert t["name"] == "OT cell" and t["description"] == "PLC + HMI"


def test_one_environment_comes_back_with_its_vms():
    payload = {"id": "7", "name": "poc-alpha", "runstate": "running",
               "vms": [{"id": "v1", "name": "dc01", "guestos": "Windows Server 2022",
                        "interfaces": [{"ip": "10.0.0.4"}]}]}
    with _Fixture(_json(payload)):
        r = _app().get("/api/pov/environments/7")
    assert r.status_code == 200, r.text
    env = r.json()["environment"]
    assert env["vm_count"] == 1
    assert env["vms"][0]["private_ip"] == "10.0.0.4"
    assert env["vms"][0]["os_family"] == "windows"


def test_every_read_reaches_skytap_with_keep_idle():
    """The cost leak with no symptom: a GET without it resets the environment's idle
    timer, so polling defeats suspend_on_idle and only the invoice notices."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    with _Fixture(handler):
        client = _app()
        client.get("/api/pov/environments")
        client.get("/api/pov/templates")
    assert seen, "no request reached the platform"
    assert all("keep_idle=true" in u for u in seen), \
        f"a read went out without keep_idle: {seen}"


def test_a_busy_platform_is_retried_rather_than_surfaced_as_an_error():
    """423 is Skytap saying 'busy', not 'broken'. Reporting it as a failure would make
    ordinary concurrent use look like an outage."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(423, headers={"Retry-After": "1"})
        return httpx.Response(200, json=[{"id": "1", "name": "poc"}])

    with _Fixture(handler):
        r = _app().get("/api/pov/environments")
    assert r.status_code == 200, r.text
    assert calls["n"] == 2, "the busy response was not retried"


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
