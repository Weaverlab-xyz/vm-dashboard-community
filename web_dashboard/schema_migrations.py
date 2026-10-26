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


# ── 0003: scoped Config-Management routes ────────────────────────────────────

_ROUTES = "config_mgmt_routes"


def _route_constraint_names(conn) -> set:
    insp = inspect(conn)
    return ({u["name"] for u in insp.get_unique_constraints(_ROUTES)}
            | {i["name"] for i in insp.get_indexes(_ROUTES)})


def _drop_cidr_only_unique(conn) -> None:
    """Drop ``uq_config_mgmt_route_cidr``: one CIDR may now be routed once PER SCOPE.

    PostgreSQL drops a constraint in place. SQLite cannot, so the table is rebuilt from
    the model (which carries the new constraint) and the rows copied across — the
    documented SQLite procedure, and cheap for a table of a few dozen rows.
    """
    if "uq_config_mgmt_route_cidr" not in _route_constraint_names(conn):
        return                      # a fresh create_all table, or already done
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"ALTER TABLE {_ROUTES} "
                          f"DROP CONSTRAINT IF EXISTS uq_config_mgmt_route_cidr"))
        return
    # Index names are global in SQLite, so the old table's go first or the new table's
    # same-named ones cannot be created (done inside the rebuild).
    _rebuild_routes_from_model(conn, "pre0003")


def _rebuild_routes_from_model(conn, suffix: str) -> None:
    """SQLite only: recreate the routes table from the CURRENT model and copy the rows.

    The model may be newer than the table — a later migration's column already on it —
    so only the columns both have are copied; the rest take the model's defaults, which
    is exactly what that later migration's add_column would have given them."""
    from .database import ConfigMgmtRoute
    table = ConfigMgmtRoute.__table__
    for (name,) in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t "
            "AND sql IS NOT NULL"), {"t": _ROUTES}).fetchall():
        conn.execute(text(f'DROP INDEX "{name}"'))
    old = f"{_ROUTES}_{suffix}"
    conn.execute(text(f"ALTER TABLE {_ROUTES} RENAME TO {old}"))
    have = {c["name"] for c in inspect(conn).get_columns(old)}
    table.create(conn)
    cols = ", ".join(c.name for c in table.columns if c.name in have)
    conn.execute(text(f"INSERT INTO {_ROUTES} ({cols}) SELECT {cols} FROM {old}"))
    conn.execute(text(f"DROP TABLE {old}"))


def _route_scope_unique(conn) -> None:
    if "uq_config_mgmt_route_scope" not in _route_constraint_names(conn):
        conn.execute(text(
            f"CREATE UNIQUE INDEX uq_config_mgmt_route_scope ON {_ROUTES} "
            f"(cidr, scope, scope_region, scope_connection_id, scope_workstream)"))


def _split_cloud_routes(conn) -> None:
    """Keep every lookup's answer across the upgrade.

    Before scopes, a route with ``include_cloud_vms`` governed hypervisor guests AND the
    VMs of all three clouds. It stays as the on-premises row, keeping its id, and gains
    one row per cloud with the same agent, range, label and state. An operator who meant
    only one cloud deletes the other rows; nothing moves until they do.
    """
    import uuid
    rows = conn.execute(text(
        f"SELECT id, agent_id, cidr, label, is_active, created_at, created_by "
        f"FROM {_ROUTES} WHERE include_cloud_vms = :t AND scope = 'onprem' "
        f"AND scope_region = '' AND scope_connection_id = '' AND scope_workstream = ''"),
        {"t": True}).fetchall()
    for row in rows:
        for cloud in ("aws", "azure", "gcp"):
            conn.execute(text(
                f"INSERT INTO {_ROUTES} (id, agent_id, cidr, label, is_active, "
                f"include_cloud_vms, scope, scope_region, scope_connection_id, "
                f"scope_workstream, created_at, created_by, updated_at) VALUES (:id, :agent, "
                f":cidr, :label, :active, :f, :scope, '', '', '', :created, :by, :created)"),
                {"id": str(uuid.uuid4()), "agent": row.agent_id, "cidr": row.cidr,
                 "label": row.label, "active": row.is_active, "f": False, "scope": cloud,
                 "created": row.created_at, "by": row.created_by})
        conn.execute(text(f"UPDATE {_ROUTES} SET include_cloud_vms = :f WHERE id = :id"),
                     {"f": False, "id": row.id})


_split_cloud_routes.describe = "split cloud-flagged routes into one row per scope"


# ── 0004: routes narrowed to one network ─────────────────────────────────────

def _drop_route_unique(name: str):
    """A step dropping the routes table's unique rule ``name``, whichever form it took.

    It is a table CONSTRAINT where ``create_all`` built the table, and a unique INDEX where
    0003 created it on an existing table. PostgreSQL drops either in place; SQLite drops
    an index in place but can only lose a table constraint by a rebuild from the model
    (which already carries the replacement)."""
    def step(conn):
        insp = inspect(conn)
        constraints = {u["name"] for u in insp.get_unique_constraints(_ROUTES)}
        indexes = {i["name"] for i in insp.get_indexes(_ROUTES)}
        if conn.dialect.name == "postgresql":
            conn.execute(text(f"ALTER TABLE {_ROUTES} DROP CONSTRAINT IF EXISTS {name}"))
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
            return
        if name in indexes and name not in constraints:
            conn.execute(text(f'DROP INDEX "{name}"'))
        elif name in constraints:
            _rebuild_routes_from_model(conn, "pre0004")
    step.describe = f"drop {name}"
    return step


