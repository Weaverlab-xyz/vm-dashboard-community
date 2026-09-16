"""``init_db`` must bound its DDL lock waits, and in the right place.

Why this is a SOURCE test and not a behavioural one: reproducing the failure needs a
real PostgreSQL plus a second connection holding a conflicting lock, and the suite runs
on SQLite (where ``_is_sqlite`` skips this whole branch). The ordering is the invariant
worth pinning, and the ordering is statically visible -- the same reasoning as
``tests/test_worker_health.py``, which pins that ``serve()`` runs before ``init_db()``.

The outage this guards against (pov.weaverlab.app, 2026-09-16): ``ALTER TABLE jobs``
needs ACCESS EXCLUSIVE, so it queues behind the jobs_worker's in-flight transaction.
PostgreSQL lock queues are FIFO, so that *pending* request then blocks every later
reader of ``jobs`` -- and since the routes serving those reads are ``async def`` running
synchronous SQLAlchemy, the stall lands on the event loop and freezes the whole Gunicorn
worker. Every path timed out at the 240s ingress limit, including ``/api/health``, which
does no I/O at all. A bounded wait turns that from an outage into a skipped migration.
"""
import ast
import pathlib

DB_PY = pathlib.Path(__file__).resolve().parents[1] / "web_dashboard" / "database.py"


def _init_db() -> ast.FunctionDef:
    tree = ast.parse(DB_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "init_db":
            return node
    raise AssertionError("init_db() not found in database.py")


def _sql_literals_in_order(node: ast.AST) -> list[str]:
    """Every string constant under ``node``, in source order.

    ``ast.walk`` is breadth-first and does not preserve order, so walk explicitly.
    Comments and docstrings cannot appear here: ``ast`` drops comments outright, and a
    docstring is an ``Expr``/``Constant`` we filter by content below. That matters --
    a source scan that matches its own explanatory comment passes vacuously.
    """
    out: list[str] = []

    def visit(n: ast.AST) -> None:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return out


def test_init_db_sets_a_lock_timeout_for_its_ddl():
    sql = _sql_literals_in_order(_init_db())
    assert any("lock_timeout" in s for s in sql), (
        "init_db() no longer bounds its lock waits. Without this, a migration that "
        "cannot get its table lock queues forever and a pending ACCESS EXCLUSIVE "
        "blocks every reader of that table, which takes the whole app down."
    )


def test_the_lock_timeout_is_set_before_create_all_runs():
    """A timeout set after the DDL protects nothing."""
    node = _init_db()
    sql = _sql_literals_in_order(node)

    def index_of(label: str, pred) -> int:
        # Not bare next(): a missing statement should name itself, not raise
        # StopIteration from inside the test and make the report unreadable.
        for i, s in enumerate(sql):
            if pred(s):
                return i
        raise AssertionError(f"init_db() contains no {label} statement at all")

    timeout_at = index_of("SET LOCAL lock_timeout", lambda s: "lock_timeout" in s)
    advisory_at = index_of("pg_advisory_xact_lock", lambda s: "pg_advisory" in s)
    first_ddl_at = index_of(
        "ALTER TABLE / CREATE INDEX",
        lambda s: s.startswith(("ALTER TABLE", "CREATE INDEX")))

    assert advisory_at < timeout_at, (
        "The advisory lock must be taken before the lock_timeout is set: the SET LOCAL "
        "applies to this transaction, and taking the advisory lock under a short "
        "timeout would make concurrent init_db callers fail instead of serialize."
    )
    assert timeout_at < first_ddl_at, (
        "lock_timeout is set after the first DDL statement, so the statements that "
        "actually take table locks are still unbounded."
    )


def test_the_timeout_is_a_bounded_positive_value():
    tree = ast.parse(DB_PY.read_text(encoding="utf-8"))
    value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_DDL_LOCK_TIMEOUT_MS":
                    value = node.value.value
    assert isinstance(value, int), "_DDL_LOCK_TIMEOUT_MS must be an int literal"
    assert 0 < value <= 30_000, (
        f"_DDL_LOCK_TIMEOUT_MS={value} defeats the purpose. 0 means WAIT FOREVER in "
        "PostgreSQL -- the exact behaviour this exists to prevent -- and a value this "
        "large re-opens the outage window."
    )


def test_a_lock_timeout_is_reported_rather_than_swallowed():
    """The migration loop swallows "column already exists" by design. A lock timeout is
    the one failure that means the statement never ran, so it must not be silent."""
    src = ast.unparse(_init_db())  # unparse emits no comments
    assert "55P03" in src, (
        "The migration loop no longer distinguishes PostgreSQL's lock_not_available "
        "SQLSTATE, so a skipped migration is now indistinguishable from an applied "
        "one. The symptom surfaces later as an UndefinedColumn error from a route."
    )
    assert "_lock_timed_out" in src, (
        "Nothing collects the skipped migrations, so nothing can report them."
    )
