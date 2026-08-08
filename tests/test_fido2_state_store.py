"""FIDO2 challenges and OAuth/OIDC CSRF state must survive crossing a process.

The gap this closes: `services/fido2_service` kept both in a module-level dict guarded
by a `threading.Lock`. The lock is the right guard for the wrong hazard — the app runs
`gunicorn -w 2` (Dockerfile), so it made the dict safe against the *threads* of one
worker while leaving the two worker **processes** holding a private copy each.

Every value in that store is written by one request and read by a different one:

  * FIDO2   — `GET /webauthn/login/begin` stores, `POST /webauthn/login/complete` reads
  * OAuth   — `GET /oauth/{azure,oidc}/login` stores, `GET .../callback` reads

Nothing pins a browser to a worker — no sticky sessions, no ip_hash, no affinity
anywhere in the deployment. So the second leg landed on the worker that had never seen
the state roughly half the time, and the user got `/login?error=invalid_state` or
"Invalid or expired FIDO2 challenge" with nothing actually wrong. Worse with replicas.

**Two worker processes are simulated by loading the service module twice**, under two
different names, from the same file. Each copy gets its own module globals — which is
precisely what two processes have, and what a `threading.Lock` cannot help with. If the
store ever regresses to process-local state, `test_*_crosses_workers` fails; the rest of
the file holds the properties that make the DB version safe to rely on.

Uses a temporary SQLite file and the real ORM. Skips if app deps are absent. Runs under
pytest, or standalone:
    python tests/test_fido2_state_store.py
"""
import importlib.util
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="fido2-state-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-fido2-state-tests")

try:
    from web_dashboard.database import Base, EphemeralState, SessionLocal, engine
    import web_dashboard.services.fido2_service as _real_service
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

_SERVICE_PATH = os.path.join(_ROOT, "web_dashboard", "services", "fido2_service.py")


