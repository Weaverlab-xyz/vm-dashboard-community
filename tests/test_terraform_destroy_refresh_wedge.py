"""Unit tests for terraform.destroy's ``-refresh=false`` retry.

`terraform destroy` refreshes before it plans, so a provider READ that can never
succeed aborts the run before anything is deleted. The live case: the google
provider saves a GKE cluster's in-flight operation in state and resumes waiting on
it on every read, so a cluster whose CREATE died on a zonal stockout failed EVERY
teardown with "Error waiting for resuming GKE cluster: … [GCE_STOCKOUT] …" and the
cluster + its VPC/NAT stayed orphaned. State decides what gets destroyed, so the
destroy is retried once with -refresh=false.

This pins: the wedge is recognized (and a normal failure is NOT), and both the
streamed and non-streamed destroy paths retry exactly once with -refresh=false.
Terraform itself is never invoked — `_stream` / `_run` are stubbed.
Runs under pytest, or standalone:  python tests/test_terraform_destroy_refresh_wedge.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The real destroy output that wedged a GKE teardown (trimmed).
WEDGE_OUT = """google_container_cluster.this: Refreshing state... [id=projects/p/locations/us-central1-c/clusters/k8s-test]

Error: Error waiting for resuming GKE cluster: Google Compute Engine: Not all instances running in IGM after 35m6.9s. Expected 1, running 0, transitioning 1. Current errors: [GCE_STOCKOUT]: Instance 'gke-k8s-test-default-pool-934f544e-10vf' creation failed: The zone 'projects/p/zones/us-central1-c' does not have enough resources available to fulfill the request.
"""
REAL_FAILURE_OUT = """Error: Error when reading or editing Subnetwork: googleapi: Error 400: The subnetwork resource
is already being used by 'k8s-test-nat', resourceInUseByAnotherResource
"""


class _Settings:
    """Stand-in for the pydantic Settings: any unknown key resolves to ""."""
    terraform_executable = "terraform"

    def __getattr__(self, _key):
        return ""


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    # destroy() resolves the state backend before running: keep it local so no
    # bucket/credential config is needed and backend.tf is simply removed.
    st = types.ModuleType("web_dashboard.services.storage_service")
    st.active_backend = lambda: "local"
    sys.modules["web_dashboard.services.storage_service"] = st

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    sys.modules["web_dashboard.services.config_service"] = cfg


_install_stubs()
try:
    from web_dashboard.services import terraform as tf
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"terraform service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _deploy_dir(tmp: str) -> str:
    """A deploy dir that already holds a module, so destroy skips _materialize."""
    with open(os.path.join(tmp, "main.tf"), "w") as fh:
        fh.write("# test module\n")
    return tmp


# ── the wedge predicate ───────────────────────────────────────────────────────

def test_recognizes_the_gke_resume_wedge():
    assert tf._is_refresh_wedge(WEDGE_OUT)
    assert tf._is_refresh_wedge(WEDGE_OUT.upper())  # matching is case-insensitive


def test_ignores_a_real_delete_failure():
    # A resource that genuinely won't delete must NOT be retried with a stale
    # refresh — -refresh=false would fail identically and hide the cause.
    assert not tf._is_refresh_wedge(REAL_FAILURE_OUT)
    assert not tf._is_refresh_wedge("")


def test_destroy_args_only_adds_the_flag_when_asked():
    assert "-refresh=false" not in tf._destroy_args(["-var", "x=1"])
    assert tf._destroy_args(["-var", "x=1"])[-2:] == ["-var", "x=1"]
    assert "-refresh=false" in tf._destroy_args(None, refresh=False)


# ── streamed path (the job runner's path: on_line → Live Output) ──────────────

def _run_streamed(returncodes, outputs):
    """Drive destroy() with a stubbed _stream; returns (arg lists, streamed lines)."""
    calls, lines = [], []
    seq = list(zip(returncodes, outputs))

    async def fake_stream(tf_args, cwd, env, on_line):
        calls.append(list(tf_args))
        return seq[len(calls) - 1]

    async def on_line(line):
        lines.append(line)

    orig_stream, orig_init = tf._stream, tf._init_sync
    tf._stream = fake_stream
    tf._init_sync = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            err = None
            try:
                asyncio.run(tf.destroy(_deploy_dir(tmp), variables={"region": "us-central1"},
                                       on_line=on_line))
            except tf.TerraformError as exc:
                err = exc
    finally:
        tf._stream, tf._init_sync = orig_stream, orig_init
    return calls, lines, err


def test_streamed_wedge_retries_once_without_refresh():
    calls, lines, err = _run_streamed([1, 0], [WEDGE_OUT, "Destroy complete! 7 destroyed."])
    assert err is None, f"the retry succeeded, so destroy must not raise: {err}"
    assert len(calls) == 2, f"expected exactly one retry, got {len(calls)} destroy runs"
    assert "-refresh=false" not in calls[0]  # first pass keeps drift detection
    assert "-refresh=false" in calls[1]
    assert calls[1][:4] == ["destroy", "-auto-approve", "-no-color", "-input=false"]
    assert calls[1][-2:] == ["-var", "region=us-central1"]  # same -var set
    assert any("-refresh=false" in ln for ln in lines), "operator gets told why in Live Output"


def test_streamed_real_failure_is_not_retried():
    calls, _lines, err = _run_streamed([1, 0], [REAL_FAILURE_OUT, ""])
    assert len(calls) == 1, "a genuine delete failure must not be retried"
    assert err is not None and "terraform destroy failed" in str(err)


def test_streamed_wedge_that_persists_still_raises_with_both_outputs():
    calls, _lines, err = _run_streamed([1, 1], [WEDGE_OUT, "Error: still broken"])
    assert len(calls) == 2
    assert err is not None
    assert "resuming GKE cluster" in str(err) and "still broken" in str(err), \
        "the failure must carry BOTH attempts' output, not just the retry's"


# ── non-streamed path (callers that pass no on_line) ──────────────────────────

def _run_sync(returncodes, outputs):
    """Drive destroy() with no on_line and a stubbed _run; returns the arg lists."""
    calls = []
    seq = list(zip(returncodes, outputs))

    def fake_run(cmd, cwd, timeout=600, env=None):
        if cmd and cmd[0] == "init":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        calls.append(list(cmd))
        rc, out = seq[len(calls) - 1]
        return subprocess.CompletedProcess(cmd, rc, out, "")

    orig_run = tf._run
    tf._run = fake_run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            err = None
            try:
                asyncio.run(tf.destroy(_deploy_dir(tmp), variables={"region": "us-central1"}))
            except tf.TerraformError as exc:
                err = exc
    finally:
        tf._run = orig_run
    return calls, err


def test_sync_wedge_retries_once_without_refresh():
    calls, err = _run_sync([1, 0], [WEDGE_OUT, ""])
    assert err is None, f"the retry succeeded, so destroy must not raise: {err}"
    assert len(calls) == 2
    assert "-refresh=false" not in calls[0]
    assert "-refresh=false" in calls[1]


def test_sync_real_failure_is_not_retried():
    calls, err = _run_sync([1, 0], [REAL_FAILURE_OUT, ""])
    assert len(calls) == 1
    assert err is not None


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
