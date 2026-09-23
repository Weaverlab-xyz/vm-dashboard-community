"""Editing tags: the guard, the fan-out, and what gets written to the audit log.

`tag_batch.apply_tag_edit` is the one path all four clouds take, single VM or fifty. Four
properties, in the order it would hurt to lose them:

1. **A protected key never reaches a cloud call.** `managed-by` is how `/costs`
   attributes spend and how unmanaged-VM discovery tells this dashboard's own instances
   from somebody else's; `povEnvironment` is what POV teardown selects on. Losing one
   does not break anything visibly — the resource keeps running and keeps billing, and
   nothing can find it again. The guard runs before the first read, so a batch cannot
   half-apply one.
2. **One VM's failure is not the batch's.** Fifty instances is fifty independent API
   calls. A stopped VM, a deleted VM, one throttled region — each fails alone and is
   reported by NAME, because "3 failed" sends an operator to check all fifty.
3. **A no-op is not a success and is not audited.** Re-applying a tag a VM already has
   changes nothing; an audit row claiming otherwise makes the log untrustworthy, and the
   log is the reason the feature is auditable at all.
4. **The audit row lands after the cloud call, one per VM.** Before it, and a refused
   write leaves a record saying it happened.

The route half is checked too, because the rule can be right while a router forgets to
call it — the same split `tests/test_workgroup_resource_routes.py` documents.

Runs under pytest, or standalone:  python tests/test_tag_edit.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(_TMP, "tagedit.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# Probe the app deps BY NAME and let anything else propagate — a blanket
# `except Exception: skip` here would let this file exit 0 having tested nothing.
# See tests/test_import_guard_narrowness.py.
try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi import HTTPException

from web_dashboard.api import tag_batch
from web_dashboard.database import AuditLog, Base, SessionLocal, engine

Base.metadata.create_all(bind=engine)


class _Target:
    """Stands in for a route's pydantic target model."""
    def __init__(self, name):
        self.name = name

    def model_dump_json(self):
        return '{"name": "%s"}' % self.name


def _run(add, remove, targets, applier, db=None):
    own = db is None
    db = db or SessionLocal()
    try:
        return asyncio.run(tag_batch.apply_tag_edit(
            db, cloud="aws", targets=targets, add=add, remove=remove,
            apply_one=applier, label_of=lambda t: t.name, created_by="tester"))
    finally:
        if own:
            db.close()


def _ok(before=None, after=None):
    """An applier that reports a change, and records what it was asked to do."""
    calls = []

    async def apply(target):
        calls.append(target.name)
        return (before or {}), (after if after is not None else {"env": "prod"})
    apply.calls = calls
    return apply


# ── 1. the guard ─────────────────────────────────────────────────────────────

def _refused(add, remove=()):
    applier = _ok()
    try:
        _run(add, list(remove), [_Target("vm1")], applier)
    except HTTPException as exc:
        return exc.status_code, str(exc.detail), applier.calls
    return 0, "", applier.calls


def test_a_protected_key_is_refused_before_any_cloud_call():
    code, detail, calls = _refused({"managed-by": "mine"})
    assert code == 409, code
    assert calls == [], "the cloud was called despite the refusal"


def test_removing_a_protected_key_is_refused_as_well_as_setting_it():
    """Deleting `managed-by` is exactly as damaging as overwriting it — the resource
    drops out of /costs and out of the dashboard's own idea of what it owns."""
    code, _detail, calls = _refused({}, ["managed-by"])
    assert code == 409 and calls == []


def test_the_workgroup_key_is_refused_and_the_message_names_the_alternative():
    code, detail, _ = _refused({"workgroup": "team-b"})
    assert code == 409
    assert "reassign" in detail, detail


def test_the_refusal_is_409_not_400():
    """400 reads as "you typed it wrong" and sends the operator to fix the wrong thing:
    the request is well-formed and they are allowed to edit tags — this KEY is spoken for."""
    assert _refused({"managed-by": "x"})[0] == 409


