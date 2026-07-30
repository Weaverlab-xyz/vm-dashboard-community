"""Brute-force throttling on the login endpoint.

The gap this closes: `POST /api/auth/login` had no rate limit of any kind. The app
builds a SlowAPI limiter but never adds `SlowAPIMiddleware` and carries no
`@limiter.limit` decorators, so `rate_limit_per_minute` did nothing at all — bcrypt's
cost was the only thing between an attacker and an unlimited guess rate.

What these assertions are really protecting:

  * **The cap is keyed on the username**, which an attacker cannot rotate. A per-IP cap
    alone would be defeated by one spoofed `X-Forwarded-For` header per request, since
    the shipped `trusted_proxy_hosts` default is `"*"`.
  * **It is a sliding window, not a lockout**, or anyone who knows a username gets a
    denial-of-service against that account.
  * **It reveals nothing.** An unknown username must throttle exactly like a real one,
    or the throttle becomes the enumeration oracle it exists to slow down.
  * **A success clears the user's budget but not the address's**, or one person
    remembering their password resets the allowance for whoever is spraying from the
    same NAT.

Uses a temporary SQLite file and the real ORM. Skips if app deps are absent. Runs under
pytest, or standalone:
    python tests/test_login_guard.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="login-guard-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-login-guard-tests")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.database import (Base, LoginAttempt, SessionLocal, User, engine,
                                        get_db, get_password_hash)
    from web_dashboard.services import config_service, login_guard
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)


def _db():
    return SessionLocal()


def _reset():
    db = _db()
    try:
        db.query(LoginAttempt).delete()
        db.commit()
    finally:
        db.close()


def _fail_n(n: int, *, username: str, ip: str = "", at: datetime = None):
    db = _db()
    try:
        for i in range(n):
            login_guard.record_failure(
                db, username=username, ip=ip,
                now=(at or datetime.utcnow()) + timedelta(milliseconds=i))
    finally:
        db.close()


def _check(username: str, ip: str = ""):
    db = _db()
    try:
        login_guard.check(db, username=username, ip=ip)
        return None
    except login_guard.LoginThrottled as exc:
        return exc
    finally:
        db.close()


# ── the cap ───────────────────────────────────────────────────────────────────

def test_under_the_cap_is_allowed():
    _reset()
    _fail_n(login_guard.DEFAULT_MAX_PER_USER - 1, username="alice")
    assert _check("alice") is None


def test_at_the_cap_is_refused():
    _reset()
    _fail_n(login_guard.DEFAULT_MAX_PER_USER, username="alice")
    exc = _check("alice")
    assert exc is not None and exc.scope == "user"
    assert exc.retry_after >= 1


def test_the_retry_after_is_when_the_block_actually_lifts():
    """A client that honours Retry-After should not have to poll to find out it was
    let back in, so the value has to be the real moment the count drops below the cap
    — not a fixed guess."""
    _reset()
    cap = login_guard.DEFAULT_MAX_PER_USER
    window = timedelta(minutes=login_guard.DEFAULT_WINDOW_MINUTES)
    now = datetime.utcnow()
    # Oldest failure 10 minutes ago, the rest just now.
    _fail_n(1, username="alice", at=now - timedelta(minutes=10))
    _fail_n(cap - 1, username="alice", at=now)

    exc = _check("alice")
    assert exc is not None
    # The oldest must age out; that is 5 minutes away (15-minute window, 10 elapsed).
    expected = int((now - timedelta(minutes=10) + window - now).total_seconds())
    assert abs(exc.retry_after - expected) <= 5, (
        f"retry_after {exc.retry_after}s should be ~{expected}s")


def test_failures_outside_the_window_do_not_count():
    """Sliding, not cumulative — otherwise an account accretes failures forever and
    eventually locks out on a typo months later."""
    _reset()
    old = datetime.utcnow() - timedelta(minutes=login_guard.DEFAULT_WINDOW_MINUTES + 5)
    _fail_n(login_guard.DEFAULT_MAX_PER_USER * 2, username="alice", at=old)
    assert _check("alice") is None


def test_the_block_is_not_a_lockout():
    """Anyone who knows a username could otherwise deny that account service
    indefinitely just by failing to log in as them."""
    _reset()
    cap = login_guard.DEFAULT_MAX_PER_USER
    window = login_guard.DEFAULT_WINDOW_MINUTES
    # A full budget burned, but long enough ago that it has aged out.
    _fail_n(cap, username="alice", at=datetime.utcnow() - timedelta(minutes=window + 1))
    assert _check("alice") is None, "the block must lift on its own"


# ── keys ──────────────────────────────────────────────────────────────────────

def test_the_username_key_is_case_folded():
    """Otherwise `Alice`, `alice` and `ALICE` each get a fresh allowance."""
    _reset()
    for name in ("alice", "Alice", "ALICE", "aLiCe"):
        _fail_n(3, username=name)
    assert _check("alice") is not None
    assert _check("ALICE") is not None


def test_an_unknown_username_throttles_identically():
    """The throttle must not answer 'does this user exist?'."""
    _reset()
    _fail_n(login_guard.DEFAULT_MAX_PER_USER, username="nobody-here-at-all")
    assert _check("nobody-here-at-all") is not None


def test_one_username_does_not_throttle_another():
    _reset()
    _fail_n(login_guard.DEFAULT_MAX_PER_USER, username="alice")
    assert _check("bob") is None


def test_the_ip_cap_catches_spraying_across_accounts():
    """Per-username alone would let an attacker try ten passwords against each of a
    thousand accounts from one host without ever tripping."""
    _reset()
    for i in range(login_guard.DEFAULT_MAX_PER_IP):
        _fail_n(1, username=f"user{i}", ip="10.0.0.9")
    exc = _check("someone-new", ip="10.0.0.9")
    assert exc is not None and exc.scope == "ip"


def test_the_ip_cap_does_not_affect_other_addresses():
    _reset()
    for i in range(login_guard.DEFAULT_MAX_PER_IP):
        _fail_n(1, username=f"user{i}", ip="10.0.0.9")
    assert _check("someone-new", ip="10.0.0.10") is None


# ── clearing ──────────────────────────────────────────────────────────────────

def test_a_success_clears_the_users_budget():
    _reset()
    _fail_n(login_guard.DEFAULT_MAX_PER_USER, username="alice")
    assert _check("alice") is not None
    db = _db()
    try:
        login_guard.clear(db, username="alice")
    finally:
        db.close()
    assert _check("alice") is None


def test_a_success_does_not_clear_the_address_budget():
    """One legitimate user on a shared NAT must not reset the allowance for whoever
    else is behind it.

    Deliberately overshoots the cap: clearing one username removes exactly that
    username's rows, so with a budget burned to precisely the cap a single clear would
    drop it under and this would pass for the wrong reason.
    """
    _reset()
    total = login_guard.DEFAULT_MAX_PER_IP + 5
    for i in range(total):
        _fail_n(1, username=f"user{i}", ip="10.0.0.9")

    db = _db()
    try:
        login_guard.clear(db, username="user0")
        remaining = db.query(LoginAttempt).filter(LoginAttempt.ip == "10.0.0.9").count()
    finally:
        db.close()

    assert remaining == total - 1, "clear() must remove one username's rows, not the IP's"
    assert _check("someone-new", ip="10.0.0.9") is not None


def test_clear_is_case_folded_too():
    _reset()
    _fail_n(login_guard.DEFAULT_MAX_PER_USER, username="Alice")
    db = _db()
    try:
        login_guard.clear(db, username="alice")
    finally:
        db.close()
    assert _check("ALICE") is None


# ── robustness ────────────────────────────────────────────────────────────────

def test_recording_a_failure_never_raises():
    """A throttle that can 500 the login endpoint is worse than no throttle."""
    _reset()
    db = _db()
    try:
        login_guard.record_failure(db, username="x" * 500, ip="y" * 200)
    finally:
        db.close()


def test_the_sweep_drops_rows_past_retention():
    _reset()
    old = datetime.utcnow() - timedelta(
        minutes=login_guard.DEFAULT_RETENTION_MINUTES + 10)
    _fail_n(5, username="alice", at=old)
    _fail_n(2, username="bob")
    db = _db()
    try:
        login_guard._sweep(db)
        remaining = db.query(LoginAttempt).count()
    finally:
        db.close()
    assert remaining == 2, "only the rows inside the retention horizon should survive"


def test_the_guard_can_be_turned_off():
    _reset()
    config_service.set("login_throttle_enabled", "0")
    try:
        config_service.invalidate()
        _fail_n(login_guard.DEFAULT_MAX_PER_USER * 3, username="alice")
        assert _check("alice") is None
    finally:
        config_service.set("login_throttle_enabled", "1")
        config_service.invalidate()


def test_a_zero_cap_disables_that_key_without_disabling_the_other():
    _reset()
    config_service.set("login_max_attempts", "0")
    try:
        config_service.invalidate()
        _fail_n(login_guard.DEFAULT_MAX_PER_USER * 3, username="alice", ip="10.0.0.1")
        assert _check("alice", ip="10.0.0.1") is None, "0 must mean 'no per-user cap'"
    finally:
        config_service.set("login_max_attempts", "")
        config_service.invalidate()


# ── the endpoint ──────────────────────────────────────────────────────────────

def _app() -> TestClient:
    from web_dashboard.api import auth as auth_api
    app = FastAPI()
    app.include_router(auth_api.router)

    def _get():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get
    return TestClient(app)


CLIENT = _app()


def _make_user(password: str = "correct-horse") -> str:
    username = f"u{uuid.uuid4().hex[:10]}"
    db = _db()
    try:
        db.add(User(id=str(uuid.uuid4()), username=username,
                    hashed_password=get_password_hash(password),
                    is_active=True, auth_provider="local"))
        db.commit()
    finally:
        db.close()
    return username


def _login(username: str, password: str):
    return CLIENT.post("/api/auth/login",
                       data={"username": username, "password": password})


def test_the_endpoint_returns_429_with_retry_after():
    """The behaviour the whole module exists for: an unlimited guess rate becomes a
    bounded one."""
    _reset()
    username = _make_user()
    codes = [_login(username, "wrong").status_code
             for _ in range(login_guard.DEFAULT_MAX_PER_USER + 1)]
    assert codes[0] == 401, "the first wrong password is a plain 401"
    assert codes[-1] == 429, f"expected a 429 once over the cap, got {codes}"

    resp = _login(username, "wrong")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    # The message must not distinguish throttled-because-real from
    # throttled-because-guessed.
    assert "password" not in resp.json()["detail"].lower()


def test_the_throttle_blocks_the_correct_password_too():
    """Otherwise it is not a throttle — an attacker who guesses right on attempt
    eleven still gets in."""
    _reset()
    username = _make_user("correct-horse")
    for _ in range(login_guard.DEFAULT_MAX_PER_USER):
        _login(username, "wrong")
    assert _login(username, "correct-horse").status_code == 429


def test_a_successful_login_resets_the_budget():
    _reset()
    username = _make_user("correct-horse")
    for _ in range(login_guard.DEFAULT_MAX_PER_USER - 1):
        assert _login(username, "wrong").status_code == 401
    assert _login(username, "correct-horse").status_code == 200
    # Budget cleared, so the next wrong password is a 401 rather than a 429.
    assert _login(username, "wrong").status_code == 401


def test_an_unknown_username_gets_the_same_treatment():
    """Same status codes in the same order as a real account, so the endpoint does not
    become a user-existence oracle under throttling."""
    _reset()
    ghost = f"ghost{uuid.uuid4().hex[:8]}"
    codes = [_login(ghost, "wrong").status_code
             for _ in range(login_guard.DEFAULT_MAX_PER_USER + 1)]
    assert codes[0] == 401 and codes[-1] == 429


def test_a_throttled_request_never_reaches_bcrypt():
    """Cheap to assert and worth asserting: if the check ran after password
    verification, the throttle would amplify CPU cost rather than cap it."""
    _reset()
    username = _make_user()
    for _ in range(login_guard.DEFAULT_MAX_PER_USER):
        _login(username, "wrong")

    import web_dashboard.database as dbmod
    calls = []
    original = dbmod.verify_password

    def _counting(*a, **kw):
        calls.append(1)
        return original(*a, **kw)

    # api.auth imported verify_password by name, so patch it there.
    import web_dashboard.api.auth as auth_mod
    auth_mod.verify_password = _counting
    try:
        assert _login(username, "wrong").status_code == 429
    finally:
        auth_mod.verify_password = original
    assert not calls, "a throttled request must not run the password hash"


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
