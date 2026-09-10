"""Unit tests: hypervisor_deps.queue_power_batch — the fan-out behind the bulk power
toolbar on the six on-prem VM pages.

Every one of these is a way a bulk power op can go wrong *quietly*, which is the only
interesting way for something that queues writes against real machines:

  * AN EMPTY SELECTION. Nothing to do is not the same as done. A 200 saying `count: 0`
    is read as success by a page that has just cleared its checkboxes.
  * A SELECTION TOO LARGE. Capped at the same number, and with the same sentence, as
    `inventory_service.MAX_BULK_TARGETS` — the other bulk fan-out in this codebase.
  * A DUPLICATED TARGET. One VM named twice must be one job. Two `restart` jobs against
    one guest is a second power cut arriving during the first boot, and nothing
    downstream would flag it: both jobs are individually valid.
  * ONE TARGET REFUSED, THE REST FINE. Per-target refusals genuinely do not generalise
    — one VM's workgroup, one connection's offline agent, one op with no verb for that
    kind. Those belong in `failed[]` with the rest still queued, not aborting a batch
    that is already part-queued and not silently dropped.
  * EVERY TARGET REFUSED. Must be a 400 naming the first reason, and must leave NO
    audit row: a batch that queued nothing must not leave a record shaped like one that
    did.
  * THE SHARED batch_id. It is the only handle the operator has on the batch —
    /jobs?batch_id= is where the page sends them — so every job in one call must carry
    the same one, and two calls must never collide.
  * THE SERIAL WALK. Exactly one background task for the whole batch, and it must run
    the ops one at a time. See run_power_batch's own docstring for why this is ours
    rather than inherited from Starlette.

`queue_power_batch` takes its per-router pieces as callables, so all of this runs with
no DB, no app and no FastAPI dependency graph — only `HTTPException`, which is what the
function raises and what a router's per-target refusal arrives as.

Runs under pytest, or standalone:
    python tests/test_bulk_power_fanout.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from fastapi import HTTPException

    from web_dashboard.api import hypervisor_deps as deps
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)


# ── Doubles ───────────────────────────────────────────────────────────────────

class _Target:
    """Stands in for a router's PowerOpRequest.

    Carries `model_dump_json` because that is what the de-dupe fingerprints on, and a
    plain object would silently take the `repr` fallback and pass for the wrong reason.
    """

    def __init__(self, name, vmid):
        self.name = name
        self.vmid = vmid

    def model_dump_json(self):
        return f'{{"name":"{self.name}","vmid":"{self.vmid}"}}'


class _AuditSpy:
    """Captures job_service.log_audit calls made by the function under test."""

    def __init__(self):
        self.calls = []

    def log_audit(self, db, username, action, ip_address=None, target_vm=None,
                  details=None):
        self.calls.append({"username": username, "action": action,
                           "details": details or {}})


class _Background:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


def _patch_audit(spy):
    """`queue_power_batch` imports job_service lazily, from the package."""
    from web_dashboard.services import job_service
    original = job_service.log_audit
    job_service.log_audit = spy.log_audit
    return lambda: setattr(job_service, "log_audit", original)


ALLOWED = ("start", "shutdown", "stop", "restart")


def _run(targets, *, op="start", refuse=None, direct=False, background=None,
         spy=None):
    """Drive queue_power_batch with a stub `queue_one`.

    `refuse` maps a VM name to the detail of the HTTPException its queue should raise,
    standing in for the 501/403/409 a real router produces per target.
    """
    refuse = refuse or {}
    queued = []

    async def queue_one(target, batch_id):
        if target.name in refuse:
            raise HTTPException(status_code=501, detail=refuse[target.name])
        queued.append((target.name, batch_id))

        async def _task():
            queued.append(("ran", target.name))

        return {"job_id": f"job-{target.name}", "status": "queued",
                "task": _task if direct else None}

    # Always patched: log_audit is hash-chained and writes to a real session, and the
    # `db` handed in here is a bare object. A test that did not care about the audit row
    # would otherwise fail on the audit write rather than on its own subject.
    restore = _patch_audit(spy if spy is not None else _AuditSpy())
    try:
        result = asyncio.run(deps.queue_power_batch(
            object(), kind="hyperv", op=op, targets=targets, allowed_ops=ALLOWED,
            queue_one=queue_one, label_of=lambda t: t.name,
            created_by="alice", background_tasks=background))
    finally:
        restore()
    return result, queued


def _detail(fn, *a, **kw):
    """The HTTPException a refusal raises, or an assertion failure if it didn't."""
    try:
        fn(*a, **kw)
    except HTTPException as exc:
        return exc.status_code, str(exc.detail)
    raise AssertionError("expected an HTTPException and got none")