def test_an_illegal_key_for_this_cloud_is_refused_before_any_cloud_call():
    code, detail, calls = _refused({"k" * 200: "v"})
    assert code == 409 and calls == []


def test_an_empty_edit_is_refused():
    code, _d, calls = _refused({}, [])
    assert code == 400 and calls == []


def test_no_targets_is_refused():
    applier = _ok()
    try:
        _run({"env": "prod"}, [], [], applier)
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("an empty selection was accepted")
    assert applier.calls == []


def test_more_than_the_cap_is_refused_whole():
    applier = _ok()
    many = [_Target(f"vm{i}") for i in range(tag_batch.TAG_MAX_TARGETS + 1)]
    try:
        _run({"env": "prod"}, [], many, applier)
    except HTTPException as exc:
        assert exc.status_code == 400
        assert str(tag_batch.TAG_MAX_TARGETS) in str(exc.detail)
    else:
        raise AssertionError("over-cap selection accepted")
    assert applier.calls == [], "a partial batch ran before the cap was checked"


def test_the_cap_equals_the_bulk_power_cap():
    """An operator who may select fifty VMs for one action should not meet a different
    number on the next one."""
    from web_dashboard.api.power_batch import BULK_MAX_TARGETS
    assert tag_batch.TAG_MAX_TARGETS == BULK_MAX_TARGETS


# ── 2. the fan-out ───────────────────────────────────────────────────────────

def test_every_target_is_visited():
    applier = _ok()
    out = _run({"env": "prod"}, [], [_Target("a"), _Target("b")], applier)
    assert applier.calls == ["a", "b"]
    assert [u["name"] for u in out["updated"]] == ["a", "b"]


def test_a_repeated_target_is_visited_once():
    """The second pass would read what the first just wrote and log a second audit row
    for one edit."""
    applier = _ok()
    _run({"env": "prod"}, [], [_Target("a"), _Target("a")], applier)
    assert applier.calls == ["a"]


def test_one_failure_does_not_end_the_batch_and_is_named():
    async def apply(target):
        if target.name == "b":
            raise RuntimeError("instance is terminated")
        return {}, {"env": "prod"}

    out = _run({"env": "prod"}, [], [_Target("a"), _Target("b"), _Target("c")], apply)
    assert [u["name"] for u in out["updated"]] == ["a", "c"]
    assert [f["name"] for f in out["failed"]] == ["b"]
    assert "terminated" in out["failed"][0]["error"]


def test_an_http_refusal_for_one_vm_is_carried_as_that_vms_failure():
    """A 404 for one instance must not 404 the whole request."""
    async def apply(target):
        if target.name == "b":
            raise HTTPException(status_code=404, detail="not deployed by this dashboard")
        return {}, {"env": "prod"}

    out = _run({"env": "prod"}, [], [_Target("a"), _Target("b")], apply)
    assert out["count"] == 1
    assert "not deployed" in out["failed"][0]["error"]


def test_a_no_op_is_reported_as_unchanged_not_as_updated():
    async def apply(target):
        return {"env": "prod"}, {"env": "prod"}

    out = _run({"env": "prod"}, [], [_Target("a")], apply)
    assert out["updated"] == [] and out["count"] == 0
    assert [u["name"] for u in out["unchanged"]] == ["a"]


def test_count_is_what_changed_not_what_was_selected():
    async def apply(target):
        if target.name == "a":
            return {}, {"env": "prod"}
        return {"env": "prod"}, {"env": "prod"}

    out = _run({"env": "prod"}, [], [_Target("a"), _Target("b")], apply)
    assert out["count"] == 1


def test_the_result_carries_render_ready_chips_so_the_row_updates_without_a_refetch():
    """A refetch can land on a sibling worker whose cache still holds the old tags, and
    silently undo what the operator just watched succeed."""
    out = _run({"env": "prod"}, [], [_Target("a")],
               _ok({}, {"managed-by": "vm-dashboard", "env": "prod"}))
    chips = out["updated"][0]["tags"]
    assert [c["key"] for c in chips] == ["managed-by", "env"]
    assert chips[0]["cls"] == "system" and chips[1]["cls"] == "user"


