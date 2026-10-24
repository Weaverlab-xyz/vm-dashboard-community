"""Build the app's schema in this test file's own database.

A file whose code under test reads the database -- most often ``config_service`` reading
``app_config`` -- has to create the tables itself. ``scripts/ci/run_tests.sh`` gives every
file an empty database of its own, so a file can no longer pass on tables an earlier
file happened to create in the shared ``./vm_cli.db``.

Call it at import, before anything opens a session::

    from _schema import build_schema
    build_schema("persona-identity")

Not a test file (no ``test_`` prefix): imported by tests, run by none.
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_schema(name: str) -> None:
    """Point at a fresh SQLite file unless DATABASE_URL is set, then create every table.

    Under the runner DATABASE_URL is already this file's own empty database; run by
    hand, the file gets a temp one rather than the developer's ./vm_cli.db. Puts the
    repo root on sys.path itself, so it works wherever in a file's setup it is called.
    """
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    os.environ.setdefault("JWT_SECRET_KEY", f"test-secret-{name}")
    os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(
        tempfile.mkdtemp(prefix=f"{name}-"), "test.db"))
    from web_dashboard.database import Base, engine
    Base.metadata.create_all(bind=engine)
