"""Schema changes run once and are recorded (web_dashboard/schema_migrations.py).

init_db's legacy list of ALTER TABLE / CREATE INDEX statements used to run on every boot.
It now runs until one boot leaves everything it adds in place, records
``ddl:legacy_statements_v1`` in ``schema_markers``, and is not read again. New schema
changes are numbered ``Migration`` entries, each applied once. These pin, on SQLite:

  * a new install records the list on its first boot, and a later boot issues no DDL,
  * an install from before the ledger (no record) runs the list, gets its columns back
    and records it,
  * a database still missing something the list should have made is NOT recorded, the
    warning names it, and the list runs again next boot,
  * a numbered migration applies once, is recorded, skips what is already there, and a
    failing one rolls back and stops the ones after it,
  * the legacy list is frozen, and the numbered ones are well-formed.

tests/test_postgres_migrations.py covers the same ledger on PostgreSQL, including the
lock-contention case that SQLite cannot produce.

Run: python tests/test_schema_migrations.py   (or under pytest)
"""
import ast
import logging
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="schema-mig-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-schema-migrations-" + "0" * 32)

from sqlalchemy import event, inspect, text  # noqa: E402

from web_dashboard import database as D  # noqa: E402
from web_dashboard import schema_migrations as M  # noqa: E402

#: The legacy list's length when it was frozen. A schema change is a numbered Migration.
_FROZEN_AT = 222


def _legacy_statements():
    tree = ast.parse(open(os.path.join(_ROOT, "web_dashboard", "database.py"),
                          encoding="utf-8").read())
    init_db = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == "init_db")
    lst = next(n for n in ast.walk(init_db) if isinstance(n, ast.Assign)
               and getattr(n.targets[0], "id", None) == "_migrations")
    return [e.value for e in lst.value.elts]


def _fresh():
    D.Base.metadata.drop_all(bind=D.engine)
    D.init_db()


def _ledger():
    with D.engine.connect() as conn:
        return M.applied(conn)


def _forget_legacy():
    """What an install from before the ledger looks like: no record of the list."""
    with D.engine.begin() as conn:
        conn.execute(text("DELETE FROM schema_markers WHERE key = :k"), {"k": M.LEGACY_KEY})


def _ddl_during(fn):
    """Every ALTER TABLE / CREATE INDEX the engine issues while ``fn`` runs."""
    seen = []

    def listen(conn, cursor, statement, *args):
        if re.match(r"\s*(ALTER TABLE|CREATE (UNIQUE )?INDEX)", statement, re.I):
            seen.append(statement)
    event.listen(D.engine, "before_cursor_execute", listen)
    try:
        fn()
    finally:
        event.remove(D.engine, "before_cursor_execute", listen)
    return seen


class _Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def __enter__(self):
        logging.getLogger(M.__name__).addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger(M.__name__).removeHandler(self)


def _legacy_added_column():
    """A column the legacy list adds that SQLite can drop (no index, key or constraint)."""
    for stmt in _legacy_statements():
        m = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+) (?:FLOAT|TEXT|TIMESTAMP)$", stmt)
        if m:
            return m.group(1), m.group(2)
    raise AssertionError("no plain droppable ADD COLUMN in the legacy list")


def _column_the_list_does_not_add():
    """A model column no legacy statement adds, droppable on SQLite."""
    added = {(m.group(1), m.group(2)) for s in _legacy_statements()
             if (m := re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", s))}
    indexed = {(m.group(2), c.strip()) for s in _legacy_statements()
               if (m := re.match(r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?(\w+) ON "
                                 r"(\w+)\s*\(([^)]*)\)", s))
               for c in m.group(3).split(",")}
    for table in D.Base.metadata.sorted_tables:
        for col in table.columns:
            if (col.nullable and not col.primary_key and not col.index and not col.unique
                    and not col.foreign_keys and (table.name, col.name) not in added
                    and (table.name, col.name) not in indexed
                    and not any(col.name in [c.name for c in ix.columns]
                                for ix in table.indexes)):
                return table.name, col.name
    raise AssertionError("no candidate column")


def _drop(table, column):
    with D.engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))


def _has(table, column):
    return column in {c["name"] for c in inspect(D.engine).get_columns(table)}


# ── the legacy list ───────────────────────────────────────────────────────────

def test_a_new_install_records_the_list_and_a_later_boot_issues_no_ddl():
    _fresh()
    assert M.LEGACY_KEY in _ledger(), "a fresh database did not record the legacy list"
    issued = _ddl_during(D.init_db)
    assert issued == [], (
        f"a boot after the list was recorded still issued {len(issued)} DDL statement(s), "
        f"e.g. {issued[:2]} -- on PostgreSQL each takes ACCESS EXCLUSIVE until commit")


