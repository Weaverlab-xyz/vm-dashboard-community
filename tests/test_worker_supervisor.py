"""Behavioural tests for the job runner's concurrent supervisor.

tests/test_worker_tiers.py pins the SHAPE of _run_loop statically. This file pins what it
actually does, by driving the real loop against a fake queue and a fake dispatch and
watching which jobs overlap in time.

The headline tests are :func:`test_light_jobs_do_not_queue_behind_a_packer_build` and
:func:`test_two_heavy_jobs_overlap`. The first is the regression this whole change exists
for: a two-second expiry sweep used to wait behind a forty-minute image build because the
worker awaited each job. The second is why the heavy cap defaults to 2 rather than 1 —
without it, "concurrency" would mean the cheap jobs only, and provisioning two clusters
would still be strictly serial.

:func:`test_a_crashing_job_takes_down_neither_the_loop_nor_its_sibling` is the one that
matters if the others regress quietly: with the await gone, an exception escaping a task
surfaces only as a GC warning, so the backstop is the only thing standing between a bad
payload and a job stuck `running` until the ten-minute reconciler.

No database, no cloud: _claim_one, _dispatch, _heartbeat, _fail_backstop,
_reconcile_at_startup and _expire_heartbeats are all replaced, which is why they are named
module functions rather than inline blocks. Requires sqlalchemy only because jobs_worker
imports it at module level.

Run: python tests/test_worker_supervisor.py   (or under pytest)
"""
import asyncio
import os
import sys
import time

os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_worker_supervisor.db")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard import jobs_worker as jw                      # noqa: E402
from web_dashboard.logging_context import get_correlation_id     # noqa: E402
from web_dashboard.services import worker_policy                 # noqa: E402


class _Harness:
    """A fake queue + dispatch, and the timeline of what overlapped with what."""

    def __init__(self, queue, durations=None, crash=(), default_duration=0.06):
        # queue: [(job_id, job_type)] in created_at order, oldest first
        self.queue = list(queue)
        self.durations = durations or {}
        self.crash = set(crash)
        self.default_duration = default_duration
        self.active = {}                 # job_id -> job_type, currently dispatching
        self.timeline = []               # (job_id, job_type, t_enter, t_exit)
        self.max_overlap = 0
        self.max_overlap_by_tier = {"heavy": 0, "medium": 0, "light": 0}
        self.overlap_samples = []        # set of job_types live at each entry
        self.failed = []                 # (job_id, str(exc)) from the backstop
        self.correlations = {}           # job_id -> correlation id seen inside dispatch
        self.expired = []                # job ids handed to _expire_heartbeats
        self.claims = []                 # (job_id, allowed_len) in claim order
        self.t0 = time.monotonic()

    # ── fakes ────────────────────────────────────────────────────────────────

    def claim_one(self, db, allowed=jw.HANDLED_TYPES, max_attempts=10):
        """Honours `allowed` exactly the way the SQL does: oldest-first WITHIN it."""
        if not allowed:
            return None
        for i, (job_id, job_type) in enumerate(self.queue):
            if job_type in allowed:
                self.queue.pop(i)
                self.claims.append((job_id, len(allowed)))
                return (job_id, job_type, {})
        return None

    async def dispatch(self, job_id, job_type, meta):
        t_enter = time.monotonic() - self.t0
        self.active[job_id] = job_type
        self.correlations[job_id] = get_correlation_id()
        self.overlap_samples.append(set(self.active.values()))
        self.max_overlap = max(self.max_overlap, len(self.active))
        by_tier = {"heavy": 0, "medium": 0, "light": 0}
        for jt in self.active.values():
            by_tier[jw._TIER_OF[jt]] += 1
        for tier, n in by_tier.items():
            self.max_overlap_by_tier[tier] = max(self.max_overlap_by_tier[tier], n)
        try:
            if job_id in self.crash:
                raise RuntimeError("boom: bad payload")
            await asyncio.sleep(self.durations.get(job_type, self.default_duration))
        finally:
            self.active.pop(job_id, None)
            self.timeline.append((job_id, job_type, t_enter, time.monotonic() - self.t0))

    def fail_backstop(self, job_id, exc):
        self.failed.append((job_id, str(exc)))

    def expire_heartbeats(self, job_ids):
        self.expired.append(list(job_ids))

    # ── queries over the timeline ────────────────────────────────────────────

    def entry(self, job_id):
        for jid, _, t_enter, _ in self.timeline:
            if jid == job_id:
                return t_enter
        raise AssertionError(f"{job_id} never started; timeline={self.summary()}")

    def exit(self, job_id):
        for jid, _, _, t_exit in self.timeline:
            if jid == job_id:
                return t_exit
        raise AssertionError(f"{job_id} never finished; timeline={self.summary()}")

    def started(self):
        return {jid for jid, _, _, _ in self.timeline}

    def ran_while(self, job_id, other_id):
        """True if job_id started before other_id finished and vice versa."""
        return (self.entry(job_id) < self.exit(other_id)
                and self.entry(other_id) < self.exit(job_id))

    def summary(self):
        return [(j, t, round(a, 3), round(b, 3)) for j, t, a, b in self.timeline]


