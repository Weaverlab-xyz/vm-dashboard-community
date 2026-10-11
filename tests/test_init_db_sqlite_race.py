"""init_db on SQLite survives a second worker creating tables at the same time.

The image runs ``gunicorn -w 2`` against a fresh SQLite file. PostgreSQL serializes the
two workers' init_db with an advisory lock; SQLite has none, so the slower worker's
CREATE TABLE could fail with "table ... already exists", abort its startup and take the
container down (the Images workflow's boot check, intermittently). These pin the retry:

- losing the race is retried, and the retry succeeds;
- any other OperationalError is still raised, untouched;
- it gives up after a bounded number of attempts rather than looping forever.

Run: python tests/test_init_db_sqlite_race.py   (or under pytest)
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="init-db-race-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-init-db-race")

from sqlalchemy.exc import OperationalError  # noqa: E402

from web_dashboard import database as D  # noqa: E402


class _Conn:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _with_create_all(fn):
    """Swap Base.metadata.create_all for ``fn`` and skip the backoff sleeps."""
    import time
    real_create, real_sleep = D.Base.metadata.create_all, time.sleep
    D.Base.metadata.create_all = fn
    time.sleep = lambda _s: None
    return real_create, real_sleep


def _restore(real):
    import time
    D.Base.metadata.create_all, time.sleep = real


def _exists_error():
    return OperationalError("CREATE TABLE users (...)", {},
                           Exception("table users already exists"))


def test_losing_the_race_is_retried():
    calls = []

    def create_all(bind=None):
        calls.append(bind)
        if len(calls) == 1:
            raise _exists_error()

    conn = _Conn()
    real = _with_create_all(create_all)
    try:
        D._create_all_sqlite(conn)
    finally:
        _restore(real)
    assert len(calls) == 2 and conn.rollbacks == 1


def test_any_other_error_is_raised():
    def create_all(bind=None):
        raise OperationalError("CREATE TABLE x", {}, Exception("disk I/O error"))

    real = _with_create_all(create_all)
    try:
        D._create_all_sqlite(_Conn())
    except OperationalError as exc:
        assert "disk I/O error" in str(exc)
    else:
        raise AssertionError("a non-race error was swallowed")
    finally:
        _restore(real)


def test_the_retry_is_bounded():
    calls = []

    def create_all(bind=None):
        calls.append(1)
        raise _exists_error()

    real = _with_create_all(create_all)
    try:
        D._create_all_sqlite(_Conn())
    except OperationalError:
        pass
    else:
        raise AssertionError("an endless race was swallowed")
    finally:
        _restore(real)
    assert len(calls) == D._SQLITE_CREATE_ATTEMPTS


def test_init_db_uses_it_on_sqlite():
    src = open(os.path.join(_ROOT, "web_dashboard", "database.py"), encoding="utf-8").read()
    body = src.split("def init_db():", 1)[1].split("\ndef ", 1)[0]
    assert "_create_all_sqlite(conn)" in body
    D.init_db()     # and a real fresh SQLite file still initialises


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
