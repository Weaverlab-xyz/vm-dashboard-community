"""Unit tests for services/cloud_executor.py — the bounded per-provider thread pools.

The headline test is :func:`test_a_saturated_provider_cannot_block_another_provider`.
It pins the exact property whose absence took dash.weaverlab.app down for 30 minutes on
2026-08-12: every blocking cloud call shared the event loop's default executor, which in
that container was 8 threads, and the home page fans out ~8 per-cloud reads. GCP and OCI
went slow upstream, parked all 8, and routes that touch no cloud at all queued behind
them forever. If that test ever fails, the outage is back.

:func:`test_a_timed_out_call_keeps_its_slot_until_the_thread_really_finishes` is the
subtle one. Python cannot interrupt a blocking socket read, so a timeout frees the caller
but NOT the thread. Occupancy is therefore tracked on the underlying
``concurrent.futures.Future``, not the asyncio wrapper — count the wrapper and we would
re-admit work into a pool with no free threads and rebuild the unbounded queue this
module exists to prevent.

:func:`test_the_worker_deadline_outlives_the_longest_poller` is the boring one that will
actually catch a regression: a GCP image export runs to 7200s inside ONE to_thread call,
so a worker default below that would kill legitimate work.

Pure Python: ``config_service`` and ``config.settings`` are stubbed, no DB, no cloud SDK.
Runs under pytest, or standalone:
    python tests/test_cloud_executor.py
"""
import asyncio
import importlib.util
import os
import re
import sys
import threading
import time
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _install_stubs():
    """Stub parent packages so cloud_executor's relative imports resolve while it is
    loaded by file path — mirrors test_worker_policy._install_stubs, and keeps this
    runnable on a bare checkout with no SDKs installed."""
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = []
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg

    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = conf_mod


_install_stubs()
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "cloud_executor.py")


def _fresh(**conf):
    """A cloud_executor with virgin pool state. Pools are created once per provider and
    never resized by design, so every test that cares about sizing needs its own module
    instance rather than a shared one."""
    CONF.clear()
    CONF.update({k: str(v) for k, v in conf.items()})
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.cloud_executor", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── helpers ──────────────────────────────────────────────────────────────────

def _blocker(running: threading.Semaphore, release: threading.Event):
    """A call that parks its thread until released — a slow cloud API, in one function."""
    def fn():
        running.release()
        release.wait(10)
        return "finished"
    return fn


async def _await_running(running: threading.Semaphore, count: int, budget=5.0):
    """Yield to the loop until ``count`` blockers are genuinely on a thread.

    Must not block: these calls are submitted by coroutines that have not been scheduled
    yet, so a blocking acquire here would deadlock the very tasks it is waiting for.
    """
    got, deadline = 0, time.monotonic() + budget
    while got < count and time.monotonic() < deadline:
        if running.acquire(blocking=False):
            got += 1
        else:
            await asyncio.sleep(0.01)
    assert got == count, f"only {got}/{count} blockers reached a thread"


# ── The headline property ────────────────────────────────────────────────────

def test_a_saturated_provider_cannot_block_another_provider():
    ce = _fresh(cloud_pool_size=3)
    running, release = threading.Semaphore(0), threading.Event()

    async def scenario():
        size = ce.pool_size()
        hogs = [asyncio.ensure_future(ce.run("gcp", _blocker(running, release),
                                             deadline=10))
                for _ in range(size)]
        # Wait until every GCP thread is genuinely occupied.
        await _await_running(running, size)

        # GCP is now saturated. AWS must be completely unaffected.
        began = time.monotonic()
        assert await ce.run("aws", lambda: "aws-ok", deadline=5) == "aws-ok"
        assert time.monotonic() - began < 2.0, "AWS queued behind GCP — pools are shared"

        # And a further GCP call is REFUSED, not queued behind the hogs.
        began = time.monotonic()
        try:
            await ce.run("gcp", lambda: "never", deadline=10)
            raise AssertionError("saturated GCP accepted more work instead of refusing")
        except ce.CloudProviderBusy as exc:
            assert exc.provider == "gcp"
        assert time.monotonic() - began < 2.0, "refusal was not immediate"

        release.set()
        await asyncio.gather(*hogs)

    asyncio.run(scenario())
    assert ce.stats()["gcp"]["busy"] == 0, "threads not returned after the calls finished"


def test_a_provider_recovers_once_its_calls_return():
    """Saturation must be a phase, not a state: the pool has to take work again."""
    ce = _fresh(cloud_pool_size=2)
    running, release = threading.Semaphore(0), threading.Event()

    async def scenario():
        hogs = [asyncio.ensure_future(ce.run("gcp", _blocker(running, release),
                                             deadline=10))
                for _ in range(2)]
        await _await_running(running, 2)
        release.set()
        await asyncio.gather(*hogs)
        assert await ce.run("gcp", lambda: "ok", deadline=5) == "ok"

    asyncio.run(scenario())