def _drive(harness, caps, poll_interval=0.02, capacity=0, timeout=15.0,
           stop_after=None, drain_timeout=0.0):
    """Run the REAL _run_loop against the harness until the queue drains, then stop it.

    Only the leaf functions are faked; _run_loop, _limits, _allowed_types, _idle, _run_job
    and _drain are the shipped ones, which is the point — the caps are enforced by
    _allowed_types feeding _claim_one an allowlist, and that interaction is what breaks.
    """
    saved = {name: getattr(jw, name) for name in
             ("_claim_one", "_dispatch", "_fail_backstop", "_reconcile_at_startup",
              "_heartbeat", "_expire_heartbeats", "SessionLocal", "pool_capacity",
              "_publish_if_changed")}
    saved_caps = worker_policy.caps
    saved_drain = worker_policy.drain_timeout_s

    async def _never_beat(job_id, interval=0):
        await asyncio.sleep(3600)

    class _FakeSession:
        def close(self):
            pass

    jw._claim_one = harness.claim_one
    jw._dispatch = harness.dispatch
    jw._fail_backstop = harness.fail_backstop
    jw._reconcile_at_startup = lambda: None
    jw._heartbeat = _never_beat
    jw._expire_heartbeats = harness.expire_heartbeats
    jw.SessionLocal = _FakeSession
    jw.pool_capacity = lambda: capacity
    jw._publish_if_changed = lambda limits, in_flight: None
    worker_policy.caps = lambda: dict(caps)
    worker_policy.drain_timeout_s = lambda: drain_timeout

    async def _go():
        # Owned per run, like _main does: an asyncio.Event binds to the loop that first
        # awaits it, so a shared one would work for exactly one test in this file.
        shutdown = asyncio.Event()
        loop_task = asyncio.create_task(jw._run_loop(poll_interval, shutdown=shutdown))

        async def _stopper():
            if stop_after is not None:
                await asyncio.sleep(stop_after)
                shutdown.set()
                return
            # Idle means: nothing queued and nothing dispatching, twice in a row.
            while True:
                await asyncio.sleep(poll_interval)
                if not harness.queue and not harness.active:
                    await asyncio.sleep(poll_interval * 3)
                    if not harness.queue and not harness.active:
                        shutdown.set()
                        return

        stopper = asyncio.create_task(_stopper())
        try:
            await asyncio.wait_for(loop_task, timeout=timeout)
        finally:
            stopper.cancel()
            try:
                await stopper
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_go())
    finally:
        for name, fn in saved.items():
            setattr(jw, name, fn)
        worker_policy.caps = saved_caps
        worker_policy.drain_timeout_s = saved_drain
        jw._last_published = None
    return harness


# ── the regression this exists for ───────────────────────────────────────────

def test_light_jobs_do_not_queue_behind_a_packer_build():
    """THE test. A Packer build is claimed first and runs for a long time; every cheap
    poll queued behind it must start while it is still in flight, not after it."""
    h = _Harness(
        queue=[("build", "packer_aws_build"),
               ("exp1", "aws_export_image"), ("exp2", "azure_export_image"),
               ("copy", "ami_copy")],
        durations={"packer_aws_build": 0.9}, default_duration=0.05)
    _drive(h, caps={"heavy": 2, "medium": 1, "light": 3, "total": 4})

    for jid in ("exp1", "exp2", "copy"):
        assert h.ran_while(jid, "build"), (
            f"{jid} did not overlap the packer build — the worker is still serial. "
            f"timeline={h.summary()}")


