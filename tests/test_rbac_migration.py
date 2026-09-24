"""The role columns' migration, exercised against tables that PREDATE it.

`Base.metadata.create_all` only ever proves the fresh-install path: it makes the whole table
from the model, so every new column is present and no `ALTER TABLE` in `init_db`'s
`_migrations` list is even reached. The upgrade path -- an existing install whose `users`
table was created before these columns existed -- is the one that can break, and it breaks
differently on each backend:

  * on SQLite each statement is wrapped in a bare `try/except: pass`, so a failure is
    indistinguishable from "column already present";
  * on PostgreSQL each is wrapped in a per-statement SAVEPOINT, so a statement that is
    INVALID THERE is silently rolled back and the column **never appears** -- no error, no
    log line, and the model attribute exists with nothing behind it. The symptom arrives
    much later as `column ... does not exist` on one page.

`ADD COLUMN x BOOLEAN DEFAULT 0` is the canonical instance of that second trap: SQLite
accepts an integer default on a boolean, PostgreSQL rejects it. So this file checks the
SHAPE of the statements as well as running them.

Runs under pytest, or standalone:
    python tests/test_rbac_migration.py
"""
import os
import re
import sqlite3
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# The columns this change adds, and the tables they land on.
_NEW_COLUMNS = {
    "users": ("role_id", "role_permissions"),
    "oauth_group_mappings": ("role_id",),
}

# The pre-change shape of both tables, written out rather than derived: deriving it from
# today's model is what makes a migration test prove nothing.
_LEGACY_DDL = (
    """CREATE TABLE users (
           id VARCHAR(36) PRIMARY KEY,
           username VARCHAR(100) UNIQUE NOT NULL,
           hashed_password VARCHAR(255),
           full_name VARCHAR(200),
           email VARCHAR(200),
           workgroups TEXT,
           is_active BOOLEAN,
           created_at DATETIME,
           auth_provider VARCHAR(20),
           oauth_subject VARCHAR(255),
           mfa_required BOOLEAN,
           is_admin BOOLEAN,
           permissions TEXT,
           session_permissions TEXT,
           jit_permissions TEXT,
           persona VARCHAR(32),
           session_persona VARCHAR(32),
           accessor_env_id VARCHAR(36),
           pov_env_ids TEXT)""",
    """CREATE TABLE oauth_group_mappings (
           id VARCHAR(36) PRIMARY KEY,
           entra_group_id VARCHAR(255) UNIQUE NOT NULL,
           display_name VARCHAR(200) NOT NULL,
           workgroup VARCHAR(100) NOT NULL,
           default_permissions TEXT,
           persona VARCHAR(32),
           persona_priority INTEGER,
           created_at DATETIME)""",
)


def _read_database_py():
    with open(os.path.join(_ROOT, "web_dashboard", "database.py"), encoding="utf-8") as fh:
        return fh.read()


def _migration_statements():
    """The raw SQL strings in `init_db`'s `_migrations` list that mention a new column."""
    src = _read_database_py()
    block = src.split("_migrations = [", 1)[1].split("\n        ]", 1)[0]
    stmts = re.findall(r'"((?:ALTER TABLE|CREATE INDEX)[^"]*)"', block)
    assert len(stmts) > 100, (
        "found only %d migration statements — the list parse broke" % len(stmts))
    return stmts


def _ours():
    return [s for s in _migration_statements()
            if re.search(r"\brole_id\b|\brole_permissions\b", s)]


# ── shape ─────────────────────────────────────────────────────────────────────

def test_the_new_statements_are_present():
    ours = _ours()
    got = set(ours)
    for want in ("ALTER TABLE users ADD COLUMN role_id VARCHAR(36)",
                 "ALTER TABLE users ADD COLUMN role_permissions TEXT",
                 "ALTER TABLE oauth_group_mappings ADD COLUMN role_id VARCHAR(36)"):
        assert want in got, "missing migration statement: %s" % want


def test_no_new_statement_carries_a_default_or_a_boolean():
    """The PostgreSQL savepoint trap: an integer default on a boolean column is valid on
    SQLite and rejected by PostgreSQL, where the per-statement rollback swallows it and the
    column never appears. `is_builtin` lives on the new `access_roles` table for this
    reason -- `create_all` makes that one, so no ALTER is involved."""
    for stmt in _ours():
        assert "DEFAULT" not in stmt.upper(), (
            "a DEFAULT on a retrofitted column risks the savepoint trap: %s" % stmt)
        assert "BOOLEAN" not in stmt.upper(), (
            "a BOOLEAN ADD COLUMN is the canonical form of that trap: %s" % stmt)


