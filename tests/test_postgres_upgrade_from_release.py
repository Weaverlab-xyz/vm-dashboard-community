"""An install of the PREVIOUS RELEASE upgrades to the same schema a new install gets.

``test_postgres_migrations.py`` simulates an upgrade by dropping the columns the migration
list adds and re-running ``init_db``. That proves each statement is valid on PostgreSQL;
it does not prove that a database as it REALLY looked one release ago ends up identical
to a fresh one. Drift between the two is the upgrade bug nobody sees until a customer
upgrades: a model column with no migration (``create_all`` never alters an existing
table), a migration that no-ops on a changed type, an index only one path creates.

This test loads ``tests/fixtures/postgres_schema_<tag>.sql`` -- the schema, and the
``schema_markers`` ledger rows, of a database ``init_db`` built at that release tag --
into the CI PostgreSQL, runs today's ``init_db`` over it, and compares a normalised
FINGERPRINT of the result with the one from ``init_db`` on an empty database. The
fingerprint (tables; per column name, type, nullability; primary key, unique
constraints, indexes, foreign keys) is read through SQLAlchemy's inspector, not diffed
as SQL, so dump formatting, ordering and constraint-name noise cannot fail it.

Skips cleanly with no PostgreSQL configured (CI's `postgres` job runs it)::

    DATABASE_URL=postgresql://user:pw@host:5432/db python tests/test_postgres_upgrade_from_release.py

REFRESH THE FIXTURE AFTER EACH RELEASE TAG (so the test always upgrades from the last
shipped schema, and the fixture stays a record of a real one):

1. ``git worktree add --detach /tmp/rel <new-tag>``
2. Start a throwaway server: ``docker run -d --name pgrel -e POSTGRES_USER=test
   -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb -p 55432:5432 postgres:16-alpine``
3. In the worktree: ``DATABASE_URL=postgresql://test:test@localhost:55432/testdb
   JWT_SECRET_KEY=x python -c "from web_dashboard.database import init_db; init_db()"``
4. Dump schema, then the ledger as INSERTs (the loader runs it through one cursor, so
   no COPY), and drop the per-run noise (``\\restrict`` / ``\\unrestrict`` lines, which
   carry a random token, and the "Dumped from/by" comments)::

       docker exec pgrel pg_dump -U test -d testdb --schema-only --no-owner --no-privileges
       docker exec pgrel pg_dump -U test -d testdb --data-only --inserts --no-owner -t schema_markers

   concatenated, into ``tests/fixtures/postgres_schema_<new-tag>.sql``.
5. Update ``FIXTURE`` below, delete the old fixture, run this file against a local
   ``postgres:16-alpine``, and remove the throwaway worktree and container.

A failure names the table and column (or index, constraint) that differs, and which side
has it. "Only on the fresh side" means a model change shipped without a migration.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-postgres-upgrade-from-release")

FIXTURE = os.path.join(_ROOT, "tests", "fixtures", "postgres_schema_v26.10.44.sql")

_URL = os.environ.get("DATABASE_URL", "")


def _skip(reason):
    try:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {reason}")
        sys.exit(0)


# Gate on the URL BEFORE importing the app: `database.py` binds its engine at import time.
if not _URL.startswith(("postgresql:", "postgres:", "postgresql+")):
    _skip("no PostgreSQL DATABASE_URL configured — this file only runs against a real "
          "PostgreSQL (CI's `postgres` job)")

try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover
    _skip(f"sqlalchemy unavailable: {exc}")

from sqlalchemy import inspect, text  # noqa: E402

from web_dashboard.database import engine, init_db  # noqa: E402


def _reset_schema():
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def _load_fixture():
    """Run the dump through one raw cursor (it is several statements, with its own SETs)."""
    with open(FIXTURE, encoding="utf-8") as fh:
        sql = fh.read()
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql)
        raw.commit()
    finally:
        raw.close()
    # The dump blanks search_path for its session; do not hand that to the next user of
    # a pooled connection.
    engine.dispose()


def fingerprint():
    """A normalised, comparable description of the whole ``public`` schema."""
    insp = inspect(engine)
    out = {}
    for table in sorted(insp.get_table_names()):
        cols = {c["name"]: (str(c["type"]), bool(c["nullable"]))
                for c in insp.get_columns(table)}
        pk = tuple(insp.get_pk_constraint(table).get("constrained_columns") or ())
        uniques = sorted(tuple(u["column_names"]) for u in insp.get_unique_constraints(table))
        # An index by columns and uniqueness, not by name: a unique constraint also shows
        # up as an index, and auto-generated names are not part of the contract.
        indexes = sorted((tuple(i["column_names"]), bool(i.get("unique")))
                         for i in insp.get_indexes(table))
        fks = sorted((tuple(f["constrained_columns"]), f["referred_table"],
                      tuple(f["referred_columns"])) for f in insp.get_foreign_keys(table))
        out[table] = {"columns": cols, "pk": pk, "unique": uniques,
                      "indexes": indexes, "fks": fks}
    return out


def _diff(upgraded, fresh):
    """Human-readable lines for every difference, [] when the schemas match."""
    lines = []
    for t in sorted(set(upgraded) | set(fresh)):
        if t not in upgraded:
            lines.append(f"table {t}: only on the fresh side (no migration creates it)")
            continue
        if t not in fresh:
            lines.append(f"table {t}: only on the upgraded side (the models no longer have it)")
            continue
        u, f = upgraded[t], fresh[t]
        for c in sorted(set(u["columns"]) | set(f["columns"])):
            if c not in u["columns"]:
                lines.append(f"{t}.{c}: only on the fresh side (model column with no migration)")
            elif c not in f["columns"]:
                lines.append(f"{t}.{c}: only on the upgraded side (column the models dropped)")
            elif u["columns"][c] != f["columns"][c]:
                lines.append(f"{t}.{c}: upgraded {u['columns'][c]} != fresh {f['columns'][c]}")
        for key in ("pk", "unique", "indexes", "fks"):
            if u[key] != f[key]:
                lines.append(f"{t}.{key}: upgraded {u[key]} != fresh {f[key]}")
    return lines


# ── The test ─────────────────────────────────────────────────────────────────

def test_fixture_is_a_real_older_schema():
    """Guard the guard: an empty or truncated fixture would make the next test pass."""
    _reset_schema()
    _load_fixture()
    tables = inspect(engine).get_table_names()
    assert len(tables) > 50, f"fixture loaded only {len(tables)} tables"
    assert "schema_markers" in tables


def test_upgrading_from_the_last_release_matches_a_fresh_install():
    _reset_schema()
    _load_fixture()
    init_db()
    upgraded = fingerprint()

    _reset_schema()
    init_db()
    fresh = fingerprint()

    problems = _diff(upgraded, fresh)
    assert not problems, (
        "a database upgraded from the last release differs from a fresh install:\n  "
        + "\n  ".join(problems))


def test_the_comparison_notices_a_missing_column():
    """The comparison must be able to fail: drop a column from the upgraded side."""
    _reset_schema()
    _load_fixture()
    init_db()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users DROP COLUMN is_admin"))
    upgraded = fingerprint()

    _reset_schema()
    init_db()
    problems = _diff(upgraded, fingerprint())
    assert any(p.startswith("users.is_admin: only on the fresh side") for p in problems), problems


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
