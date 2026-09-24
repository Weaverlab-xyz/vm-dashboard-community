"""A booked power batch must not be executed the moment it is booked.

`queue_power_batch` collects a `task` from each target and hands it to
`BackgroundTasks` — work THIS process runs, now. On the four cloud routers that value
is always None: a `*_power` row is claimed by the jobs worker, and the change-window
clause on that claim is the entire mechanism by which a booking means anything.

An on-premises DIRECT connection is the opposite — its `_queue_one` returns a real
coroutine. A router that wired scheduling onto that path would create a job row booked
for Saturday and power the VM off immediately, and the row would still *read* as
scheduled afterwards. There is no error, no log line, and the job page agrees with the
operator's intent while the machine is already off.

So `queue_power_batch` refuses that combination outright, in the same "programming
error, said out loud" style the module already uses for a forgotten `background_tasks`.
These two tests are what keep that honest in both directions.

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


def test_the_cloud_routers_are_the_only_ones_offering_the_control():
    """The toolbar control must appear only where the route accepts a booking.

    The on-premises bulk-power models do not carry the scheduling fields, and pydantic
    ignores unknown ones rather than erroring — so a control on those pages would
    render a booking that silently never happened.
    """
    import re
    tpl = os.path.join(_ROOT, "web_dashboard", "templates")
    offering, expected = set(), {"aws", "azure", "gcp", "oci"}
    for root, _dirs, files in os.walk(tpl):
        for name in files:
            if not name.endswith(".html"):
                continue
            src = open(os.path.join(root, name), encoding="utf-8").read()
            if re.search(r"bulk_power_buttons\([^)]*schedulable\s*=\s*true", src, re.S):
                offering.add(os.path.basename(root))
    assert offering == expected, (
        f"pages offering a bulk-power booking: {sorted(offering)}; "
        f"expected exactly {sorted(expected)} — the routes that accept one")


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