def test_no_migration_anywhere_defaults_a_boolean_to_an_integer():
    """The same trap, over the WHOLE list rather than only this change's statements.

    The check above is scoped to `_ours()`, which is correct for a test about the role
    columns — and is exactly why this escaped. `ALTER TABLE users ADD COLUMN is_admin
    BOOLEAN DEFAULT 0` had been in the list since long before that check existed, was
    never covered by it, and PostgreSQL had been silently skipping it: an install that
    predated the column never received it, and would fail with `column users.is_admin
    does not exist` on the first query against `users`. SQLite accepts the integer
    literal, so nothing in a SQLite-only suite could ever have noticed.

    Found by `tests/test_postgres_migrations.py`, which runs the real statements against
    a real PostgreSQL in CI. This is the cheap static twin of that check: it needs no
    database, so it fails in front of whoever writes the next one rather than waiting
    for the service-container job.

    Narrower than the `_ours()` rule above on purpose. That one bans DEFAULT and BOOLEAN
    outright, which is the right bar for a NEW retrofit; this one bans only the
    combination that is actually invalid, because the list already contains legitimate
    `INTEGER DEFAULT 0` and `VARCHAR DEFAULT 'x'` statements that work on both backends.
    """
    offenders = [s for s in _migration_statements()
                 if re.search(r"\bBOOLEAN\s+DEFAULT\s+[01]\b", s, re.I)]
    assert not offenders, (
        "PostgreSQL rejects an integer literal as a BOOLEAN default and its savepoint "
        "swallows the whole statement, so the column silently never appears on an "
        "upgraded install. Write `DEFAULT false` / `DEFAULT true`:\n  "
        + "\n  ".join(offenders))


def test_no_new_statement_carries_a_foreign_key_clause():
    """Deliberate, and the delete guard in role_service depends on knowing it.

    A retrofit ALTER cannot add a REFERENCES clause without rewriting the table, so every
    UPGRADED install has no constraint on `role_id` whatever the model says. A design
    leaning on `ondelete="SET NULL"` would therefore be absent on exactly the installs that
    have data, and untested everywhere (SQLite enforces no foreign keys at all). Worse, a
    silent SET NULL clears `role_id` and leaves `role_permissions` populated -- a principal
    still holding a role's grants with nothing naming the role.

    The model does not declare these FKs either, so fresh and upgraded installs behave
    identically -- see `test_the_role_columns_declare_no_foreign_key` below for the second,
    independent reason.
    """
    for stmt in _ours():
        assert "REFERENCES" not in stmt.upper(), (
            "the retrofit ALTER declares a foreign key, which upgraded installs will not "
            "have: %s" % stmt)


def test_the_role_columns_declare_no_foreign_key():
    """Because `access_roles.created_by_user_id` points back at `users`, declaring the
    reverse constraint makes the two tables mutually dependent -- and SQLAlchemy cannot
    order a cycle. It warns and then IGNORES the offending constraints when sorting, so a
    fresh PostgreSQL install can emit CREATE TABLE users with REFERENCES access_roles
    before that table exists. SQLite accepts a forward reference, so the entire suite is
    blind to it; this asserts the shape instead.
    """
    from web_dashboard.database import OAuthGroupMapping, User
    for model, col in ((User, "role_id"), (User, "role_permissions"),
                       (OAuthGroupMapping, "role_id")):
        fks = model.__table__.columns[col].foreign_keys
        assert not fks, (
            "%s.%s declares a foreign key: %s. That closes a cycle with "
            "access_roles.created_by_user_id, and an upgraded install would not have the "
            "constraint anyway." % (model.__name__, col, {str(f.target_fullname) for f in fks}))


def test_the_table_order_is_resolvable_and_puts_users_first():
    """The consequence of the above, asserted directly rather than inferred.

    `access_roles.created_by_user_id` references `users`, so `users` must be created first.
    A cycle makes that order arbitrary, which is the failure this pins.
    """
    import warnings
    from sqlalchemy.schema import sort_tables
    from web_dashboard.database import Base
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        order = [t.name for t in sort_tables(Base.metadata.tables.values()) if t is not None]
    cycles = [str(w.message) for w in caught if "cycle" in str(w.message).lower()]
    assert not cycles, "SQLAlchemy cannot sort the tables: %s" % cycles
    assert order.index("users") < order.index("access_roles"), (
        "access_roles is created before users, but it references users.id")


def test_the_new_table_has_no_alter_statement():
    """`create_all` makes new tables, so `access_roles` needs no entry -- and must not have
    one, since an ALTER against a table that does not exist yet is a silent rollback."""
    for stmt in _migration_statements():
        assert "access_roles" not in stmt, (
            "access_roles has a migration entry; create_all already makes it: %s" % stmt)


def test_the_role_columns_are_indexed_where_they_are_queried():
    """`fan_out`, `reconcile` and the assignee count all filter users by role_id."""
    assert any("CREATE INDEX" in s and "users(role_id)" in s for s in _ours()), (
        "users.role_id has no index, and three role operations filter on it")


# ── behaviour, against the OLD shape ─────────────────────────────────────────