def test_added_reports_what_actually_changed_not_what_was_asked():
    """`create_tags` is an upsert, so a requested "add" that matched the existing value
    changed nothing and should not be reported as an edit."""
    out = _run({"env": "prod", "new": "x"}, [],
               [_Target("a")], _ok({"env": "prod"}, {"env": "prod", "new": "x"}))
    assert out["updated"][0]["added"] == {"new": "x"}


def test_removed_reports_only_keys_that_were_present():
    out = _run({}, ["gone", "never"], [_Target("a")],
               _ok({"gone": "1", "keep": "2"}, {"keep": "2"}))
    assert out["updated"][0]["removed"] == ["gone"]


# ── 3. the audit trail ───────────────────────────────────────────────────────

def _audit_rows(db):
    return db.query(AuditLog).filter(AuditLog.action == tag_batch.AUDIT_ACTION).all()


def test_one_audit_row_per_changed_vm():
    db = SessionLocal()
    try:
        before = len(_audit_rows(db))
        _run({"env": "prod"}, [], [_Target("aa"), _Target("bb")], _ok(), db=db)
        rows = _audit_rows(db)
        assert len(rows) - before == 2
        assert {r.target_vm for r in rows[-2:]} == {"aa", "bb"}
    finally:
        db.close()


def test_the_audit_row_records_the_actor_and_what_changed():
    db = SessionLocal()
    try:
        _run({"env": "prod"}, [], [_Target("cc")], _ok(), db=db)
        row = [r for r in _audit_rows(db) if r.target_vm == "cc"][0]
        assert row.username == "tester"
        assert row.details_dict["cloud"] == "aws"
        assert row.details_dict["added"] == {"env": "prod"}
    finally:
        db.close()


def test_a_no_op_writes_no_audit_row():
    """An entry for a change that did not happen is what makes a log untrustworthy."""
    async def apply(target):
        return {"env": "prod"}, {"env": "prod"}

    db = SessionLocal()
    try:
        before = len(_audit_rows(db))
        _run({"env": "prod"}, [], [_Target("dd")], apply, db=db)
        assert len(_audit_rows(db)) == before
    finally:
        db.close()


def test_a_failed_vm_writes_no_audit_row():
    async def apply(target):
        raise RuntimeError("nope")

    db = SessionLocal()
    try:
        before = len(_audit_rows(db))
        _run({"env": "prod"}, [], [_Target("ee")], apply, db=db)
        assert len(_audit_rows(db)) == before
    finally:
        db.close()


def test_the_audit_action_is_dotted_so_the_prefix_filter_groups_it():
    """api/audit.py filters `action` by PREFIX, which is why the newer subsystems use a
    dotted namespace."""
    assert tag_batch.AUDIT_ACTION.startswith("tags.")


# ── 4. the routes ────────────────────────────────────────────────────────────

def test_every_cloud_exposes_the_same_tag_route():
    """One path on all four, so a shared editor never needs a per-cloud lookup table."""
    from web_dashboard.main import app
    paths = {r.path for r in app.routes}
    for cloud in ("aws", "azure", "gcp", "oci"):
        assert f"/api/{cloud}/instances/tags" in paths, cloud


def test_every_tag_route_goes_through_the_shared_guard():
    """A router that wrote tags itself would bypass assert_editable — the rule can be
    right while one of four callers forgets it."""
    import inspect
    from web_dashboard.api import aws, azure, gcp, oci
    for mod in (aws, azure, gcp, oci):
        src = inspect.getsource(mod)
        assert "tag_batch.apply_tag_edit" in src, mod.__name__


def test_every_tag_route_requires_write_not_read():
    import inspect
    from web_dashboard.api import aws, azure, gcp, oci
    for mod, cloud in ((aws, "aws"), (azure, "azure"), (gcp, "gcp"), (oci, "oci")):
        src = inspect.getsource(mod)
        head = src[src.index("/instances/tags"):]
        head = head[:head.index("async def") + 600]
        assert f'require_permission("{cloud}", "write")' in head, cloud


