"""The Entitle user-JIT "request access" deep link on a 403 (design doc §Phase 4).

``require_permission`` is supposed to answer a permission denial with a structured
detail payload — ``{message, missing_scope, missing_level, request_access_url}`` —
so the frontend can render a one-click link to the operator's Entitle portal
instead of a dead-end error string.

It never did. ``_build_request_access_link`` referenced ``config_service`` without
importing it, so every call raised ``NameError``; the call site wraps the builder
in a bare ``except Exception`` (deliberately — a malformed deep-link config must
not turn a clean 403 into a 500), which swallowed it. The feature was silently
dead rather than loudly broken, and nothing covered the path.

So this drives the real dependency graph — a real FastAPI app, the real
``require_permission`` dependency, the real builder — and asserts on the response
body. A missing import in the builder fails every test here. It also pins the
half of the contract that must NOT change: when the feature is off or
unconfigured, the detail stays the plain string it has always been.

Runs under pytest, or standalone:
    python tests/test_entitle_request_access_link.py
"""
import json
import logging
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Bind the ORM engine to a throwaway SQLite file before web_dashboard.database is
# imported. Nothing here touches the DB — get_current_user is overridden — but
# importing api.auth constructs the engine.
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="request-access-link-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-request-access-link-tests")

try:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import auth as auth_api
    from web_dashboard.services import config_service
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


PORTAL = "https://acme.entitle.io/portal"


class _RestrictedUser:
    """A non-admin with a real (non-empty) permission set that lacks vms:write.

    Both matter: an admin short-circuits the check, and an *empty* permission
    dict means "unrestricted" (pre-OIDC users), which also short-circuits.
    """
    username = "restricted"
    is_effective_admin = False
    effective_permissions_dict = {"vms": ["read"]}


def _client(**config):
    """A one-route app guarded by vms:write, with the config store faked.

    ``config`` maps config keys to values; anything unset reads as absent. The
    builder imports config_service at call time, so patching the module object
    is what the production code path actually sees.
    """
    strings = {k: v for k, v in config.items() if isinstance(v, str)}
    flags = {k: v for k, v in config.items() if isinstance(v, bool)}

    app = FastAPI()

    @app.get("/guarded")
    async def guarded(user=Depends(auth_api.require_permission("vms", "write"))):
        return {"ok": True}

    app.dependency_overrides[auth_api.get_current_user] = lambda: _RestrictedUser()

    class _Ctx:
        def __enter__(self):
            self._saved = (config_service.get, config_service.get_bool)
            config_service.get = lambda key, default="", **kw: strings.get(key, default)
            config_service.get_bool = lambda key, default=False: flags.get(key, default)
            return TestClient(app)

        def __exit__(self, *exc):
            config_service.get, config_service.get_bool = self._saved
            return False

    return _Ctx()


def test_denial_payload_carries_request_access_url():
    """The bug this file exists for: JIT on + portal set must yield the payload."""
    with _client(entitle_user_jit_enabled=True, entitle_request_portal_url=PORTAL) as c:
        resp = c.get("/guarded")
    assert resp.status_code == 403, resp.status_code
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), f"detail is still the plain string: {detail!r}"
    assert "request_access_url" in detail, detail
    assert detail["request_access_url"] == PORTAL
    assert detail["missing_scope"] == "vms"
    assert detail["missing_level"] == "write"
    assert detail["message"] == "Requires 'vms:write' permission."


def test_deep_link_targets_the_mapped_entitle_resource():
    """Exit criterion 2: the link lands on the right resource, not just the portal."""
    mapping = json.dumps({"vms:write": "res-abc123", "aws:read": "res-other"})
    with _client(
        entitle_user_jit_enabled=True,
        entitle_request_portal_url=PORTAL,
        entitle_resource_ids_json=mapping,
    ) as c:
        resp = c.get("/guarded")
    assert resp.status_code == 403
    assert resp.json()["detail"]["request_access_url"] == f"{PORTAL}/resources/res-abc123"


