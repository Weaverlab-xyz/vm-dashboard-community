"""Unit tests for the GCE ``instances.insert`` retry on the VM deploy path.

A live deploy failed on ``503 … Internal error. Please try again or contact
Google Support`` (us-east1-b, 2026-08-12) — Google's own advice was "try again",
but ``_launch_instance_sync`` inserted exactly once, so the blip failed the whole
job. Covered here:

  * ``gcp_service._is_transient_insert_error`` — server-side blips retry, and a
    zone stockout does NOT, even though it also arrives as a 503 (that one needs
    a different zone, not another attempt in the same one).
  * ``gcp_service._insert_instance_with_retry`` — retry/give-up/raise-immediately
    behaviour, plus the adopt path for an insert whose write landed with the
    response lost (retrying that name would 409 instead of returning the VM).

``google.api_core`` is faked so this runs offline. Runs under pytest or standalone:

    python tests/test_gce_launch_retry.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ── Fake google.api_core.exceptions (imported inside the retry helper) ────────────
class _NotFound(Exception):
    pass


_g = sys.modules.setdefault("google", types.ModuleType("google"))
_gac = types.ModuleType("google.api_core")
_gace = types.ModuleType("google.api_core.exceptions")
_gace.NotFound = _NotFound
sys.modules["google.api_core"] = _gac
sys.modules["google.api_core.exceptions"] = _gace

from web_dashboard.services import gcp_service  # noqa: E402

# Never actually sleep between attempts; keep monotonic real for the poll loop.
_fake_time = types.SimpleNamespace(sleep=lambda _s: None, monotonic=__import__("time").monotonic)
gcp_service.time = _fake_time


# ── Fakes ────────────────────────────────────────────────────────────────────────

class _FakeOp:
    def result(self, timeout=None):
        return None


class _FakeInstance:
    def __init__(self, status):
        self.status = status


class _FakeClient:
    """Records inserts; ``insert_errors`` is popped one per attempt (None = OK).
    ``instances`` maps name → status for the get() probe; missing raises NotFound."""

    def __init__(self, insert_errors=(), instances=None):
        self.insert_errors = list(insert_errors)
        self.instances = dict(instances or {})
        self.inserts = 0
        self.gets = 0

    def insert(self, project=None, zone=None, instance_resource=None):
        self.inserts += 1
        err = self.insert_errors.pop(0) if self.insert_errors else None
        if err is not None:
            raise err
        return _FakeOp()

    def get(self, project=None, zone=None, instance=None):
        self.gets += 1
        if instance not in self.instances:
            raise _NotFound(f"no instance {instance}")
        return _FakeInstance(self.instances[instance])


_INTERNAL_503 = Exception(
    "503 POST https://compute.googleapis.com/compute/v1/projects/p/zones/us-east1-b/"
    "instances: Internal error. Please try again or contact Google Support. "
    "(Code: '658D9B2609F7A.EE226.1E3B3E7B')")


def _run(client, **kw):
    return gcp_service._insert_instance_with_retry(
        client, "proj", "us-east1-b", "vm1", object(), delay=0, **kw)


# ── _is_transient_insert_error ───────────────────────────────────────────────────

def test_internal_error_is_transient():
    assert gcp_service._is_transient_insert_error(_INTERNAL_503)
    assert gcp_service._is_transient_insert_error(Exception("500 Internal Server Error"))
    assert gcp_service._is_transient_insert_error(Exception("backendError: try again"))
    assert gcp_service._is_transient_insert_error(Exception("Connection reset by peer"))


def test_capacity_error_is_not_transient():
    # Arrives as a 503 too, but another attempt in the SAME zone is pointless —
    # this one belongs to the zone-rotation ladder, not the retry loop.
    assert not gcp_service._is_transient_insert_error(Exception(
        "503 SERVICE UNAVAILABLE ZONE_RESOURCE_POOL_EXHAUSTED: the zone ..."))
    assert not gcp_service._is_transient_insert_error(Exception(
        "The zone 'us-east1-b' does not have enough resources"))


def test_real_failures_are_not_transient():
    assert not gcp_service._is_transient_insert_error(Exception("QUOTA_EXCEEDED: quota 'CPUS'"))
    assert not gcp_service._is_transient_insert_error(Exception(
        "Required 'compute.instances.create' permission"))
    assert not gcp_service._is_transient_insert_error(Exception(
        "409 The resource 'projects/p/zones/us-east1-b/instances/vm1' already exists"))
    assert not gcp_service._is_transient_insert_error(Exception("Invalid value for field 'subnetwork'"))


def test_http_status_is_read_off_the_exception_not_the_message():
    # api_core exceptions carry the status on .code, which is how a 5xx with an
    # unfamiliar message still counts…
    class _ApiError(Exception):
        code = 503

    assert gcp_service._is_transient_insert_error(_ApiError("something we've not seen"))

    # …and why the digits "503" merely appearing in the text do not.
    assert not gcp_service._is_transient_insert_error(Exception(
        "Quota 'CPUS' exceeded. Limit: 503.0 in region us-east1"))
    assert not gcp_service._is_transient_insert_error(Exception(
        "The resource 'projects/p/zones/us-east1-b/instances/app-503' was not found"))


# ── _should_try_next_zone (the gateway / Rancher / Portainer zone ladders) ───────

def test_next_zone_covers_both_capacity_and_blips():
    # Capacity was the original case; a 503 blip is worth a sibling zone too — it is
    # a different set of servers, and a gateway that fails takes every tunnel with it.
    assert gcp_service._should_try_next_zone(Exception("ZONE_RESOURCE_POOL_EXHAUSTED"))
    assert gcp_service._should_try_next_zone(_INTERNAL_503)


def test_next_zone_still_refuses_errors_that_fail_everywhere():
    assert not gcp_service._should_try_next_zone(Exception("QUOTA_EXCEEDED: quota 'CPUS'"))
    assert not gcp_service._should_try_next_zone(Exception(
        "Required 'compute.instances.create' permission"))
    assert not gcp_service._should_try_next_zone(Exception("Invalid value for field 'subnetwork'"))


# ── _insert_instance_with_retry ──────────────────────────────────────────────────

def test_happy_path_inserts_once():
    c = _FakeClient()
    _run(c)
    assert c.inserts == 1
    assert c.gets == 0          # no existence probe when nothing failed


def test_transient_failure_is_retried():
    c = _FakeClient(insert_errors=[_INTERNAL_503])
    _run(c)
    assert c.inserts == 2


def test_gives_up_after_attempts_and_reraises():
    c = _FakeClient(insert_errors=[_INTERNAL_503, _INTERNAL_503, _INTERNAL_503])
    try:
        _run(c, attempts=3)
    except Exception as e:
        assert "Internal error" in str(e)
    else:
        raise AssertionError("expected the last transient error to propagate")
    assert c.inserts == 3


def test_non_transient_failure_raises_immediately():
    boom = Exception("QUOTA_EXCEEDED: quota 'CPUS' exceeded")
    c = _FakeClient(insert_errors=[boom, None])
    try:
        _run(c)
    except Exception as e:
        assert "QUOTA_EXCEEDED" in str(e)
    else:
        raise AssertionError("expected a non-transient error to propagate")
    assert c.inserts == 1       # no retry burned on an error that won't fix itself


def test_adopts_instance_whose_insert_landed_with_the_response_lost():
    # The 503 came back but the VM exists — re-inserting that name would 409.
    c = _FakeClient(insert_errors=[_INTERNAL_503], instances={"vm1": "RUNNING"})
    _run(c)
    assert c.inserts == 1
    assert c.gets >= 1


def test_adopted_instance_waits_out_provisioning():
    class _Provisioning(_FakeClient):
        def get(self, project=None, zone=None, instance=None):
            self.gets += 1
            # PROVISIONING on the probe + first poll, then up.
            return _FakeInstance("PROVISIONING" if self.gets < 3 else "RUNNING")

    c = _Provisioning(insert_errors=[_INTERNAL_503])
    _run(c)
    assert c.inserts == 1
    assert c.gets == 3          # probe, still provisioning, RUNNING


def test_broken_existing_instance_raises_rather_than_reporting_success():
    c = _FakeClient(insert_errors=[_INTERNAL_503], instances={"vm1": "TERMINATED"})
    try:
        _run(c)
    except Exception as e:
        assert "Internal error" in str(e)
    else:
        raise AssertionError("a TERMINATED instance must not be adopted as a successful deploy")
    assert c.inserts == 1


def test_operation_timeout_raises_readable_and_does_not_reinsert():
    # The create is probably still running, so a re-insert would 409 — and a bare
    # TimeoutError has an empty message, which is what the job row would have shown.
    class _HangingOp:
        def result(self, timeout=None):
            raise TimeoutError()

    class _Hangs(_FakeClient):
        def insert(self, project=None, zone=None, instance_resource=None):
            self.inserts += 1
            return _HangingOp()

    c = _Hangs()
    try:
        _run(c)
    except gcp_service.GCPError as e:
        assert "vm1" in str(e) and "us-east1-b" in str(e)
    else:
        raise AssertionError("expected a GCPError carrying the instance and zone")
    assert c.inserts == 1


def test_probe_failure_falls_through_to_a_retry():
    class _BadGet(_FakeClient):
        def get(self, project=None, zone=None, instance=None):
            self.gets += 1
            raise RuntimeError("compute API unreachable")

    c = _BadGet(insert_errors=[_INTERNAL_503])
    _run(c)
    assert c.inserts == 2       # probe error must not abort the retry


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    _failures = 0
    for _t in _tests:
        try:
            _t()
            print(f"PASS {_t.__name__}")
        except Exception as _e:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_t.__name__}: {_e!r}")
    print(f"\n{len(_tests) - _failures}/{len(_tests)} passed")
    sys.exit(1 if _failures else 0)
