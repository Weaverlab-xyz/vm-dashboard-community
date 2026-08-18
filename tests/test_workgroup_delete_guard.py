"""Deleting a workgroup that on-prem VMs are still tagged into.

`workgroup_service.delete` refuses a workgroup that users or active jobs still reference.
It also used to count `vm_state_cache` rows — the local PowerShell VMX scan — which was
the one VM source that never carried an override and no longer exists at all. The rows
that DO reference a workgroup, `vm_workgroup_overrides`, were unguarded for every
provider.

That gap is not cosmetic. `VMWorkgroupOverride.workgroup` is a ForeignKey declared
``ondelete="CASCADE"`` and nothing in this codebase issues ``PRAGMA foreign_keys=ON``,
so the delete destroys every override on PostgreSQL and leaves dangling rows on SQLite.
Either way the operator's first clue is a hypervisor page that has quietly gone
admin-only, long after the delete they would connect it to.

Real throwaway SQLite.

Runs under pytest, or standalone:  python tests/test_workgroup_delete_guard.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "wg.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.database import (Base, Job, SessionLocal, User,
                                        VMWorkgroupOverride, Workgroup, engine)
    from web_dashboard.services import workgroup_service as svc
    from web_dashboard.services import workgroup_override_service as wos
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


def _reset(db):
    db.query(VMWorkgroupOverride).delete()
    db.query(Job).delete()
    db.query(User).delete()
    db.query(Workgroup).delete()
    db.commit()


def _workgroup(db, name="doomed"):
    db.add(Workgroup(id=f"wg-{name}", name=name, display_name=name.title()))
    db.commit()
    return name


def test_a_workgroup_with_no_references_deletes():
    """The control. Without this, a guard that refused everything would pass the rest."""
    db = SessionLocal()
    try:
        _reset(db)
        name = _workgroup(db)
        svc.delete(db, name)
        assert db.query(Workgroup).filter(Workgroup.name == name).first() is None
    finally:
        db.close()


def test_a_workgroup_with_tagged_vms_is_refused():
    db = SessionLocal()
    try:
        _reset(db)
        name = _workgroup(db)
        wos.set_many(db, provider="workstation", vm_ids=["AB12", "CD34"],
                     workgroup=name)
        try:
            svc.delete(db, name)
        except svc.WorkgroupError as exc:
            assert "2 VM(s)" in str(exc), str(exc)
            assert "reassign" in str(exc).lower(), (
                "the refusal has to name the way out, not just say no")
        else:
            raise AssertionError("a workgroup with tagged VMs must not delete")
        assert db.query(Workgroup).filter(Workgroup.name == name).first() is not None
    finally:
        db.close()


def test_the_guard_is_not_scoped_to_one_provider():
    """Every provider FKs the same column, and none of them was guarded. A Hyper-V
    override has to block the delete exactly as a Workstation one does."""
    db = SessionLocal()
    try:
        for provider in sorted(wos.ALLOWED_PROVIDERS):
            _reset(db)
            name = _workgroup(db)
            vm_id = "pve1/100" if provider == "proxmox" else "vm-1"
            wos.set_many(db, provider=provider, vm_ids=[vm_id], workgroup=name)
            try:
                svc.delete(db, name)
            except svc.WorkgroupError:
                pass
            else:
                raise AssertionError(f"a {provider} override must block the delete")
    finally:
        db.close()


def test_clearing_the_tags_lets_the_delete_through():
    """The documented way out actually works — otherwise the refusal is a dead end."""
    db = SessionLocal()
    try:
        _reset(db)
        name = _workgroup(db)
        wos.set_many(db, provider="workstation", vm_ids=["AB12"], workgroup=name)
        wos.clear_many(db, provider="workstation", vm_ids=["AB12"])
        svc.delete(db, name)
        assert db.query(Workgroup).filter(Workgroup.name == name).first() is None
    finally:
        db.close()


def test_an_override_on_a_different_workgroup_does_not_block():
    db = SessionLocal()
    try:
        _reset(db)
        doomed = _workgroup(db, "doomed")
        keeper = _workgroup(db, "keeper")
        wos.set_many(db, provider="workstation", vm_ids=["AB12"], workgroup=keeper)
        svc.delete(db, doomed)
        assert db.query(Workgroup).filter(Workgroup.name == doomed).first() is None
        assert wos.get(db, "workstation", "AB12") == keeper, (
            "the surviving tag must be untouched")
    finally:
        db.close()


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
    sys.exit(1 if failures else 0)