def test_an_install_from_before_the_ledger_runs_the_list_and_records_it():
    _fresh()
    table, column = _legacy_added_column()
    _drop(table, column)
    _forget_legacy()
    assert len(_ddl_during(D.init_db)) == len(_legacy_statements())
    assert _has(table, column), f"the legacy list did not put {table}.{column} back"
    assert M.LEGACY_KEY in _ledger()


def test_a_second_worker_recording_the_list_does_not_fail():
    # gunicorn -w 2 on a fresh SQLite file: both workers find the ledger empty, the
    # faster records the list, and the slower must not die on the UNIQUE key.
    _fresh()
    with D.engine.begin() as conn:
        M._record(conn, M.LEGACY_KEY, "the second worker")
    with D.engine.connect() as conn:
        rows = conn.execute(text("SELECT detail FROM schema_markers WHERE key = :k"),
                            {"k": M.LEGACY_KEY}).all()
    assert len(rows) == 1 and rows[0][0] != "the second worker", rows


def test_a_gap_the_list_cannot_fill_is_named_and_not_recorded():
    _fresh()
    table, column = _column_the_list_does_not_add()
    _drop(table, column)
    _forget_legacy()
    with _Warnings() as w:
        D.init_db()
    assert M.LEGACY_KEY not in _ledger(), (
        f"recorded the list as applied with {table}.{column} missing -- it would never "
        f"run again, and nothing would say the column is absent")
    assert any(f"{table}.{column}" in m for m in w.messages), w.messages
    # Still unrecorded, so the next boot runs it again rather than going quiet.
    assert len(_ddl_during(D.init_db)) == len(_legacy_statements())


def test_legacy_gaps_reads_effects_not_errors():
    _fresh()
    with D.engine.connect() as conn:
        assert M.legacy_gaps(conn, D.Base.metadata, _legacy_statements()) == []
        # An index the list creates, missing, is a gap; one on a table no model has is not.
        fake = ["CREATE INDEX ix_not_there ON jobs(status)",
                "CREATE INDEX ix_on_a_dropped_table ON no_such_table(x)"]
        assert M.legacy_gaps(conn, D.Base.metadata, fake) == ["index ix_not_there on jobs"]


# ── numbered migrations ───────────────────────────────────────────────────────

def _apply(migrations):
    with D.engine.connect() as conn:
        ran = M.apply_numbered(conn, migrations)
        conn.commit()
    return ran


def test_a_numbered_migration_applies_once_and_is_recorded():
    _fresh()
    mig = M.Migration("0001_test_add", (M.add_column("jobs", "zz_probe", "TEXT"),
                                        M.create_index("ix_jobs_zz_probe", "jobs", "zz_probe")))
    assert _apply([mig]) == ["0001_test_add"]
    assert _has("jobs", "zz_probe") and mig.key in _ledger()
    assert "ix_jobs_zz_probe" in {i["name"] for i in inspect(D.engine).get_indexes("jobs")}
    assert _apply([mig]) == [], "a recorded migration ran again"


def test_the_helpers_skip_what_create_all_already_built():
    """On a new install create_all makes the latest columns first; the step must not fail."""
    _fresh()
    mig = M.Migration("0001_already_there", (M.add_column("jobs", "status", "TEXT"),))
    assert _apply([mig]) == ["0001_already_there"]


def test_a_failing_migration_rolls_back_and_stops_the_rest():
    _fresh()
    first = M.Migration("0001_half", (M.add_column("jobs", "zz_half", "TEXT"),
                                      M.sql("ALTER TABLE no_such_table ADD COLUMN x TEXT")))
    second = M.Migration("0002_after", (M.add_column("jobs", "zz_after", "TEXT"),))
    with _Warnings() as w:
        assert _apply([second, first]) == []          # name order, not list order
    assert not _has("jobs", "zz_half"), "the failed migration's first step was kept"
    assert not _has("jobs", "zz_after"), "a migration after a failed one still ran"
    assert not ({first.key, second.key} & _ledger())
    assert any("0001_half" in m for m in w.messages), w.messages


# ── the rules ─────────────────────────────────────────────────────────────────

def test_the_legacy_list_is_frozen():
    n = len(_legacy_statements())
    assert n == _FROZEN_AT, (
        f"init_db's legacy list has {n} statements, not {_FROZEN_AT}. A database that "
        f"has recorded {M.LEGACY_KEY} never reads it again, so a statement added there "
        f"runs on new installs only. Add a Migration to schema_migrations.MIGRATIONS "
        f"instead.")


def test_numbered_migrations_are_well_formed():
    names = [m.name for m in M.MIGRATIONS]
    assert len(names) == len(set(names)), "two migrations share a name"
    for name in names:
        assert re.fullmatch(r"\d{4}_[a-z0-9_]+", name), f"bad migration name {name!r}"
    assert names == sorted(names), "MIGRATIONS is not in name order"
    assert all(m.steps for m in M.MIGRATIONS), "a migration with no steps"
    assert M.LEGACY_KEY not in {m.key for m in M.MIGRATIONS}


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
