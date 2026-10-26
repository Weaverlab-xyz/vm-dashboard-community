"""Seeding the default workstream survives a second worker seeding it at the same time.

The image runs ``gunicorn -w 2`` against a fresh SQLite file. Both workers can find the
workgroups table empty in ``seed_if_empty``; the slower one's insert then fails on the
unique name and, before this, took its worker down at startup (the Images workflow's boot
check, intermittently: ``UNIQUE constraint failed: workgroups.name``). These pin:

- losing the race is a no-op: the other worker's ``default`` row is kept, nothing raises;
- an integrity error with no ``default`` row behind it is still raised;
- a normal first boot still seeds ``default``.

Run: python tests/test_workgroup_seed_race.py   (or under pytest)
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="workgroup-seed-race-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-workgroup-seed-race")

from sqlalchemy.exc import IntegrityError  # noqa: E402

from web_dashboard import database as D  # noqa: E402
from web_dashboard.services import workgroup_service as wgs  # noqa: E402

D.Base.metadata.create_all(bind=D.engine)


class _SawEmpty:
    """A session that saw an empty table on its first look, as the slower worker did."""

    def __init__(self, db, commit_error=None):
        self._db, self._looked, self._commit_error = db, False, commit_error

    def query(self, *args, **kwargs):
        if not self._looked:
            self._looked = True

            class _Empty:
                def first(self):
                    return None
            return _Empty()
        return self._db.query(*args, **kwargs)

    def commit(self):
        if self._commit_error is not None:
            raise self._commit_error
        return self._db.commit()

    def __getattr__(self, name):
        return getattr(self._db, name)


def _defaults(db):
    return db.query(D.Workgroup).filter(D.Workgroup.name == "default").count()


def _clear(db):
    db.query(D.Workgroup).delete()
    db.commit()


def test_a_normal_first_boot_seeds_default():
    with D.SessionLocal() as db:
        _clear(db)
        wgs.seed_if_empty(db)
        assert _defaults(db) == 1


def test_losing_the_race_keeps_the_other_workers_row_and_does_not_raise():
    with D.SessionLocal() as db:
        _clear(db)
        wgs.seed_if_empty(db)          # the faster worker
    with D.SessionLocal() as db:
        wgs.seed_if_empty(_SawEmpty(db))   # the slower one, past its empty check
        assert _defaults(db) == 1


def test_an_integrity_error_with_no_default_behind_it_is_raised():
    with D.SessionLocal() as db:
        _clear(db)
        error = IntegrityError("INSERT INTO workgroups", {}, Exception("something else"))
        try:
            wgs.seed_if_empty(_SawEmpty(db, commit_error=error))
            raise AssertionError("expected IntegrityError")
        except IntegrityError:
            pass
        assert _defaults(db) == 0


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