def test_trailing_slash_on_the_portal_url_does_not_double_up():
    mapping = json.dumps({"vms:write": "res-abc123"})
    with _client(
        entitle_user_jit_enabled=True,
        entitle_request_portal_url=PORTAL + "/",
        entitle_resource_ids_json=mapping,
    ) as c:
        resp = c.get("/guarded")
    assert resp.json()["detail"]["request_access_url"] == f"{PORTAL}/resources/res-abc123"


def test_unmapped_scope_falls_back_to_the_portal_root():
    """A partial resource map must not drop the link for scopes it omits."""
    with _client(
        entitle_user_jit_enabled=True,
        entitle_request_portal_url=PORTAL,
        entitle_resource_ids_json=json.dumps({"aws:read": "res-other"}),
    ) as c:
        resp = c.get("/guarded")
    assert resp.json()["detail"]["request_access_url"] == PORTAL


def test_malformed_resource_map_still_yields_a_portal_link():
    """Bad operator JSON degrades to the portal root, it does not kill the link."""
    with _client(
        entitle_user_jit_enabled=True,
        entitle_request_portal_url=PORTAL,
        entitle_resource_ids_json="{not valid json",
    ) as c:
        resp = c.get("/guarded")
    assert resp.status_code == 403
    assert resp.json()["detail"]["request_access_url"] == PORTAL


def test_detail_stays_a_plain_string_when_jit_is_disabled():
    """The default install must keep the historical error shape."""
    with _client(entitle_request_portal_url=PORTAL) as c:
        resp = c.get("/guarded")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Requires 'vms:write' permission."


def test_detail_stays_a_plain_string_when_the_portal_is_unconfigured():
    """Toggle on but no URL yet — no link is better than a broken one."""
    with _client(entitle_user_jit_enabled=True) as c:
        resp = c.get("/guarded")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Requires 'vms:write' permission."


def test_a_broken_link_builder_is_logged_and_never_breaks_the_403():
    """The bare except is intentional, but it must not hide the next bug.

    A raising builder still has to produce a clean 403 (not a 500), and it has
    to leave a warning behind — the absence of one is exactly why the NameError
    survived for the whole life of the feature.
    """
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("web_dashboard.api.auth")
    logger.addHandler(handler)
    saved_builder = auth_api._build_request_access_link

    def _boom(scope, level):
        raise RuntimeError("secrets backend unreachable")

    auth_api._build_request_access_link = _boom
    try:
        with _client(entitle_user_jit_enabled=True, entitle_request_portal_url=PORTAL) as c:
            resp = c.get("/guarded")
    finally:
        auth_api._build_request_access_link = saved_builder
        logger.removeHandler(handler)

    assert resp.status_code == 403, f"a broken link config must not 500: {resp.status_code}"
    assert resp.json()["detail"] == "Requires 'vms:write' permission."
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert warnings, "the swallowed exception was not logged; the next bug would hide too"
    assert warnings[0].exc_info is not None, "log the traceback, not just the message"
    assert "vms" in warnings[0].getMessage() and "write" in warnings[0].getMessage()


def test_admins_and_unrestricted_users_are_untouched():
    """Guard the two short-circuits above the deep-link code."""
    saved_get_bool = config_service.get_bool
    config_service.get_bool = lambda key, default=False: True
    try:
        app = FastAPI()

        @app.get("/guarded")
        async def guarded(user=Depends(auth_api.require_permission("vms", "write"))):
            return {"ok": True}

        class _Admin:
            username = "root"
            is_effective_admin = True
            effective_permissions_dict = {}

        class _Unrestricted:
            username = "legacy"
            is_effective_admin = False
            effective_permissions_dict = {}  # NULL permissions = full access

        for principal in (_Admin, _Unrestricted):
            app.dependency_overrides[auth_api.get_current_user] = lambda p=principal: p()
            resp = TestClient(app).get("/guarded")
            assert resp.status_code == 200, f"{principal.__name__} got {resp.status_code}"
    finally:
        config_service.get_bool = saved_get_bool


if __name__ == "__main__":
    fns = [v for k_, v in sorted(globals().items()) if k_.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
