"""A booked power batch must not be executed the moment it is booked.

`queue_power_batch` collects a `task` from each target and hands it to
`BackgroundTasks` — work THIS process runs, now. On the four cloud routers that value
is always None: a `*_power` row is claimed by the jobs worker, and the change-window
clause on that claim is the entire mechanism by which a booking means anything.

An on-premises connection splits in two, and the split is the whole subject here:

* **Agent-bound** — `_queue_one` returns `task: None` and an `agent_hypervisor` row.
  `agent_service.lease_one` filters on `job_service.claimable_now()`, the same
  predicate the local worker claims through, so the booking is honoured by the lease.
  These pages may offer the control.
* **Directly dialled** — `_queue_one` returns a real coroutine. A router that wired
  scheduling onto that path would create a job row booked for Saturday and power the
  VM off immediately, and the row would still *read* as scheduled afterwards. No
  error, no log line, and the job page agrees with the operator's intent while the
  machine is already off.

Two defences, at different levels. Each router refuses the direct combination per
target in `_queue_one`, before any job row exists, so a mixed selection still books the
half that can be booked. `queue_power_batch` keeps its RuntimeError as the backstop for
a router that forgets — deliberately a "programming error, said out loud", because
reaching it aborts the batch.

These tests keep that honest in both directions, and keep the toolbar control and the
routes behind it from drifting apart.

Run: python tests/test_bulk_power_schedule.py   (or under pytest)
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-bulk-power-schedule")
os.environ["DATABASE_URL"] = "sqlite://"

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"fastapi unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.api import power_batch  # noqa: E402


class _Bag:
    """Stands in for FastAPI's BackgroundTasks."""

    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **k):
        self.tasks.append((fn, a, k))


def _queue_one_with_task(ran):
    async def queue_one(target, batch_id):
        async def _work():
            ran.append(target)
        return {"job_id": f"job-{target}", "status": "pending", "task": _work}
    return queue_one


async def _queue_one_worker_claimed(target, batch_id):
    """What every cloud router returns: a row the jobs worker will claim."""
    return {"job_id": f"job-{target}", "status": "pending", "task": None}


def _run_batch(queue_one, *, scheduled=False, bag=None):
    # A real session only so the batch's closing audit row can be written; none of
    # these tests assert on it.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from web_dashboard.database import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    async def drive():
        return await power_batch.queue_power_batch(
            db, kind="hyperv", op="stop", targets=["vm1"],
            allowed_ops=("stop",), queue_one=queue_one,
            label_of=lambda t: str(t), created_by="tester",
            background_tasks=bag if bag is not None else _Bag(),
            scheduled=scheduled)
    return asyncio.run(drive())


def test_a_scheduled_batch_refuses_a_direct_task():
    """The guard. Without it the booked work runs immediately and nothing says so."""
    ran = []
    try:
        _run_batch(_queue_one_with_task(ran), scheduled=True)
    except RuntimeError as exc:
        assert "run NOW" in str(exc), str(exc)
        assert ran == [], "the booked work ran before the guard fired"
        return
    raise AssertionError("a scheduled batch accepted a direct in-process task")


def test_an_unscheduled_batch_still_runs_its_direct_task():
    """The other direction, and the regression that matters: the guard must not touch
    the on-premises pages, whose ordinary path is exactly a direct task."""
    ran = []
    bag = _Bag()
    out = _run_batch(_queue_one_with_task(ran), scheduled=False, bag=bag)
    assert out["count"] == 1, out
    assert bag.tasks, "the direct task was never scheduled to run"


def test_a_scheduled_batch_of_worker_claimed_jobs_is_fine():
    """What the cloud routers actually do — every target is a queue row, so there is
    nothing for this process to run and the booking is honoured by the claim query."""
    bag = _Bag()
    out = _run_batch(_queue_one_worker_claimed, scheduled=True, bag=bag)
    assert out["count"] == 1, out
    assert bag.tasks == [], "a worker-claimed batch scheduled in-process work"


#: Page directory -> the router module whose bulk-power route it posts to.
_PAGE_ROUTER = {
    "aws": "aws", "azure": "azure", "gcp": "gcp", "oci": "oci",
    "hyperv": "hyperv", "proxmox": "proxmox", "vsphere": "vsphere",
    "xcpng": "xcpng", "vms": "vms", "nutanix": "nutanix",
}