def test_two_heavy_jobs_overlap():
    """Why heavy defaults to 2: provisioning two clusters concurrently is the thing the
    operator actually asked for, and a cap of 1 would make it impossible."""
    h = _Harness(queue=[("k1", "k8s_provision"), ("k2", "clouddb_provision")],
                 default_duration=0.3)
    _drive(h, caps={"heavy": 2, "medium": 1, "light": 3, "total": 3})
    assert h.ran_while("k1", "k2"), f"heavy jobs ran serially; timeline={h.summary()}"


def test_the_heavy_cap_is_enforced():
    """Three heavy jobs, cap 2 — never three at once, and all three still run."""
    h = _Harness(queue=[("h1", "k8s_provision"), ("h2", "clouddb_provision"),
                        ("h3", "packer_gcp_build")], default_duration=0.2)
    _drive(h, caps={"heavy": 2, "medium": 1, "light": 3, "total": 6})
    assert h.max_overlap_by_tier["heavy"] == 2, (
        f"heavy overlap peaked at {h.max_overlap_by_tier['heavy']}, expected exactly 2; "
        f"timeline={h.summary()}")
    assert h.started() == {"h1", "h2", "h3"}


def test_the_aggregate_cap_binds_below_the_tier_caps():
    """The tier caps sum to 6 here but total is 2, so total is what holds. This is how the
    DB-pool clamp takes effect: it lowers `total` and nothing else."""
    h = _Harness(queue=[("a", "aws_export_image"), ("b", "ami_copy"),
                        ("c", "gce_capture_image"), ("d", "k8s_provision")],
                 default_duration=0.15)
    _drive(h, caps={"heavy": 2, "medium": 1, "light": 3, "total": 2})
    assert h.max_overlap <= 2, (
        f"ran {h.max_overlap} jobs at once against total=2; timeline={h.summary()}")
    assert len(h.started()) == 4, "not every job eventually ran"


# ── scheduling behaviour ─────────────────────────────────────────────────────

def test_a_newer_light_job_overtakes_an_older_pending_heavy_one():
    """Deliberate, and asserted so nobody "fixes" it back: with the heavy tier full, a
    light job created LATER is claimed first. created_at order is only guaranteed within a
    tier. Without this, one full tier would stall the whole queue again."""
    h = _Harness(queue=[("h1", "k8s_provision"), ("h2", "clouddb_provision"),
                        ("h3", "packer_oci_build"), ("light", "expiry_sweep")],
                 durations={"k8s_provision": 0.5, "clouddb_provision": 0.5,
                            "packer_oci_build": 0.2},
                 default_duration=0.05)
    _drive(h, caps={"heavy": 2, "medium": 1, "light": 3, "total": 4})
    assert h.entry("light") < h.entry("h3"), (
        "the newer light job did not overtake the third (unclaimable) heavy job; "
        f"timeline={h.summary()}")


def test_capacity_is_filled_in_one_pass_not_one_job_per_poll_tick():
    """After a successful claim the loop must go straight back round instead of sleeping,
    or a burst of ten queued exports would trickle in one per poll interval."""
    h = _Harness(queue=[("a", "aws_export_image"), ("b", "azure_export_image"),
                        ("c", "gcp_export_image")], default_duration=0.4)
    # A poll interval far larger than the work: if the loop slept between claims, the
    # third job could not start until ~1.0s.
    _drive(h, caps={"heavy": 1, "medium": 1, "light": 3, "total": 3}, poll_interval=0.5)
    spread = max(h.entry(j) for j in "abc") - min(h.entry(j) for j in "abc")
    assert spread < 0.25, (
        f"the three light jobs started {spread:.3f}s apart with a 0.5s poll interval — "
        f"the loop is sleeping between claims; timeline={h.summary()}")


def test_a_freed_slot_is_refilled_before_the_next_poll_tick():
    """_idle waits on FIRST_COMPLETED, so finishing a job wakes the loop immediately
    rather than leaving a slot idle for the rest of the poll interval."""
    h = _Harness(queue=[("first", "aws_export_image"), ("second", "ami_copy")],
                 default_duration=0.1)
    _drive(h, caps={"heavy": 1, "medium": 1, "light": 1, "total": 1}, poll_interval=1.0)
    gap = h.entry("second") - h.exit("first")
    assert gap < 0.5, (
        f"the freed slot sat idle {gap:.3f}s with a 1.0s poll interval — _idle is not "
        f"waking on FIRST_COMPLETED; timeline={h.summary()}")


