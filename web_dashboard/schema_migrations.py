"""Schema changes that run once, recorded in ``schema_markers``.

``init_db`` used to run its whole list of ``ALTER TABLE`` / ``CREATE INDEX`` statements on
every boot and swallow "already exists". On PostgreSQL that is not free even when every
statement is a no-op: each one takes ACCESS EXCLUSIVE on its table and holds it until the
boot transaction commits, which is what made init_db a deadlock partner for the
jobs_worker (2026-09-25, see the comment in ``init_db``). And a statement that fails for
any other reason is swallowed just the same.

So schema changes now go through the same ledger the one-time data fixes already use
(``database.SchemaMarker``), under keys prefixed ``ddl:``:

**The legacy list** (``schema_legacy.STATEMENTS``) is frozen. It still runs on every boot
until one boot leaves the database with everything it adds -- every model column, every
index it creates, the one column it retypes -- and no statement skipped for lock
contention or a deadlock. That boot records :data:`LEGACY_KEY` and later boots skip the
list. Until then, :func:`legacy_gaps` names what is missing in a warning on each boot,
instead of the old silence.

**New schema changes** are :class:`Migration` entries in :data:`MIGRATIONS`, applied in
name order, each once, each recorded by its own key. Their steps are idempotent by
construction (:func:`add_column` and :func:`create_index` look before they act), because
``create_all`` runs first: a table it has just built already carries the column a later
migration adds. A step that fails rolls its migration back, stops the rest (a later one
may depend on it) and logs which; it is retried next boot.

Nothing here opens its own transaction or takes a lock: ``init_db`` calls it inside the
advisory-locked, ``lock_timeout``-bounded transaction it already holds on PostgreSQL.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)

#: Recorded once the legacy statement list has fully applied on this database.
LEGACY_KEY = "ddl:legacy_statements_v1"

_INDEX_RE = re.compile(r"^CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?(\w+) ON (\w+)", re.I)
_RETYPE_RE = re.compile(r"^ALTER TABLE (\w+) ALTER COLUMN (\w+) TYPE (\w+)", re.I)


# ── the ledger ────────────────────────────────────────────────────────────────

def applied(conn) -> set:
    """Every ``ddl:`` key already recorded on this database."""
    rows = conn.execute(text("SELECT key FROM schema_markers WHERE key LIKE 'ddl:%'"))
    return {row[0] for row in rows}


def _record(conn, key: str, detail: str) -> None:
    """Record ``key``, or leave it as another process already recorded it.

    SQLite has no advisory lock: the image's ``gunicorn -w 2`` boots two workers on the
    same fresh file, both find the ledger empty, and both get here. A UNIQUE failure in
    the slower one would abort its startup and take the container down with it.
    """
    from .utils.clock import utcnow
    conn.execute(text("INSERT INTO schema_markers (key, applied_at, detail) "
                      "VALUES (:k, :t, :d) ON CONFLICT (key) DO NOTHING"),
                 {"k": key, "t": utcnow(), "d": detail})


# ── the frozen legacy list ────────────────────────────────────────────────────

def legacy_gaps(conn, metadata, statements) -> list:
    """What the legacy list should have left behind and has not, as ``table.column``
    style strings. Empty means it has fully applied.

    Checked against its effects rather than its errors, because an error is the
    *expected* outcome of nearly every statement after the first boot:

    * every column on every model exists (covers ``ADD COLUMN`` and the one
      ``RENAME COLUMN``, whose new name is the model's),
    * every index the list creates exists, on a table the models still have,
    * on PostgreSQL, the column the list retypes has that type (SQLite cannot retype,
      and never could, so it is not asked).
    """
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    gaps = []
    columns = {}
    for table in metadata.sorted_tables:
        if table.name not in tables:
            gaps.append(f"{table.name} (whole table)")
            continue
        columns[table.name] = {c["name"]: c for c in insp.get_columns(table.name)}
        gaps += [f"{table.name}.{col.name}" for col in table.columns
                 if col.name not in columns[table.name]]
    indexes = {}
    for stmt in statements:
        m = _INDEX_RE.match(stmt.strip())
        if m and m.group(2) in columns:
            name, table = m.group(1), m.group(2)
            if table not in indexes:
                indexes[table] = ({i["name"] for i in insp.get_indexes(table)}
                                  | {u["name"] for u in insp.get_unique_constraints(table)})
            if name not in indexes[table]:
                gaps.append(f"index {name} on {table}")
    if conn.dialect.name == "postgresql":
        for stmt in statements:
            m = _RETYPE_RE.match(stmt.strip())
            if m and m.group(2) in columns.get(m.group(1), {}):
                got = str(columns[m.group(1)][m.group(2)]["type"]).upper()
                if not got.startswith(m.group(3).upper()):
                    gaps.append(f"{m.group(1)}.{m.group(2)} is {got}, not {m.group(3)}")
    return gaps


def finish_legacy(conn, metadata, statements, not_applied) -> bool:
    """After the legacy list has run this boot: record it if it has fully applied.

    ``not_applied`` is init_db's list of statements skipped for lock contention or a
    deadlock. Either means a statement never ran, and not every effect is checkable
    (a retype on SQLite, an index on a dropped table), so any skip defers the record.
    """
    gaps = legacy_gaps(conn, metadata, statements)
    if gaps:
        logger.warning(
            "init_db: the legacy migration list has not fully applied, so it will run "
            "again next boot. Missing: %s", "; ".join(gaps))
        return False
    if not_applied:
        return False                    # init_db has already logged what was skipped
    _record(conn, LEGACY_KEY, f"all {len(statements)} legacy statements applied")
    logger.info("init_db: the legacy migration list has fully applied; it is recorded "
                "and will not run again")
    return True


# ── numbered migrations ───────────────────────────────────────────────────────

Step = Callable[[object], None]


@dataclass(frozen=True)
class Migration:
    """One schema change. ``name`` sorts: ``0001_short_reason``, ``0002_...``."""
    name: str
    steps: tuple
    detail: str = ""

    @property
    def key(self) -> str:
        return f"ddl:{self.name}"


def add_column(table: str, column: str, ddl: str) -> Step:
    """``ALTER TABLE table ADD COLUMN column ddl``, skipped when the column is there.

    ``ddl`` is the type and any default, written for PostgreSQL and SQLite both: a
    boolean default is ``false``, never ``0`` (see ``tests/test_rbac_migration.py``).
    """
    def step(conn):
        if column not in {c["name"] for c in inspect(conn).get_columns(table)}:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    step.describe = f"add {table}.{column}"
    return step


def create_index(name: str, table: str, columns, unique: bool = False) -> Step:
    """``CREATE [UNIQUE] INDEX name ON table (columns)``, skipped when it exists."""
    cols = ", ".join([columns] if isinstance(columns, str) else columns)

    def step(conn):
        if name not in {i["name"] for i in inspect(conn).get_indexes(table)}:
            conn.execute(text(f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} "
                              f"ON {table} ({cols})"))
    step.describe = f"index {name} on {table}"
    return step


def sql(statement: str, params: dict | None = None) -> Step:
    """A raw statement, for what the two helpers cannot express. It must be safe to run
    on a database ``create_all`` has just built, because on a new install it will be."""
    def step(conn):
        conn.execute(text(statement), params or {})
    step.describe = statement.split("\n", 1)[0][:80]
    return step


#: Every schema change since the legacy list was frozen, oldest first. Append; never
#: reorder, rename or edit one that has shipped -- databases have recorded it.
MIGRATIONS: tuple = ()


def apply_numbered(conn, migrations=None) -> list:
    """Apply every unrecorded migration in name order. Returns the names applied.

    Each runs inside a savepoint, so a failure leaves the boot transaction usable (on
    PostgreSQL a failed statement aborts it otherwise). The first failure stops the
    run: a later migration may depend on it, and it is retried next boot.
    """
    migrations = MIGRATIONS if migrations is None else migrations
    done = applied(conn)
    ran = []
    for mig in sorted(migrations, key=lambda m: m.name):
        if mig.key in done:
            continue
        savepoint = conn.begin_nested()
        try:
            for step in mig.steps:
                step(conn)
            _record(conn, mig.key, mig.detail or ", ".join(
                getattr(s, "describe", "step") for s in mig.steps))
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 - reported, retried next boot
            savepoint.rollback()
            code = getattr(getattr(exc, "orig", None), "pgcode", None)
            logger.warning(
                "init_db: schema migration %s did not apply (%s%s); it and every "
                "migration after it will be retried next boot", mig.name,
                type(exc).__name__, f" {code}" if code else "")
            break
        ran.append(mig.name)
    return ran
