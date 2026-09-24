"""The schema actually applies on PostgreSQL, which is what production runs.

Every other file in this suite runs on SQLite, and for almost everything that is the
right trade: it needs no service, it is fast, and the application code is dialect-neutral
by design. The exception is DDL, where the two databases genuinely disagree — and they
disagree SILENTLY, in the one direction that matters.

``init_db``'s migration list runs each statement inside its own savepoint and swallows
the failure, because "column already exists" is the expected outcome on every boot after
the first. That is the right design and it has a sharp edge: a statement that is
*malformed for PostgreSQL* is swallowed exactly like one that was redundant. Nothing
raises, nothing logs, the column simply never appears — and SQLite, which is more
permissive about literals and types, keeps the whole suite green.

That is not hypothetical. This file found one on the first run it ever did:

    ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0

PostgreSQL will not coerce `0` to a boolean default and rejects the statement; SQLite
takes it. The column was therefore never added on any PostgreSQL install that predated
it, and the symptom would have been `UndefinedColumn` on every query against `users`.

**A fresh database does not catch this, which is the subtle part.** ``create_all`` builds
every table from the models with all columns already present, so the ALTER statements are
no-ops and pass whatever they contain. The bug only exists on an UPGRADE. So the test
below deliberately simulates one: create the schema, drop every column the migration list
claims to add, run ``init_db`` again, and require that all of them came back.

Skips cleanly when no PostgreSQL is configured, so it costs nothing locally and in the
SQLite CI job. Point it at one with:

    DATABASE_URL=postgresql://user:pw@host:5432/db python tests/test_postgres_migrations.py

CI runs it in its own job with a `postgres` service — see .github/workflows/tests.yml.
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-postgres-migrations")

_URL = os.environ.get("DATABASE_URL", "")


def _skip(reason):
    try:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {reason}")
        sys.exit(0)


# Gate on the URL BEFORE importing the app: `database.py` binds its engine at import
# time from DATABASE_URL, so there is no way to point it at PostgreSQL afterwards.
if not _URL.startswith(("postgresql:", "postgres:", "postgresql+")):
    _skip("no PostgreSQL DATABASE_URL configured — this file only runs against a real "
          "PostgreSQL (CI's `postgres` job); SQLite cannot exercise these differences")

try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover
    _skip(f"sqlalchemy unavailable: {exc}")

from sqlalchemy import inspect, text  # noqa: E402

from web_dashboard import database as db_mod  # noqa: E402
from web_dashboard.database import Base, Job, engine, init_db  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402


def _reset_schema():
    """A clean database, so a test never depends on what an earlier one left."""
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def _added_columns():
    """``[(table, column, rest_of_statement)]`` parsed from the real migration list.

    Parsed from source rather than imported because ``_migrations`` is a local inside
    ``init_db``, and the parse is the point: the test must see exactly the statements
    that ship, not a copy of them that can drift.
    """
    src = open(os.path.join(_ROOT, "web_dashboard/database.py"), encoding="utf-8").read()
    block = src.split("_migrations = [", 1)[1].split("\n        ]", 1)[0]
    return re.findall(r'"ALTER TABLE (\w+) ADD COLUMN (\w+)([^"]*)"', block)


# ── The one this file exists for ─────────────────────────────────────────────

def test_every_migration_applies_on_an_upgrade():
    """Drop every migration-added column, re-run init_db, require them all back.

    This is an UPGRADE, not a fresh install, and only an upgrade exercises the ALTER
    statements at all — on a fresh database `create_all` has already made every column
    and they are all no-ops.

    A failure here names the exact `table.column` that did not come back, which is the
    information the swallowed savepoint threw away.
    """
    _reset_schema()
    init_db()                      # create_all builds everything from the models
    adds = _added_columns()
    assert len(adds) > 100, f"only parsed {len(adds)} ADD COLUMN statements — parser broke"

    with engine.begin() as conn:
        for table, column, _rest in adds:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))

    init_db()                      # now every ALTER must genuinely run

    insp = inspect(engine)
    missing = []
    for table, column, rest in adds:
        try:
            cols = {c["name"] for c in insp.get_columns(table)}
        except Exception:          # noqa: BLE001 — table not in this schema
            continue
        if column not in cols:
            missing.append(f"{table}.{column} ({rest.strip()})")
    assert not missing, (
        "these migrations did not apply on PostgreSQL — each was swallowed by its "
        "savepoint as if it were a redundant 'column already exists', so the column is "
        "silently absent on every upgraded install:\n  " + "\n  ".join(missing))


# The static twin of the test above — "no BOOLEAN DEFAULT 0 anywhere in the migration
# list" — deliberately does NOT live here. It needs no database, so it belongs where it
# runs on every pull request rather than only in the service-container job: see
# `test_no_migration_anywhere_defaults_a_boolean_to_an_integer` in
# tests/test_rbac_migration.py, whose docstring already named this trap as canonical.
# This file keeps the behavioural check, which is the one that catches a malformed
# statement of a shape nobody thought to write a pattern for.


# ── The model and the database agree ─────────────────────────────────────────

def test_every_model_column_exists_in_the_database():
    """A column on a model that no table has is an UndefinedColumn waiting for the
    first query that selects it — and on SQLite nothing would notice, because
    `create_all` there is equally happy to build whatever the models describe."""
    _reset_schema()
    init_db()
    insp = inspect(engine)
    present = {t: {c["name"] for c in insp.get_columns(t)}
               for t in insp.get_table_names()}
    missing = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in present:
            missing.append(f"{table_name} (whole table)")
            continue
        for col in table.columns:
            if col.name not in present[table_name]:
                missing.append(f"{table_name}.{col.name}")
    assert not missing, "declared on a model but absent from PostgreSQL:\n  " + \
        "\n  ".join(missing)


# ── Dialect-specific SQL the application actually issues ─────────────────────

def test_the_claim_predicate_runs_on_postgres():
    """`job_service.claimable_now` is the gate every job passes through, and it uses
    `IS NOT true` — which renders differently per dialect and is null-safe on both.
    A SQLite-only suite proves nothing about the form PostgreSQL receives."""
    _reset_schema()
    init_db()
    from web_dashboard.database import SessionLocal
    with SessionLocal() as db:
        rows = (db.query(Job.id)
                .filter(Job.status == "pending", *job_service.claimable_now())
                .all())
        assert rows == []          # empty database; the point is that it EXECUTES


def test_a_null_approval_row_passes_the_gate_on_postgres():
    """The property the whole scheduler rests on, against a real PostgreSQL.

    Every job row that predates the change-window columns carries NULL in
    `approval_required`. `NULL IS NOT true` is TRUE in PostgreSQL, so those rows stay
    claimable. Were it the other way round, enabling this feature would wedge the entire
    queue — so it is worth one real query rather than a compiled-SQL inspection.
    """
    _reset_schema()
    init_db()
    from web_dashboard.database import SessionLocal
    with SessionLocal() as db:
        job = job_service.create_job(db, job_type="ansible_local", created_by="t")
        # Explicitly NULL, which is what an upgraded row looks like.
        db.execute(text("UPDATE jobs SET approval_required = NULL WHERE id = :i"),
                   {"i": job.id})
        db.commit()
        got = (db.query(Job.id)
               .filter(Job.status == "pending", *job_service.claimable_now())
               .all())
        assert [r[0] for r in got] == [job.id], (
            "a row with a NULL approval_required was NOT claimable on PostgreSQL — "
            "every pre-existing job would be frozen")


def test_the_advisory_lock_path_runs():
    """`pg_advisory_xact_lock` is PostgreSQL-only and is short-circuited on SQLite, so
    the sweeps' enqueue guards have never been executed against the database they were
    written for. This runs one for real."""
    _reset_schema()
    init_db()
    assert db_mod._is_sqlite is False, "this file must run against PostgreSQL"
    from web_dashboard.database import SessionLocal
    from web_dashboard.services import schedule_sweeper
    with SessionLocal() as db:
        # Takes the advisory lock, finds no work, returns None without writing a row.
        assert schedule_sweeper.enqueue_sweep_if_due(db) is None


def test_indexes_declared_in_the_migrations_exist():
    """A CREATE INDEX is swallowed by the same savepoint as an ADD COLUMN. A missing
    index is not a crash, which is exactly why nothing would ever report it — the claim
    query just gets slower as `jobs` grows."""
    _reset_schema()
    init_db()
    src = open(os.path.join(_ROOT, "web_dashboard/database.py"), encoding="utf-8").read()
    block = src.split("_migrations = [", 1)[1].split("\n        ]", 1)[0]
    declared = re.findall(
        r'"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?(\w+) ON (\w+)', block)
    insp = inspect(engine)
    missing = []
    for name, table in declared:
        try:
            have = {i["name"] for i in insp.get_indexes(table)}
        except Exception:          # noqa: BLE001
            continue
        if name not in have:
            missing.append(f"{name} on {table}")
    assert not missing, ("indexes the migrations declare but PostgreSQL does not have:"
                         "\n  " + "\n  ".join(missing))


def _run():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