def test_a_singleton_type_never_runs_twice_at_once():
    """expiry_sweep and the rancher/portainer node jobs write deployment-global state, so
    the tier cap is not enough — api/expiry.py can deliberately queue a second sweep."""
    h = _Harness(queue=[("s1", "expiry_sweep"), ("s2", "expiry_sweep"),
                        ("s3", "expiry_sweep")], default_duration=0.15)
    _drive(h, caps={"heavy": 1, "medium": 1, "light": 3, "total": 3})
    for live in h.overlap_samples:
        assert list(live).count("expiry_sweep") <= 1, "two expiry_sweeps ran concurrently"
    assert h.started() == {"s1", "s2", "s3"}, "the singletons did not all eventually run"


def test_two_different_singletons_still_overlap():
    """The constraint is per TYPE, not across the whole singleton set — otherwise a
    rancher teardown would block an unrelated expiry sweep."""
    h = _Harness(queue=[("sweep", "expiry_sweep"), ("node", "rancher_node_deploy")],
                 default_duration=0.25)
    _drive(h, caps={"heavy": 1, "medium": 2, "light": 3, "total": 4})
    assert h.ran_while("sweep", "node"), (
        f"different singleton types were serialized; timeline={h.summary()}")


# ── isolation ────────────────────────────────────────────────────────────────

def test_a_crashing_job_takes_down_neither_the_loop_nor_its_sibling():
    """With no await around the dispatch, a leaked exception is only a GC warning — so the
    backstop inside _run_job is the only thing that fails the row. Meanwhile the loop must
    keep claiming and the concurrent sibling must finish untouched."""
    h = _Harness(queue=[("bad", "aws_export_image"), ("good", "ami_copy"),
                        ("after", "gce_capture_image")],
                 durations={"ami_copy": 0.3}, default_duration=0.05, crash={"bad"})
    _drive(h, caps={"heavy": 1, "medium": 1, "light": 3, "total": 3})

    assert h.failed and h.failed[0][0] == "bad", (
        f"the crashed job was not handed to the backstop; failed={h.failed}")
    assert "boom: bad payload" in h.failed[0][1], "the backstop lost the error text"
    assert len(h.failed) == 1, f"the backstop fired more than once: {h.failed}"
    assert "good" in h.started() and h.exit("good") > h.entry("good")
    assert "after" in h.started(), "the loop stopped claiming after a job crashed"


def test_each_concurrent_job_gets_its_own_correlation_id():
    """contextvars are snapshotted per task, so entering correlation() inside _run_job
    gives each job its own id. In the loop body they would all report the same wrong one,
    which is worse than no correlation at all — the log would actively mislead."""
    h = _Harness(queue=[("j1", "aws_export_image"), ("j2", "ami_copy"),
                        ("j3", "gce_capture_image")], default_duration=0.2)
    _drive(h, caps={"heavy": 1, "medium": 1, "light": 3, "total": 3})
    assert h.correlations == {"j1": "j1", "j2": "j2", "j3": "j3"}, (
        f"correlation ids do not match their jobs: {h.correlations}")


# ── the pool clamp ───────────────────────────────────────────────────────────

def test_the_pool_clamp_lowers_the_aggregate_cap():
    """A capacity of 10 (5+5, the B1ms default) reserves 4 and allows 2 sessions per job,
    so 3 concurrent jobs — whatever the configured caps say."""
    saved = jw.pool_capacity
    saved_caps = worker_policy.caps
    try:
        jw.pool_capacity = lambda: 10
        worker_policy.caps = lambda: {"heavy": 4, "medium": 4, "light": 12, "total": 12}
        lim = jw._limits()
        assert lim["total"] == 3, f"expected a clamp to 3, got {lim['total']}"
        assert "DB_POOL_SIZE" in lim["clamp_reason"], (
            "the clamp reason must name the knob to raise — it is the only thing the "
            "operator sees when a cap they set does not take effect")
        # And an ample pool must not clamp at all.
        jw.pool_capacity = lambda: 64
        assert jw._limits()["total"] == 12
        assert jw._limits()["clamp_reason"] == ""
    finally:
        jw.pool_capacity = saved
        worker_policy.caps = saved_caps