# ── Deadlines ────────────────────────────────────────────────────────────────

def test_a_call_that_never_returns_raises_rather_than_hanging():
    ce = _fresh(cloud_pool_size=2)
    release = threading.Event()

    async def scenario():
        try:
            await ce.run("oci", lambda: release.wait(10), deadline=0.2)
            raise AssertionError("a call past its deadline returned normally")
        except ce.CloudCallTimeout as exc:
            assert exc.provider == "oci"
            assert exc.budget == 0.2
        finally:
            release.set()

    asyncio.run(scenario())


def test_a_timed_out_call_keeps_its_slot_until_the_thread_really_finishes():
    """Occupancy follows the THREAD, not the awaiting caller.

    asyncio.wait_for cancels its wrapper; the OS thread keeps running the blocking SDK
    call regardless. Releasing the slot on cancellation would let the pool admit work it
    has no thread for — which is precisely the unbounded queue that caused the outage.
    """
    # cloud_pool_size=1 would be raised to POOL_SIZE_FLOOR, so fill whatever the floor is.
    ce = _fresh(cloud_pool_size=1)
    release = threading.Event()

    async def scenario():
        size = ce.pool_size()
        for _ in range(size):
            try:
                await ce.run("gcp", lambda: release.wait(10), deadline=0.2)
            except ce.CloudCallTimeout:
                pass
        # Every caller gave up, but the threads are all still inside their calls.
        assert ce.stats()["gcp"]["busy"] == size, "slot freed while the thread still runs"
        try:
            await ce.run("gcp", lambda: "never", deadline=5)
            raise AssertionError("admitted work into a pool with no free thread")
        except ce.CloudProviderBusy:
            pass

        release.set()
        for _ in range(100):                     # the callback runs on the worker thread
            if ce.stats()["gcp"]["busy"] == 0:
                break
            await asyncio.sleep(0.02)
        assert ce.stats()["gcp"]["busy"] == 0, "slot never returned after the call ended"

    asyncio.run(scenario())


def test_a_timeout_kwarg_reaches_the_sdk_call_untouched():
    """``timeout`` is one of the commonest kwargs in a cloud SDK, and every _to_thread
    shim forwards **kwargs straight through. If the executor's own knob were named
    ``timeout`` it would silently eat one meant for the SDK and apply it as the deadline
    — which reads as "the SDK ignored my timeout" and is very hard to see. It is named
    ``deadline`` precisely so this test can pass."""
    ce = _fresh()
    seen = {}

    def sdk_call(timeout=None):
        seen["timeout"] = timeout
        return "ok"

    async def scenario():
        assert await ce.run("aws", sdk_call, timeout=17, deadline=5) == "ok"

    asyncio.run(scenario())
    assert seen["timeout"] == 17, "the executor swallowed a kwarg meant for the SDK"


def test_the_error_from_the_call_itself_is_not_swallowed():
    """Only the executor's own refusals are translated; an SDK error must pass through
    untouched or every existing except-clause in the services would stop matching."""
    ce = _fresh()

    class Boom(Exception):
        pass

    def explode():
        raise Boom("the API said no")

    async def scenario():
        try:
            await ce.run("aws", explode, deadline=5)
            raise AssertionError("the call's own exception vanished")
        except Boom as exc:
            assert "the API said no" in str(exc)

    asyncio.run(scenario())
    assert ce.stats()["aws"]["busy"] == 0, "a failed call leaked its slot"


def test_the_callers_contextvars_are_visible_inside_the_pool_thread():
    """asyncio.to_thread copies the caller's context into its thread; this executor
    replaced asyncio.to_thread at every cloud call site (pinned below), so it must keep
    that property. It is not a nicety: the job runner marks provisioning jobs with a
    contextvar (workload_credential_lease.provisioning) and the AWS credential lookup
    reads it INSIDE the pool thread. With a bare pool.submit the thread starts on a
    fresh context, the marker is invisible, and a deploy is silently served the
    read-only everyday lease — the live ec2_deploy failure of 2026-08-25
    (iam:PassRole: explicit deny in a session policy)."""
    import contextvars
    ce = _fresh()
    var = contextvars.ContextVar("wlc_purpose_probe", default="outside")

    async def scenario():
        token = var.set("inside-the-job")
        try:
            assert await ce.run("aws", var.get, deadline=5) == "inside-the-job", \
                "the pool thread did not see the caller's contextvars"
            # And it must be a COPY: a set made inside the thread may not leak back.
            await ce.run("aws", lambda: var.set("leaked"), deadline=5)
            assert var.get() == "inside-the-job", \
                "the pool thread mutated the caller's own context"
        finally:
            var.reset(token)

    asyncio.run(scenario())