def test_no_new_permission_scope_was_invented_for_tags():
    """api/auth.py records that ADDING a scope silently revokes it for everyone, because
    nothing is stored against a key nobody has been granted."""
    from web_dashboard.api.auth import PERMISSION_SCOPES
    assert "tags" not in PERMISSION_SCOPES


# ── 5. Proxmox: two paths, one guard ─────────────────────────────────────────

def test_the_proxmox_route_exists_alongside_the_clouds():
    from web_dashboard.main import app
    assert "/api/proxmox/instances/tags" in {r.path for r in app.routes}


def test_both_proxmox_paths_run_the_same_guard():
    """The agent-brokered half does NOT go through apply_tag_edit — it queues jobs — so
    the guard is shared explicitly. A guard only one of two paths runs is no guard."""
    import inspect
    from web_dashboard.api import proxmox
    src = inspect.getsource(proxmox.edit_vm_tags)
    assert "assert_edit_allowed" in src
    # …and the agent branch is taken AFTER it, never before.
    assert src.index("assert_edit_allowed") < src.index("_queue_tag_jobs")


def test_the_agent_path_reports_queued_not_updated():
    """An agent-bound write has not happened when the request returns. Calling it
    `updated` would claim a change the page could then render as done."""
    import inspect
    from web_dashboard.api import proxmox
    src = inspect.getsource(proxmox._queue_tag_jobs)
    assert '"updated": []' in src and '"queued": queued' in src


def test_the_desired_set_is_computed_in_one_place():
    """Both paths need the FINAL tag list, because Proxmox replaces the whole field.
    Two implementations is how they come to disagree about what an edit means."""
    from web_dashboard.api.proxmox import _desired_tags
    assert _desired_tags(["a", "b"], {"c": ""}, ["a"]) == ["b", "c"]
    assert _desired_tags(["a"], {"a": ""}, []) == ["a"], "an existing tag is not doubled"
    assert _desired_tags(["a"], {}, ["a"]) == [], "removing the last tag is legitimate"
    assert _desired_tags([], {"z": "", "y": ""}, []) == ["z", "y"], "order preserved"


def test_a_proxmox_tag_may_not_carry_a_value():
    """A Proxmox tag is a bare label. Refused rather than silently dropped, because a
    dropped value is an edit that did something other than what was asked."""
    from web_dashboard.api import tag_batch as tb
    try:
        tb.assert_edit_allowed("proxmox", {"env": "prod"}, [])
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "bare label" in str(exc.detail)
    else:
        raise AssertionError("a valued Proxmox tag was accepted")


def test_the_protected_keys_apply_to_proxmox_too():
    from web_dashboard.api import tag_batch as tb
    try:
        tb.assert_edit_allowed("proxmox", {"workgroup": ""}, [])
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("workgroup was editable on proxmox")


def test_the_agent_verb_is_refused_for_a_tagless_hypervisor():
    """vSphere/Nutanix/XCP-ng store tags in three different models, none of which the
    dashboard reads. Said before a job exists, not after a round trip."""
    import inspect
    from web_dashboard.api import hypervisor_deps
    src = inspect.getsource(hypervisor_deps.agent_tag_job)
    assert 'conn.kind != "proxmox"' in src
    assert "501" in src


def test_the_agent_job_carries_the_complete_desired_set():
    """Not a delta. Proxmox replaces the whole field, so a delta would have to be
    re-derived agent-side against a read the agent would then have to trust."""
    import inspect
    from web_dashboard.api import hypervisor_deps
    src = inspect.getsource(hypervisor_deps.agent_tag_job)
    assert '"tags": list(tags)' in src


def test_a_tag_the_agent_would_refuse_is_caught_before_a_job_row_exists():
    """An operator typo should read as a sentence, not as a failed job on /jobs."""
    import inspect
    from web_dashboard.api import hypervisor_deps
    src = inspect.getsource(hypervisor_deps.agent_tag_job)
    assert src.index("tags_refusal") < src.index("create_job")


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