def test_the_migration_applies_to_a_table_that_predates_it_and_is_idempotent():
    """Create both tables in their pre-change shape with a row in each, run `init_db`
    TWICE, and assert the columns appear exactly once and backfill to NULL.

    A pre-existing row must read NULL/NULL -- "this principal holds no role" -- because that
    is what keeps the change from altering anybody's access.
    """
    tmp = tempfile.mkdtemp(prefix="rbac-migration-test-")
    db_path = os.path.join(tmp, "legacy.db")

    conn = sqlite3.connect(db_path)
    for ddl in _LEGACY_DDL:
        conn.execute(ddl)
    conn.execute("INSERT INTO users (id, username) VALUES ('u1', 'predates-roles')")
    conn.execute("INSERT INTO oauth_group_mappings "
                 "(id, entra_group_id, display_name, workgroup) "
                 "VALUES ('g1', 'gid-1', 'Legacy Group', 'default')")
    conn.commit()
    conn.close()

    # A child process per run: init_db reads DATABASE_URL at import time, and this file may
    # already have imported the module under a different URL.
    import subprocess
    runner = (
        "import sys; sys.path.insert(0, %r)\n"
        "from web_dashboard.database import init_db\n"
        "init_db()\n" % _ROOT
    )
    env = dict(os.environ)
    # Forward slashes: a backslash path gives SQLAlchemy "unable to open database file".
    env["DATABASE_URL"] = "sqlite:///" + db_path.replace(os.sep, "/")
    env["JWT_SECRET_KEY"] = "test-secret-rbac-migration"
    for run in (1, 2):
        proc = subprocess.run([sys.executable, "-c", runner], env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, (
            "init_db run %d failed: %s" % (run, proc.stderr[-800:]))

    conn = sqlite3.connect(db_path)
    try:
        for table, cols in _NEW_COLUMNS.items():
            present = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
            for col in cols:
                assert present.count(col) == 1, (
                    "%s.%s appears %d times after two runs"
                    % (table, col, present.count(col)))

        row = conn.execute(
            "SELECT role_id, role_permissions FROM users WHERE id='u1'").fetchone()
        assert row == (None, None), (
            "a user that predates the change did not backfill to 'no role': %r" % (row,))
        assert conn.execute(
            "SELECT role_id FROM oauth_group_mappings WHERE id='g1'").fetchone() == (None,)

        # create_all made the new table, and the boot seed filled it -- exactly once.
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='access_roles'")]
        assert names == ["access_roles"], "create_all did not make access_roles"

        from web_dashboard.services.role_service import _BUILTIN_ROLES
        seeded = conn.execute("SELECT COUNT(*) FROM access_roles").fetchone()[0]
        assert seeded == len(_BUILTIN_ROLES), (
            "expected %d built-in roles after two boots, found %d — the seed is not "
            "idempotent" % (len(_BUILTIN_ROLES), seeded))
    finally:
        conn.close()


def test_the_seed_runs_outside_the_advisory_locked_transaction():
    """A data seed inside that transaction is the QueuePool leak that hung cold app+worker
    co-deploys, which the comments around the seed block already record."""
    src = _read_database_py()
    body = src.split("def init_db(", 1)[1]
    ddl, _, after = body.partition("with SessionLocal() as _seed_db:")
    assert after, "init_db no longer has a post-lock seed block"
    assert "role_service.seed_builtins(" in after, (
        "the role seed is not in the post-lock block")

    # Comments stripped before looking for a CALL: the migration block carries a comment
    # explaining that access_roles needs no entry because the seed fills it, and a plain
    # substring test cannot tell that prose from a call -- it would fail on its own
    # documentation.
    code = "\n".join(ln for ln in ddl.splitlines()
                     if not ln.strip().startswith("#"))
    assert "role_service.seed_builtins(" not in code, (
        "the role seed runs inside the advisory-locked DDL transaction, which is the "
        "QueuePool leak that hung cold app+worker co-deploys")


def test_the_seed_cannot_stop_the_app_booting():
    """House rule, stated in the comments beside every other seed there."""
    src = _read_database_py()
    after = src.split("with SessionLocal() as _seed_db:", 1)[1]
    block = after.split("role_service.seed_builtins", 1)[0]
    assert "try:" in block.rsplit("\n", 6)[-1] or "try:" in block[-300:], (
        "the role seed is not wrapped in a try/except, so a seed failure would stop boot")


def test_the_backfill_note_names_the_third_table():
    """A scope added later has to widen THREE tables now: users.permissions,
    oauth_group_mappings.default_permissions, and the BUILT-IN rows in access_roles -- whose
    definitions are frozen literals that do not track the catalog. The third is the one that
    will be missed, and it has the widest blast radius of the three."""
    src = _read_database_py()
    note = src.split("_BACKFILL_V1_MARKER", 1)[0][-2000:]
    assert "access_roles" in note, (
        "nothing beside the frozen backfill list tells the next author that a new scope "
        "must widen the built-in roles too")
    assert "reconcile" in note, (
        "the note does not say to reconcile, so widening the role would change nothing "
        "for anyone already holding it")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print("ok   %s" % fn.__name__)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print("FAIL %s: %s" % (fn.__name__, e))
    print("\n%d/%d passed" % (len(fns) - failures, len(fns)))
    sys.exit(1 if failures else 0)