# ── Bounds a misconfiguration cannot defeat ──────────────────────────────────

def test_pool_size_has_a_floor_and_a_ceiling():
    assert _fresh(cloud_pool_size=0).pool_size() == _fresh().POOL_SIZE_FLOOR
    assert _fresh(cloud_pool_size=9999).pool_size() == _fresh().POOL_SIZE_CEILING
    assert _fresh(cloud_pool_size="banana").pool_size() == _fresh()._DEFAULT_POOL_SIZE


def test_no_call_can_be_configured_to_wait_forever():
    """0 or a negative deadline reads like "no limit", which is the bug being fixed."""
    ce = _fresh(cloud_call_timeout_s=0)
    assert ce.call_timeout() == ce.CALL_TIMEOUT_FLOOR_S
    assert _fresh(cloud_call_timeout_s=-1).call_timeout() == ce.CALL_TIMEOUT_FLOOR_S
    assert _fresh(cloud_call_timeout_s=10 ** 9).call_timeout() == ce.CALL_TIMEOUT_CEILING_S


def test_the_worker_deadline_outlives_the_longest_poller():
    """A GCP image export runs to 7200s inside a single blocking call (see
    jobs_worker._executor_size). A worker default under that would fail live work."""
    ce = _fresh()
    ce.use_worker_defaults()
    assert ce.call_timeout() > 7200, "worker deadline would kill a running image export"
    # ...and the request path stays short, or a wedged tile hangs the page again.
    assert _fresh().call_timeout() <= 120


def test_request_and_worker_paths_read_different_keys():
    """Same image, same imports, two processes with opposite needs — so the deadline
    cannot be one shared key that is wrong for one of them."""
    ce = _fresh(cloud_call_timeout_s=30, cloud_worker_call_timeout_s=9000)
    assert ce.call_timeout() == 30
    ce.use_worker_defaults()
    assert ce.call_timeout() == 9000


# ── The services actually route through it ───────────────────────────────────

_SERVICES = {
    # The four hyperscalers, covered since the outage that produced this module.
    "gcp_service.py": ("gcp", "GCPError"),
    "oci_service.py": ("oci", "OCIError"),
    "aws_service.py": ("aws", "AWSError"),
    "azure_service.py": ("azure", "AzureError"),
    # Everything else the dashboard home page fans out to. These were left on the shared
    # default executor with NO deadline, which meant the property this module claims —
    # "one provider's bad day cannot reach the others" — held for only 4 of 12 providers.
    # A slow Proxmox host or an unreachable storage backend could still queue up every
    # thread in the process and take unrelated routes down with it.
    "proxmox_service.py": ("proxmox", "ProxmoxError"),
    "nutanix_service.py": ("nutanix", "NutanixError"),
    "vsphere_service.py": ("vsphere", "VSphereError"),
    "hyperv_service.py": ("hyperv", "HyperVError"),
    "xcpng_service.py": ("xcpng", "XcpNgError"),
    "k8s_service.py": ("k8s", "K8sError"),
    "storage_service.py": ("storage", "StorageError"),
    "cloud_database_service.py": ("clouddb", "CloudDatabaseError"),
}


def _read(name):
    with open(os.path.join(_ROOT, "web_dashboard", "services", name),
              encoding="utf-8") as fh:
        return fh.read()


def test_every_cloud_service_routes_through_its_own_pool():
    """Read as source, not imported: these modules need the real SDKs at call time, and
    the point is that no call site is left on the shared default executor."""
    for name, (provider, error) in _SERVICES.items():
        src = _read(name)
        assert "async def _to_thread" in src, f"{name} has no _to_thread shim"
        assert f'cloud_executor.run("{provider}"' in src, \
            f"{name} does not submit to the {provider} pool"
        assert re.search(rf"except cloud_executor\.CloudCallError.*?\n\s*raise {error}",
                         src, re.S), \
            f"{name} does not translate executor refusals into {error}"
        assert "asyncio.to_thread" not in src, \
            (f"{name} still has a call on the SHARED default executor — one of these is "
             f"enough to reproduce the outage")


def test_the_worker_switches_to_job_path_deadlines():
    with open(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "cloud_executor.use_worker_defaults()" in src, \
        "the worker never opts into job-path deadlines; a 60s cap would kill long jobs"


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as exc:                       # noqa: BLE001
                failed += 1
                print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
    print("OK" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