def test_the_clamp_never_reaches_zero():
    """A tiny pool must still run one job at a time, not deadlock the queue."""
    saved = jw.pool_capacity
    try:
        jw.pool_capacity = lambda: 4
        assert jw._limits()["total"] >= 1
        jw.pool_capacity = lambda: 1
        assert jw._limits()["total"] >= 1
    finally:
        jw.pool_capacity = saved


def test_sqlite_is_not_clamped():
    """NullPool opens a fresh connection per session, so there is no ceiling to clamp to —
    pool_capacity returns 0 and the caps stand."""
    saved = jw.pool_capacity
    saved_caps = worker_policy.caps
    try:
        jw.pool_capacity = lambda: 0
        worker_policy.caps = lambda: {"heavy": 2, "medium": 1, "light": 3, "total": 6}
        lim = jw._limits()
        assert lim["total"] == 6 and lim["clamp_reason"] == ""
    finally:
        jw.pool_capacity = saved
        worker_policy.caps = saved_caps


# ── _allowed_types, directly ─────────────────────────────────────────────────

def test_allowed_types_is_empty_at_the_aggregate_cap():
    lim = {"heavy": 2, "medium": 1, "light": 3, "total": 2}
    running = {object(): ("light", "ami_copy"), object(): ("light", "aws_export_image")}
    assert jw._allowed_types(lim, running) == (), (
        "at the aggregate cap the claim must be skipped entirely, not issued and filtered")


def test_allowed_types_drops_only_the_full_tier():
    lim = {"heavy": 1, "medium": 1, "light": 3, "total": 5}
    running = {object(): ("heavy", "k8s_provision")}
    allowed = set(jw._allowed_types(lim, running))
    assert not (allowed & set(jw.HEAVY_TYPES)), "the full heavy tier is still claimable"
    assert set(jw.LIGHT_TYPES) <= allowed, "light jobs were locked out by a full heavy tier"
    assert set(jw.MEDIUM_TYPES) <= allowed


def test_allowed_types_excludes_a_live_singleton_but_not_its_tier():
    lim = {"heavy": 2, "medium": 1, "light": 3, "total": 5}
    running = {object(): ("light", "expiry_sweep")}
    allowed = set(jw._allowed_types(lim, running))
    assert "expiry_sweep" not in allowed
    assert "aws_export_image" in allowed, "a live singleton closed its whole tier"


# ── shutdown ─────────────────────────────────────────────────────────────────

def test_shutdown_stops_claiming_and_abandons_in_flight_work_to_the_reconciler():
    """A revision change SIGTERMs the worker. Anything still running is deliberately not
    requeued (the cloud side effect is already under way) — its heartbeat is backdated so
    the next process's startup reconcile fails it in seconds instead of ten minutes."""
    h = _Harness(queue=[("slow", "aws_export_image"), ("never", "ami_copy")],
                 durations={"aws_export_image": 5.0}, default_duration=5.0)
    # One slot, so "never" is still queued when shutdown lands mid-flight.
    _drive(h, caps={"heavy": 1, "medium": 1, "light": 1, "total": 1},
           poll_interval=0.02, stop_after=0.15, drain_timeout=0.05, timeout=10)

    assert "never" not in h.started(), "the loop kept claiming after shutdown was set"
    assert h.expired, "no heartbeat was backdated — the row would look alive for 10 min"
    assert any("slow" in batch for batch in h.expired), (
        f"the in-flight job was not abandoned to the reconciler; expired={h.expired}")
    assert len(h.expired) >= 2, (
        "the drain must backdate BEFORE the cancel as well as after, so a SIGKILL during "
        f"the cancel window still leaves the UPDATE committed; got {len(h.expired)} call(s)")


def test_a_job_that_finishes_inside_the_grace_period_is_not_abandoned():
    """The drain is a grace period, not a guillotine: work that completes in time must not
    be backdated into a failure."""
    h = _Harness(queue=[("quick", "expiry_sweep")], default_duration=0.1)
    _drive(h, caps={"heavy": 1, "medium": 1, "light": 1, "total": 1},
           poll_interval=0.02, stop_after=0.05, drain_timeout=2.0, timeout=10)
    assert "quick" in h.started()
    # The first backdate call is unconditional; what must NOT happen is a second one
    # after a clean finish.
    assert len(h.expired) <= 1, (
        f"a job that finished within the grace period was still abandoned: {h.expired}")


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