#: The one page that must never offer the control. Nutanix has no agent power path at
#: all — see the standing decision in api/nutanix.py::_queue_one — so every target is
#: dialled directly by this process and nothing on the page can be booked.
_NEVER_SCHEDULABLE = {"nutanix"}


def _pages_offering_the_control():
    import re
    tpl = os.path.join(_ROOT, "web_dashboard", "templates")
    offering = set()
    for root, _dirs, files in os.walk(tpl):
        for name in files:
            if not name.endswith(".html"):
                continue
            src = open(os.path.join(root, name), encoding="utf-8").read()
            if re.search(r"bulk_power_buttons\([^)]*schedulable\s*=\s*true", src, re.S):
                offering.add(os.path.basename(root))
    return offering


def test_every_page_offering_the_control_posts_to_a_route_that_accepts_one():
    """The toolbar control must appear only where the route accepts a booking.

    A model that does not carry the scheduling fields makes the control a lie, because
    pydantic ignores unknown fields rather than erroring — the page would render a
    booking that silently never happened. Asserted as a RULE rather than a fixed list:
    the list was `{aws, azure, gcp, oci}` until the on-premises agent path was wired,
    and a hard-coded set turns "this page grew the capability" into a test failure that
    reads like a regression.
    """
    import re
    api = os.path.join(_ROOT, "web_dashboard", "api")
    bad = []
    for page in sorted(_pages_offering_the_control()):
        module = _PAGE_ROUTER.get(page)
        assert module, f"{page}/ offers the control but is not in _PAGE_ROUTER"
        src = open(os.path.join(api, f"{module}.py"), encoding="utf-8").read()
        if "class BulkPowerRequest(ScheduleRequestMixin" not in src:
            bad.append(f"{page}: api/{module}.py BulkPowerRequest has no schedule fields")
        elif not re.search(r"_sched\s*=\s*change_window_service\.schedule_kwargs", src):
            bad.append(f"{page}: api/{module}.py bulk_power never resolves the booking")
        elif "scheduled=bool(_sched)" not in src:
            bad.append(f"{page}: api/{module}.py does not tell queue_power_batch it is booked")
    assert not bad, "toolbar controls with no route behind them:\n  " + "\n  ".join(bad)


def test_a_page_whose_power_ops_cannot_queue_never_offers_the_control():
    """The inverse, and the one that needs naming rather than deriving.

    Booking requires the work to WAIT somewhere — a `*_power` row for the jobs worker,
    or an `agent_hypervisor` row for the agent's lease. A router with no such path runs
    every op in this process, so a control on its page would refuse every target.
    """
    offending = _pages_offering_the_control() & _NEVER_SCHEDULABLE
    assert not offending, (
        f"{sorted(offending)} offers a bulk-power booking, but every one of its "
        f"targets is dialled directly by this process and would be refused")


def test_the_on_premises_direct_path_refuses_a_booking_before_creating_a_job():
    """Each on-premises router that CAN dial a connection itself must refuse a booked
    request on that path, and do it before `create_job`.

    `queue_power_batch`'s RuntimeError is the backstop for a router that forgets this;
    it is not the intended route, because reaching it aborts the whole batch. A mixed
    selection has to leave the agent-bound targets queued.
    """
    api = os.path.join(_ROOT, "web_dashboard", "api")
    bad = []
    for module in ("hyperv", "proxmox", "vsphere", "xcpng"):
        src = open(os.path.join(api, f"{module}.py"), encoding="utf-8").read()
        if "refuse_direct_booking(conn, op)" not in src:
            bad.append(f"api/{module}.py never refuses a booking on its direct path")
            continue
        # Before create_job, not after: a refusal must leave nothing on /jobs.
        refusal = src.index("refuse_direct_booking(conn, op)")
        after = src.index('job_type=f"', refusal) if 'job_type=f"' in src[refusal:] else -1
        if after == -1:
            bad.append(f"api/{module}.py refuses after its direct create_job, not before")
    assert not bad, "\n  ".join(bad)


def _run():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
