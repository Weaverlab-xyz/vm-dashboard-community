"""The retired-permission-level migration, run against PostgreSQL.

``database._retire_unenforced_levels`` rewrites JSON stored in TEXT columns across four
tables, then commits a schema marker. SQLite accepts nearly anything in that shape;
PostgreSQL is where a type or transaction mistake would surface, and production runs
PostgreSQL. So the scenarios in tests/test_permission_levels_retired.py run here too,
unchanged -- imported rather than copied, so the two cannot drift.

Skips cleanly when no PostgreSQL is configured. CI's `postgres` job sets DATABASE_URL and
runs every tests/test_postgres_*.py file. Locally:
    DATABASE_URL=postgresql://user:pw@host:5432/db python tests/test_postgres_retired_levels.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-postgres-retired-levels")

_URL = os.environ.get("DATABASE_URL", "")


def _skip(reason):
    try:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {reason}")
        sys.exit(0)


# Gate BEFORE importing anything that imports database.py: it binds its engine from
# DATABASE_URL at import time, and the scenario module's own setdefault would otherwise
# point it at a throwaway SQLite file.
if not _URL.startswith(("postgresql:", "postgres:", "postgresql+")):
    _skip("DATABASE_URL is not a PostgreSQL URL")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_permission_levels_retired as scenarios  # noqa: E402

from web_dashboard.database import engine  # noqa: E402

assert engine.dialect.name == "postgresql", engine.dialect.name


def test_every_stored_map_is_cleaned():
    scenarios.scenario_every_stored_map_is_cleaned()


def test_nobodys_effective_access_changes():
    scenarios.scenario_nobodys_effective_access_changes()


def test_it_runs_once_and_leaves_empty_and_null_maps_alone():
    scenarios.scenario_it_runs_once_and_leaves_empty_and_null_maps_alone()


def test_reconcile_rebuilds_role_copies_from_the_cleaned_roles():
    scenarios.scenario_reconcile_rebuilds_role_copies_from_the_cleaned_roles()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