# ── Selection refusals: the whole request, before any job exists ──────────────

def test_an_empty_selection_is_refused_rather_than_reported_as_done():
    status, detail = _detail(_run, [])
    assert status == 400, status
    assert "at least one VM" in detail, detail
    # And it says why, because "nothing happened" is the outcome being prevented.
    assert "report success" in detail, detail


def test_a_selection_over_the_cap_is_refused_and_names_the_way_out():
    targets = [_Target(f"vm{i}", str(i)) for i in range(deps.BULK_MAX_TARGETS + 1)]
    status, detail = _detail(_run, targets)
    assert status == 400, status
    assert str(deps.BULK_MAX_TARGETS) in detail, detail
    # The same sentence inventory_service.plan_bulk_run uses. An operator who has hit
    # one cap should recognise the other.
    assert "Narrow the selection with the filters and run again." in detail, detail


def test_the_cap_matches_the_other_bulk_fanout_in_this_codebase():
    """Not a coincidence to be re-picked: `inventory_service.MAX_BULK_TARGETS` is the
    precedent, and two different ceilings for "how many things may one click do" is the
    kind of difference nobody can explain a year later."""
    from web_dashboard.services import inventory_service
    assert deps.BULK_MAX_TARGETS == inventory_service.MAX_BULK_TARGETS, (
        "bulk power and bulk Config-Management now cap a selection differently")


def test_the_cap_admits_exactly_the_limit():
    """Off-by-one, in the direction that would silently shrink the feature."""
    targets = [_Target(f"vm{i}", str(i)) for i in range(deps.BULK_MAX_TARGETS)]
    result, _ = _run(targets)
    assert result["count"] == deps.BULK_MAX_TARGETS, result["count"]


def test_an_op_the_page_does_not_offer_is_refused_before_any_verb_lookup():
    """400, not 501. A 501 is `agent_power_job`'s answer for "this build cannot express
    that op on THIS connection" — a per-connection fact. An op outside the toolbar's own
    list never gets that far, and must not borrow that meaning."""
    status, detail = _detail(_run, [_Target("dc01", "1")], op="save")
    assert status == 400, status
    assert "'save' is not a bulk power operation" in detail, detail
    # Names what does work, because the caller is a page whose button just failed.
    for op in ALLOWED:
        assert op in detail, detail


def test_the_op_is_matched_case_and_whitespace_insensitively():
    result, _ = _run([_Target("dc01", "1")], op="  Start ")
    assert result["op"] == "start", result["op"]


# ── The de-dupe ───────────────────────────────────────────────────────────────

def test_one_vm_named_twice_is_one_job():
    a, b = _Target("dc01", "1"), _Target("dc01", "1")
    result, queued = _run([a, b, _Target("sql01", "2")])
    assert result["count"] == 2, result["jobs"]
    assert [n for n, _ in queued] == ["dc01", "sql01"], queued


def test_two_different_vms_that_share_a_display_name_both_run():
    """The fingerprint is the whole payload, not the label. Two VMs may legitimately be
    called the same thing on one host, and dropping one of them would be a silent
    refusal to power a machine the operator selected."""
    result, queued = _run([_Target("clone", "1"), _Target("clone", "2")])
    assert result["count"] == 2, result["jobs"]
    assert [n for n, _ in queued] == ["clone", "clone"], queued


def test_selection_order_is_preserved():
    names = ["web03", "dc01", "sql01", "app02"]
    result, _ = _run([_Target(n, n) for n in names])
    assert [j["name"] for j in result["jobs"]] == names, result["jobs"]


# ── Per-target refusals ───────────────────────────────────────────────────────

def test_one_refused_target_does_not_abort_the_rest():
    result, queued = _run(
        [_Target("dc01", "1"), _Target("sql01", "2"), _Target("web01", "3")],
        refuse={"sql01": "'shutdown' is not available on an agent-bound connection"})
    assert result["count"] == 2, result["jobs"]
    assert [j["name"] for j in result["jobs"]] == ["dc01", "web01"], result["jobs"]
    assert [f["name"] for f in result["failed"]] == ["sql01"], result["failed"]