def _worker(tag: str):
    """Load `services/fido2_service` as a fresh module object with its own globals.

    This is the whole point of the file: a second import stands in for the second
    gunicorn worker process. The two copies share the database and nothing else.
    """
    spec = importlib.util.spec_from_file_location(
        f"web_dashboard.services.fido2_service__{tag}", _SERVICE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Two "processes". Built once — module import is the expensive part, and no test
# mutates module state.
w1 = _worker("w1")
w2 = _worker("w2")


def _db():
    return SessionLocal()


def _reset():
    db = _db()
    try:
        db.query(EphemeralState).delete()
        db.commit()
    finally:
        db.close()


def _rows() -> int:
    db = _db()
    try:
        return db.query(EphemeralState).count()
    finally:
        db.close()


# ── The regression itself ─────────────────────────────────────────────────────

def test_the_two_workers_really_are_independent():
    """Guards the harness, not the code. If both names resolved to one module object
    the cross-worker tests below would pass no matter how broken the store was."""
    assert w1 is not w2
    assert w1.__dict__ is not w2.__dict__
    assert w1 is not _real_service


def test_oauth_state_crosses_workers():
    """`/oauth/oidc/login` on one worker, `/oauth/oidc/callback` on the other.

    Returning None here is exactly the `/login?error=invalid_state` redirect at
    api/auth.py — SSO simply failing, about half the time, on a default deployment.
    """
    _reset()
    state = str(uuid.uuid4())
    packed = "https://dash.example.com/api/auth/oauth/oidc/callback|pkce-verifier"

    db = _db()
    try:
        w1.store_oauth_state(db, state, packed)
    finally:
        db.close()

    db = _db()
    try:
        assert w2.verify_and_consume_oauth_state(db, state) == packed
    finally:
        db.close()


def test_fido2_challenge_crosses_workers():
    """`/webauthn/login/begin` on one worker, `/webauthn/login/complete` on the other.

    Returning None here is the "Invalid or expired FIDO2 challenge" 400 — MFA failing
    for a user whose key worked perfectly.
    """
    _reset()
    payload = {"state": {"challenge": "abc", "user_verification": "preferred"},
               "user_id": "user-123"}

    db = _db()
    try:
        token = w1.store_fido2_challenge(db, payload)
    finally:
        db.close()

    db = _db()
    try:
        assert w2.fetch_fido2_challenge(db, token) == payload
    finally:
        db.close()


def test_a_separate_session_sees_it_too():
    """The narrower claim, without the module trick: a value written through one
    Session is readable through a different one. Two workers are two sessions plus
    two module copies; this isolates the half that the database is responsible for."""
    _reset()
    state = str(uuid.uuid4())

    writer = _db()
    try:
        w1.store_oauth_state(writer, state, "https://example.test/cb")
    finally:
        writer.close()

    reader = _db()
    try:
        assert w1.verify_and_consume_oauth_state(reader, state) == "https://example.test/cb"
    finally:
        reader.close()


# ── Single-use, which is the security property ────────────────────────────────

def test_oauth_state_is_consumed_exactly_once_across_workers():
    """getdel, not get. A CSRF state that survives its first presentation is not a
    CSRF defence — and the replay would now come from a *different* worker, which is
    the case a process-local dict accidentally covered and a shared table must not
    regress on."""
    _reset()
    state = str(uuid.uuid4())

    db = _db()
    try:
        w1.store_oauth_state(db, state, "https://example.test/cb")
    finally:
        db.close()

    db = _db()
    try:
        assert w2.verify_and_consume_oauth_state(db, state) == "https://example.test/cb"
        assert w2.verify_and_consume_oauth_state(db, state) is None
    finally:
        db.close()

    db = _db()
    try:
        assert w1.verify_and_consume_oauth_state(db, state) is None
    finally:
        db.close()


def test_fido2_challenge_is_consumed_exactly_once():
    """Same single-use rule for the WebAuthn ceremony: a challenge that can be
    replayed is a challenge that is not doing its job."""
    _reset()
    db = _db()
    try:
        token = w1.store_fido2_challenge(db, {"state": {"challenge": "x"}, "user_id": "u"})
    finally:
        db.close()

    db = _db()
    try:
        assert w2.fetch_fido2_challenge(db, token) is not None
        assert w2.fetch_fido2_challenge(db, token) is None
    finally:
        db.close()


def test_consuming_deletes_the_row():
    """The delete is what makes it single-use, so assert the row is really gone
    rather than merely filtered out on read."""
    _reset()
    state = str(uuid.uuid4())
    db = _db()
    try:
        w1.store_oauth_state(db, state, "https://example.test/cb")
        assert _rows() == 1
        w2.verify_and_consume_oauth_state(db, state)
        assert _rows() == 0
    finally:
        db.close()


def test_the_two_namespaces_cannot_consume_each_other():
    """FIDO2 challenges and OAuth states share one table, so the key prefixes are
    the only thing keeping a token from one ceremony valid in the other."""
    _reset()
    db = _db()
    try:
        token = w1.store_fido2_challenge(db, {"state": {}, "user_id": "u"})
        assert w2.verify_and_consume_oauth_state(db, token) is None
        # ...and the challenge is still intact, i.e. the miss consumed nothing.
        assert w2.fetch_fido2_challenge(db, token) is not None
    finally:
        db.close()


def test_unknown_state_is_rejected():
    _reset()
    db = _db()
    try:
        assert w1.verify_and_consume_oauth_state(db, str(uuid.uuid4())) is None
        assert w1.fetch_fido2_challenge(db, str(uuid.uuid4())) is None
    finally:
        db.close()


def test_legacy_empty_redirect_uri_sentinel_still_reads_as_valid():
    """`store_oauth_state(db, state)` with no URI writes the "1" sentinel, and the
    Entra callback distinguishes "" (valid, no URI — fall back to the configured
    one) from None (invalid). Collapsing those two would break that fallback."""
    _reset()
    state = str(uuid.uuid4())
    db = _db()
    try:
        w1.store_oauth_state(db, state)
        assert w2.verify_and_consume_oauth_state(db, state) == ""
    finally:
        db.close()


# ── Expiry ────────────────────────────────────────────────────────────────────

def test_expired_state_is_refused():
    """TTL has to be enforced on the reading worker's clock against a stored wall
    time. The dict used `time.monotonic()`, whose zero point is per-process and
    means nothing to a reader that did not start the same process."""
    _reset()
    state = str(uuid.uuid4())
    db = _db()
    try:
        w1.store_oauth_state(db, state, "https://example.test/cb")
        # Age the row past its TTL rather than sleeping for five minutes.
        row = db.query(EphemeralState).filter(
            EphemeralState.key == f"{w1._OAUTH_STATE_PREFIX}{state}").one()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()

        assert w2.verify_and_consume_oauth_state(db, state) is None
    finally:
        db.close()


def test_an_expired_row_is_still_consumed():
    """An expired read must not leave the row behind: expiry is a rejection, not a
    retry, and a row that survives its own refusal is a slow leak."""
    _reset()
    state = str(uuid.uuid4())
    db = _db()
    try:
        w1.store_oauth_state(db, state, "https://example.test/cb")
        row = db.query(EphemeralState).filter(
            EphemeralState.key == f"{w1._OAUTH_STATE_PREFIX}{state}").one()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()

        w2.verify_and_consume_oauth_state(db, state)
        assert _rows() == 0
    finally:
        db.close()


def test_abandoned_ceremonies_are_swept():
    """The leak the dict had, which no test would have caught: it evicted only on
    read, so a closed SSO tab or a cancelled touch prompt stayed resident for the
    life of the worker. Sweeping on the write path means the next login tidies up.
    """
    _reset()
    db = _db()
    try:
        for _ in range(5):
            w1.store_oauth_state(db, str(uuid.uuid4()), "https://example.test/cb")
        assert _rows() == 5

        db.query(EphemeralState).update(
            {EphemeralState.expires_at: datetime.utcnow() - timedelta(seconds=1)},
            synchronize_session=False)
        db.commit()

        # One later ceremony on the OTHER worker clears them.
        w2.store_oauth_state(db, str(uuid.uuid4()), "https://example.test/cb")
        assert _rows() == 1
    finally:
        db.close()


def test_the_sweep_spares_live_rows():
    """A sweep that took the live rows with it would break the ceremony it runs
    inside — every store calls it, including the one that just wrote."""
    _reset()
    db = _db()
    try:
        keep = str(uuid.uuid4())
        w1.store_oauth_state(db, keep, "https://example.test/cb")
        for _ in range(3):
            w1.store_oauth_state(db, str(uuid.uuid4()), "https://example.test/cb")
        assert _rows() == 4
        assert w2.verify_and_consume_oauth_state(db, keep) == "https://example.test/cb"
    finally:
        db.close()


# ── Shape ─────────────────────────────────────────────────────────────────────

def test_the_service_holds_no_process_local_store():
    """The regression this file exists for would come back as a dict reintroduced
    'just for a cache'. There is nothing to cache — every value is read once, by a
    worker that is not the one that wrote it.

    Asserted against the module namespace rather than its source text, so that the
    comments explaining the original bug do not themselves trip the check.
    """
    mutable = {name: v for name, v in vars(w1).items()
               if not name.startswith("__") and isinstance(v, (dict, list, set))}
    assert not mutable, (
        f"module-level mutable state is back in fido2_service: {sorted(mutable)} — "
        "under gunicorn -w 2 each worker gets its own copy")
    assert "threading" not in vars(w1), (
        "a lock here would again be guarding threads against a process-level hazard")


def test_ttls_are_unchanged():
    """Behaviour-preserving move: the ceremony windows are the ones users already
    have, not new ones inherited from the storage change."""
    assert w1._CHALLENGE_TTL == 120
    assert w1._OAUTH_STATE_TTL == 300


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
