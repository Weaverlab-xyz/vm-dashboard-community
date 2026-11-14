"""A change to a built-in role's permissions must reach installs that already have the role.

``seed_builtins`` never updates an existing role -- deliberately, so it can never re-grant
something removed -- which means editing a built-in's map in
``role_service._BUILTIN_ROLES`` reaches NEW installs only. Three hand-written fix-ups were
needed to cover that before (#1325's POV Presenter levels, #1334's Read-Only demo levels),
and nothing failed when one was forgotten.

The rule this file enforces: the frozen baseline (``tests/data/builtin_roles_baseline.json``)
plus ``database.BUILTIN_ROLE_CHANGES``, applied in order, must equal the literals. So:

  * add a grant to a built-in  -> add an entry with ``add``, or this fails;
  * remove one                 -> add an entry with ``remove``;
  * add a NEW built-in role    -> nothing: ``seed_builtins`` inserts it whole;
  * delete a built-in          -> fails: an install would keep a role nobody can edit.

Never edit the baseline to make this pass. It is what installs had when the list began.

Runs under pytest, or standalone:
    python tests/test_builtin_role_upgrades.py
"""
import json
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="builtin-role-upgrades-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-builtin-role-upgrades")

from web_dashboard.api.auth import PERMISSION_SCOPE_LEVELS  # noqa: E402
from web_dashboard.database import (BUILTIN_ROLE_CHANGES, AccessRole, Base,  # noqa: E402
                                    SchemaMarker, SessionLocal,
                                    _apply_builtin_role_changes, engine)
from web_dashboard.services import role_service  # noqa: E402

Base.metadata.create_all(bind=engine)

_BASELINE = os.path.join(_ROOT, "tests", "data", "builtin_roles_baseline.json")


def _baseline():
    with open(_BASELINE, encoding="utf-8") as fh:
        return json.load(fh)


def _normal(perms):
    # Administrator's map is {"is_admin": true}: a flag, not a level list. Kept as is.
    return {k: (sorted(v) if isinstance(v, list) else v) for k, v in perms.items() if v}


def _replay(base, changes, slug):
    flags = {k: v for k, v in base.items() if not isinstance(v, list)}
    perms = {k: set(v) for k, v in base.items() if isinstance(v, list)}
    for ch in changes:
        if ch["slug"] != slug:
            continue
        for scope, levels in (ch.get("add") or {}).items():
            perms.setdefault(scope, set()).update(levels)
        for scope, levels in (ch.get("remove") or {}).items():
            perms[scope] = perms.get(scope, set()) - set(levels)
    return _normal({**flags, **{k: list(v) for k, v in perms.items()}})


def test_the_baseline_plus_the_changes_is_the_literal():
    base = _baseline()
    literal = {r["slug"]: r["permissions"] for r in role_service._BUILTIN_ROLES}
    problems = []
    for slug, perms in base.items():
        if slug not in literal:
            problems.append(f"{slug}: a built-in was deleted -- installs keep a role nobody "
                            "can edit or remove")
            continue
        want = _normal(literal[slug])
        got = _replay(perms, BUILTIN_ROLE_CHANGES, slug)
        if got != want:
            def _lv(m, s):
                v = m.get(s, [])
                return set(v) if isinstance(v, list) else {repr(v)}
            added = {s: sorted(_lv(want, s) - _lv(got, s)) for s in set(want) | set(got)}
            removed = {s: sorted(_lv(got, s) - _lv(want, s)) for s in set(want) | set(got)}
            problems.append(
                f"{slug}: changed without an entry in BUILTIN_ROLE_CHANGES -- upgraded "
                f"installs would not get it. Needs add={ {k: v for k, v in added.items() if v} } "
                f"remove={ {k: v for k, v in removed.items() if v} }")
    assert not problems, "\n  " + "\n  ".join(problems)


def test_every_change_is_well_formed():
    markers = [ch["marker"] for ch in BUILTIN_ROLE_CHANGES]
    assert len(markers) == len(set(markers)), "a marker is reused"
    slugs = {r["slug"] for r in role_service._BUILTIN_ROLES}
    for ch in BUILTIN_ROLE_CHANGES:
        assert set(ch) <= {"marker", "slug", "add", "remove"}, ch
        assert ch["slug"] in slugs, f"{ch['marker']}: no built-in {ch['slug']!r}"
        for part in ("add", "remove"):
            for scope, levels in (ch.get(part) or {}).items():
                for level in levels:
                    assert level in PERMISSION_SCOPE_LEVELS.get(scope, ()), (
                        f"{ch['marker']}: {scope}:{level} is not a level that scope offers")


def _session():
    db = SessionLocal()
    db.query(AccessRole).delete()
    db.query(SchemaMarker).filter(SchemaMarker.key.like("test_change_%")).delete(
        synchronize_session=False)
    db.commit()
    role_service.seed_builtins(db)
    return db


def test_a_change_applies_once_to_the_built_in_row_only():
    db = _session()
    try:
        clone = role_service.create(db, name=f"RO clone {uuid.uuid4().hex[:4]}",
                                    description="", permissions={"vms": ["read"]})
        db.commit()
        changes = (
            {"marker": "test_change_add", "slug": "read-only", "add": {"vms": ["write"]}},
            {"marker": "test_change_remove", "slug": "read-only", "remove": {"audit": ["read"]}},
        )
        assert _apply_builtin_role_changes(db, changes) == 2
        db.expire_all()
        ro = role_service.get_by_slug(db, "read-only").permissions_dict
        assert ro["vms"] == ["read", "write"]
        assert "audit" not in ro, "removing a scope's last level left an empty scope"
        assert role_service.get(db, clone.id).permissions_dict == {"vms": ["read"]}, (
            "a change touched a clone of the built-in")
        assert _apply_builtin_role_changes(db, changes) == 0, "a marker did not stop a re-run"
    finally:
        db.close()


def test_a_later_change_still_applies_after_earlier_ones():
    db = _session()
    try:
        first = ({"marker": "test_change_first", "slug": "auditor", "add": {"costs": ["write"]}},)
        assert _apply_builtin_role_changes(db, first) == 1
        both = first + ({"marker": "test_change_second", "slug": "auditor",
                         "add": {"images": ["read"]}},)
        assert _apply_builtin_role_changes(db, both) == 1, (
            "only the new entry should run on an install that applied the first")
    finally:
        db.close()


def test_boot_applies_the_changes_before_reconcile():
    src = open(os.path.join(_ROOT, "web_dashboard", "database", "bootstrap.py"),
               encoding="utf-8").read()
    apply_at = src.index("_apply_builtin_role_changes(")
    assert apply_at < src.index("role_service.reconcile("), (
        "changes must land before reconcile copies each role to its holders")


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
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