def _ensure_route_unique(name: str, cols: tuple):
    def step(conn):
        if name not in _route_constraint_names(conn):
            conn.execute(text(f"CREATE UNIQUE INDEX {name} ON {_ROUTES} ({', '.join(cols)})"))
    step.describe = f"unique {name}"
    return step
_drop_cidr_only_unique.describe = "drop uq_config_mgmt_route_cidr"
_route_scope_unique.describe = ("unique (cidr, scope, scope_region, scope_connection_id, "
                               "scope_workstream)")


#: Every schema change since the legacy list was frozen, oldest first. Append; never
#: reorder, rename or edit one that has shipped -- databases have recorded it.
MIGRATIONS: tuple = (
    # Config-Management routes may also govern cloud VMs (config_mgmt_route_service).
    # Bare BOOLEAN, no DEFAULT: NULL reads as off, so existing routes are unchanged.
    Migration("0001_config_route_cloud_vms",
              (add_column("config_mgmt_routes", "include_cloud_vms", "BOOLEAN"),)),
    # How many Config-Management runs an agent says it runs at once (agent 2.9+).
    Migration("0002_agent_reported_ansible_slots",
              (add_column("remote_agents", "reported_ansible_slots", "INTEGER"),)),
    # A route has a scope: on-premises guests (optionally one connection's) or one cloud's
    # VMs (optionally one region's and/or one workstream's). One CIDR may be routed once per scope, so the
    # CIDR-only unique constraint goes; flagged routes split so no lookup changes answer.
    Migration("0003_config_route_scopes",
              (add_column(_ROUTES, "scope", "VARCHAR(16) NOT NULL DEFAULT 'onprem'"),
               add_column(_ROUTES, "scope_region", "VARCHAR(64) NOT NULL DEFAULT ''"),
               add_column(_ROUTES, "scope_connection_id", "VARCHAR(36) NOT NULL DEFAULT ''"),
               add_column(_ROUTES, "scope_workstream", "VARCHAR(100) NOT NULL DEFAULT ''"),
               _drop_cidr_only_unique,
               _route_scope_unique,
               _split_cloud_routes)),
    # A cloud route may also name one network (VPC, VNet, GCP network): the exact answer
    # region and workstream approximate. The CIDR-and-scope unique rule widens to it; no
    # row changes, since no existing route names a network. Deployed agents record the
    # network their host runs in.
    Migration("0004_config_route_networks",
              (add_column(_ROUTES, "scope_network", "VARCHAR(200) NOT NULL DEFAULT ''"),
               _drop_route_unique("uq_config_mgmt_route_scope"),
               _ensure_route_unique("uq_config_mgmt_route_scope_net",
                                    ("cidr", "scope", "scope_region", "scope_connection_id",
                                     "scope_workstream", "scope_network")),
               add_column("agent_hosts", "network", "VARCHAR(200)"))),
    # Remote-agent lifecycle: what the agent reports about its upgrade opt-in and its
    # updater (2.10+), the dashboard's pause, and the rollout ring. All NULL-able with no
    # DEFAULT: NULL reads as "not reported" / not paused / the default ring. The release
    # and rollout tables are new, so create_all makes them.
    Migration("0005_agent_lifecycle",
              (add_column("remote_agents", "control_upgrades", "BOOLEAN"),
               add_column("remote_agents", "control_updater", "BOOLEAN"),
               add_column("remote_agents", "paused", "BOOLEAN"),
               add_column("remote_agents", "upgrade_ring", "VARCHAR(16)"))),
    # Agent assurance: what the last attestation was, the key it replaced (to catch a second
    # copy still using it), what needs an operator's attention, and the agent's re-attest
    # interval. All NULL-able: NULL is "nothing recorded" and "nothing to report".
    Migration("0006_agent_assurance",
              (add_column("remote_agents", "last_attest_at", "TIMESTAMP"),
               add_column("remote_agents", "last_attest_ip", "VARCHAR(45)"),
               add_column("remote_agents", "previous_public_key", "VARCHAR(64)"),
               add_column("remote_agents", "previous_key_until", "TIMESTAMP"),
               add_column("remote_agents", "attention", "TEXT"),
               add_column("remote_agents", "reported_reattest_hours", "INTEGER"))),
    # A cloud route may fall back to the remote worker while its agent is offline. Bare
    # BOOLEAN, no DEFAULT: NULL reads as off, so every existing route still refuses.
    Migration("0007_config_route_fallback",
              (add_column(_ROUTES, "fallback_to_worker", "BOOLEAN"),)),
    # Where a cloud database lives, so a cloud route can govern it by subnet range. All
    # NULL-able: NULL is "not recorded", and such a database stays on the remote worker.
    Migration("0008_clouddb_route_ranges",
              (add_column("cloud_databases", "route_cidrs", "TEXT"),
               add_column("cloud_databases", "route_network", "VARCHAR(200)"),
               add_column("cloud_databases", "deploy_workgroup", "VARCHAR(100)"))),
    # Kubernetes work on dashboard-owned agents: a cluster may name one as its executor,
    # and a deployed host opts in. NULL is the remote worker, and off.
    Migration("0009_k8s_on_owned_agents",
              (add_column("k8s_clusters", "executor_agent_id", "VARCHAR(36)"),
               add_column("agent_hosts", "k8s_enabled", "BOOLEAN"))),
    # The POV standard recipe a catalog image was built from. NULL for every image that
    # predates it and every image built any other way.
    Migration("0010_registered_image_recipe",
              (add_column("registered_images", "recipe", "VARCHAR(40)"),)),
)


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
