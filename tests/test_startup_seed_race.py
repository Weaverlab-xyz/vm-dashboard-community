"""Two processes booting together: the slower one's startup writes do not fail.

The image runs ``gunicorn -w 2``, so two workers run init_db's data migrations and seeds,
and later the background sweepers, against the same database at the same time. None of
these take init_db's advisory lock (SQLite has none at all), so both can find a marker,
a built-in role or an app_config key absent and both write it. The slower write fails on
the unique key. These pin that it reads as the other process having done the work:

  * a marker-guarded migration returns 0 and logs nothing louder than INFO, rather than
    "skipped" -- or, for the empty-permission one, an ERROR saying users are denied,
  * ``role_service.seed_builtins`` returns 0 when every built-in is now present, so the
    reconcile after it in init_db still runs,
  * ``config_service.set`` / ``set_many`` update the row the other process inserted.

Each race is made deterministic: another connection commits the same row just before
this session's commit.

Run: python tests/test_startup_seed_race.py   (or under pytest)
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _schema import build_schema  # noqa: E402

build_schema("startup-seed-race")

from sqlalchemy import event, text  # noqa: E402

from web_dashboard import database as D  # noqa: E402
from web_dashboard.services import config_service, role_service  # noqa: E402


def _reset():
    with D.engine.begin() as conn:
        for table in ("schema_markers", "access_roles", "app_config"):
            conn.execute(text(f"DELETE FROM {table}"))
    with config_service._cache_lock:
        config_service._cache.clear()


def _before_commit(session_or_factory, write):
    """Run ``write`` on another connection once, just before the next commit."""
    def _hook(_session):
        with D.engine.begin() as conn:
            write(conn)
    event.listen(session_or_factory, "before_commit", _hook, once=True)


def _marker(conn, key):
    conn.execute(text("INSERT INTO schema_markers (key, applied_at, detail) "
                      "VALUES (:k, CURRENT_TIMESTAMP, 'the other worker')"), {"k": key})


class _Loud(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


_MIGRATIONS = (
    (D._backfill_new_permission_scopes, D._BACKFILL_V1_MARKER),
    (D._materialise_empty_permission_maps, D._EMPTY_MEANS_NOTHING_MARKER),
    (D._retire_unenforced_levels, D._RETIRE_LEVELS_MARKER),
    (D._grant_presenter_own_pov, D._PRESENTER_OWN_POV_MARKER),
    (D._narrow_presenter_to_own_povs, D._PRESENTER_OWN_ONLY_MARKER),
)


def test_a_marker_migration_that_loses_the_race_returns_quietly():
    for fn, key in _MIGRATIONS:
        _reset()
        loud = _Loud()
        logging.getLogger().addHandler(loud)
        try:
            with D.SessionLocal() as db:
                _before_commit(db, lambda conn, k=key: _marker(conn, k))
                assert fn(db) == 0, fn.__name__
        finally:
            logging.getLogger().removeHandler(loud)
        assert not loud.records, (fn.__name__, [r.getMessage() for r in loud.records])
        with D.engine.connect() as conn:
            rows = conn.execute(text("SELECT detail FROM schema_markers WHERE key = :k"),
                                {"k": key}).all()
        assert rows == [("the other worker",)], (fn.__name__, rows)


def test_a_marker_migration_that_wins_still_records_itself():
    for fn, key in _MIGRATIONS:
        _reset()
        with D.SessionLocal() as db:
            fn(db)
        with D.engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM schema_markers WHERE key = :k"),
                             {"k": key}).scalar()
        assert n == 1, fn.__name__


def test_seed_builtins_that_loses_the_race_returns_zero():
    _reset()
    with D.SessionLocal() as other:
        role_service.seed_builtins(other)
    real = role_service.get_by_slug
    role_service.get_by_slug = lambda db, slug: None   # it looked before the other committed
    try:
        with D.SessionLocal() as db:
            assert role_service.seed_builtins(db) == 0
    finally:
        role_service.get_by_slug = real
    with D.engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM access_roles")).scalar()
    assert n == len(role_service._BUILTIN_ROLES)


def test_config_set_that_loses_the_insert_updates_the_other_row():
    for write in (lambda: config_service.set("race_key", "mine"),
                  lambda: config_service.set_many({"race_key": "mine"})):
        _reset()
        _before_commit(D.SessionLocal, lambda conn: conn.execute(text(
            "INSERT INTO app_config (key, value, updated_at) "
            "VALUES ('race_key', '', CURRENT_TIMESTAMP)")))
        write()
        assert config_service.get_fresh("race_key") == "mine"
        with D.engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM app_config WHERE key = 'race_key'"))
            assert n.scalar() == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