def test_a_refusal_carries_its_own_reason_through_to_the_caller():
    """The per-VM detail is the whole diagnosis. Collapsing these to a count is how a
    501 about one connection's missing verb becomes "some of them didn't work"."""
    reason = "Agent 'weaverpc' is offline, so weaverpc-hyperv cannot be reached."
    result, _ = _run([_Target("dc01", "1"), _Target("sql01", "2")],
                     refuse={"sql01": reason})
    assert result["failed"] == [{"name": "sql01", "error": reason}], result["failed"]


def test_a_batch_where_every_target_is_refused_is_a_400_naming_the_first_reason():
    status, detail = _detail(
        _run, [_Target("dc01", "1"), _Target("sql01", "2")],
        refuse={"dc01": "first reason", "sql01": "second reason"})
    assert status == 400, status
    assert "No jobs were queued" in detail, detail
    assert "first reason" in detail, detail


def test_a_batch_that_queued_nothing_leaves_no_audit_row():
    """The refusal is raised BEFORE the audit write. An audit row for a batch that did
    nothing is worse than none: it is a record that looks exactly like a batch which
    powered two machines."""
    spy = _AuditSpy()
    _detail(_run, [_Target("dc01", "1")], refuse={"dc01": "nope"}, spy=spy)
    assert spy.calls == [], spy.calls


# ── The shared batch_id ───────────────────────────────────────────────────────

def test_every_job_in_one_call_shares_one_batch_id():
    _, queued = _run([_Target("dc01", "1"), _Target("sql01", "2"),
                      _Target("web01", "3")])
    ids = {batch_id for _, batch_id in queued}
    assert len(ids) == 1, ids


def test_the_returned_batch_id_is_the_one_the_jobs_were_given():
    """The response is the only place the page learns where to send the operator. A
    batch_id returned but not stamped gives /jobs?batch_id= an empty page."""
    result, queued = _run([_Target("dc01", "1"), _Target("sql01", "2")])
    assert {b for _, b in queued} == {result["batch_id"]}, (result["batch_id"], queued)


def test_two_batches_do_not_collide():
    first, _ = _run([_Target("dc01", "1")])
    second, _ = _run([_Target("dc01", "1")])
    assert first["batch_id"] != second["batch_id"]


def test_the_batch_id_survives_a_partly_refused_batch():
    """The refused targets must not consume or reset it."""
    result, queued = _run([_Target("dc01", "1"), _Target("sql01", "2")],
                          refuse={"dc01": "nope"})
    assert {b for _, b in queued} == {result["batch_id"]}


# ── The serial walk ───────────────────────────────────────────────────────────

def test_a_direct_batch_schedules_exactly_one_task():
    """One task for the batch, never one per VM — see run_power_batch's docstring. A
    router that scheduled per VM would be relying on Starlette's loop to serialise,
    which is an upstream implementation detail this codebase declines to depend on."""
    bg = _Background()
    result, _ = _run([_Target("dc01", "1"), _Target("sql01", "2"),
                      _Target("web01", "3")], direct=True, background=bg)
    assert len(bg.tasks) == 1, bg.tasks
    func, args, _ = bg.tasks[0]
    assert func is deps.run_power_batch, func
    assert len(args[0]) == 3, args[0]
    assert result["count"] == 3


def test_an_agent_batch_schedules_nothing():
    """An agent-bound connection has no local work: the agent leases each job itself.
    A task scheduled here would run against a host this process cannot reach."""
    bg = _Background()
    _run([_Target("dc01", "1"), _Target("sql01", "2")], direct=False, background=bg)
    assert bg.tasks == [], bg.tasks


def test_a_refused_target_contributes_no_task():
    bg = _Background()
    _run([_Target("dc01", "1"), _Target("sql01", "2")], direct=True, background=bg,
         refuse={"sql01": "nope"})
    assert len(bg.tasks) == 1, bg.tasks
    assert len(bg.tasks[0][1][0]) == 1, bg.tasks[0][1][0]


def test_the_walk_runs_the_ops_one_at_a_time_in_order():
    order = []

    def make(name, delay):
        async def _task():
            await asyncio.sleep(delay)
            order.append(name)
        return _task

    # Descending delays: anything concurrent finishes in the opposite order, so this
    # distinguishes a serial walk from a gather rather than merely observing one.
    asyncio.run(deps.run_power_batch(
        [make("first", 0.03), make("second", 0.02), make("third", 0.01)]))
    assert order == ["first", "second", "third"], order


def test_the_walk_does_not_swallow_a_task_that_raises():
    """`_run_power_op` turns its own failures into `set_failed`, so an exception
    escaping to here means something upstream of the job row broke. Hiding it would
    strand the remaining jobs at `pending` with nothing said."""
    async def boom():
        raise RuntimeError("kaboom")

    try:
        asyncio.run(deps.run_power_batch([boom]))
    except RuntimeError as exc:
        assert "kaboom" in str(exc)
    else:
        raise AssertionError("run_power_batch swallowed a task failure")


# ── The two ways a batch could leave job rows nothing will ever run ───────────

def test_a_crash_mid_batch_still_schedules_the_jobs_already_created():
    """A non-HTTPException is NOT swallowed — but it must not strand the earlier rows.

    By the time target 3 blows up, targets 1 and 2 already have committed job rows, and
    on the direct path those only ever run because something scheduled the walk. Letting
    the exception skip the scheduling would leave them `pending` until
    `reconcile_stale_jobs` failed them ~12 minutes later, having powered nothing: the
    operator watches two VMs go grey on the Jobs page for no reason they can see.
    """
    bg = _Background()
    calls = []

    async def queue_one(target, batch_id):
        if target.name == "boom":
            raise RuntimeError("the database went away")
        calls.append(target.name)

        async def _task():
            pass

        return {"job_id": f"job-{target.name}", "status": "queued", "task": _task}

    restore = _patch_audit(_AuditSpy())
    try:
        asyncio.run(deps.queue_power_batch(
            object(), kind="hyperv", op="start",
            targets=[_Target("dc01", "1"), _Target("sql01", "2"), _Target("boom", "3")],
            allowed_ops=ALLOWED, queue_one=queue_one, label_of=lambda t: t.name,
            created_by="alice", background_tasks=bg))
    except RuntimeError as exc:
        assert "database went away" in str(exc), exc
    else:
        raise AssertionError(
            "an unexpected exception was swallowed, so a part-queued batch would be "
            "reported as a success")
    finally:
        restore()

    assert calls == ["dc01", "sql01"], calls
    assert len(bg.tasks) == 1, (
        "the two committed job rows were left with nothing to execute them")
    assert len(bg.tasks[0][1][0]) == 2, bg.tasks[0][1][0]


def test_direct_tasks_with_nowhere_to_run_them_is_a_loud_error():
    """A router with a direct path that forgets to pass its BackgroundTasks.

    Without this the rows are all created and none of them run — the quietest possible
    failure, and from the Jobs page indistinguishable from an agent that never picked
    the work up. `api/vms.py` legitimately passes none, but it is agent-only and every
    target returns task=None, so it never reaches here.
    """
    restore = _patch_audit(_AuditSpy())
    try:
        _run([_Target("dc01", "1")], direct=True, background=None)
    except RuntimeError as exc:
        assert "no background_tasks" in str(exc), exc
        assert "nothing would execute them" in str(exc), exc
    else:
        raise AssertionError(
            "a direct batch with no background_tasks queued silently")
    finally:
        restore()


def test_an_agent_only_batch_needs_no_background_tasks():
    """The other side of the guard: every target agent-bound, so there is nothing to
    schedule and passing none is correct rather than an oversight."""
    result, _ = _run([_Target("dc01", "1"), _Target("sql01", "2")], direct=False,
                     background=None)
    assert result["count"] == 2, result


# ── The audit row ─────────────────────────────────────────────────────────────

def test_one_audit_row_per_batch_recording_what_ran_and_what_did_not():
    spy = _AuditSpy()
    result, _ = _run([_Target("dc01", "1"), _Target("sql01", "2"),
                      _Target("web01", "3")], op="stop",
                     refuse={"sql01": "nope"}, spy=spy)
    assert len(spy.calls) == 1, spy.calls
    call = spy.calls[0]
    assert call["username"] == "alice", call
    assert call["action"] == "hyperv_power_bulk", call["action"]
    details = call["details"]
    assert details["batch_id"] == result["batch_id"], details
    assert details["op"] == "stop", details
    assert details["count"] == 2, details
    assert details["targets"] == ["dc01", "web01"], details
    # The refused ones are on the row too. An audit trail that records only what
    # succeeded cannot answer "why did that VM not come up".
    assert details["failed"] == ["sql01"], details


def test_the_audit_count_is_the_jobs_actually_queued():
    spy = _AuditSpy()
    _run([_Target("dc01", "1"), _Target("sql01", "2")], refuse={"sql01": "nope"},
         spy=spy)
    assert spy.calls[0]["details"]["count"] == 1, spy.calls[0]["details"]


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
