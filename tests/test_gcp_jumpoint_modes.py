"""Unit tests: the paired-vs-shared Jumpoint discriminator on GCP deploys.

A GCP batch borrows the ref-counted Jumpoint host that cloud databases, k8s tunnels
and VDI seats already share, while a single deploy still starts its own paired
``bt-jumpoint-<vm>`` VM. Destroy has to tell those apart, and both ways of getting it
wrong are expensive:

  * treat a shared host as paired and a VM destroy DELETES it — every cloud-database
    tunnel, k8s tunnel and desktop session routed through it dies with it;
  * treat a paired VM as shared and its e2-micro is orphaned, billing forever.

So the two things pinned here are that ``_JumpointRef.record`` never writes a shared
host under ``jumpoint_name`` (the key that triggers paired teardown), and that the
legacy inference — rows written before ``jumpoint_mode`` existed — resolves to paired.
That inference is what lets this ship without a data migration, so it is worth a test
rather than a comment.

``_active_gce_count`` is covered here too. It is the term that stops a cloud-database
decommission reclaiming the host out from under running GCE VMs; the GCP teardown
summed only databases, k8s and VDI before this change.

Imported as real package modules with their heavy dependencies stubbed in sys.modules,
so this runs without the google-cloud SDKs or a database (both modules use relative
imports, so loading them by file path is not an option). Runs under pytest, or
standalone:  python tests/test_gcp_jumpoint_modes.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_stubs():
    """Stub everything gcp_vm_service / jumpoint_host_service pull in at import time."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules.setdefault(name, m)
        return m

    sa_orm = mod("sqlalchemy.orm", Session=type("Session", (), {}))
    mod("sqlalchemy", orm=sa_orm)
    mod("web_dashboard.config", settings=types.SimpleNamespace(
        bt_jump_group_name="", bt_jumpoint_name=""))
    # Job needs the columns the count's filter() expression touches; _FakeQuery
    # ignores the resulting comparisons, they just have to evaluate.
    mod("web_dashboard.database",
        Job=type("Job", (), {"job_type": None, "status": None, "id": None}),
        SessionLocal=lambda: None,
        CloudDatabase=type("CloudDatabase", (), {}),
        K8sCluster=type("K8sCluster", (), {}),
        VirtualDesktop=type("VirtualDesktop", (), {}))
    mod("web_dashboard.models.gcp", GCPDeployRequest=type("GCPDeployRequest", (), {}))
    for svc in ("cache_service", "gcp_service", "job_service", "region_catalog"):
        mod(f"web_dashboard.services.{svc}")


_install_stubs()

try:
    from web_dashboard.services import gcp_vm_service as gvs
    from web_dashboard.services import jumpoint_host_service as jhs
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── _JumpointRef.record ──────────────────────────────────────────────────────

def test_paired_records_the_keys_the_paired_teardown_reads():
    meta = {}
    gvs._JumpointRef("paired", name="bt-jumpoint-web-01", zone="us-east1-b").record(meta)
    assert meta["jumpoint_mode"] == "paired"
    assert meta["jumpoint_name"] == "bt-jumpoint-web-01"
    assert meta["jumpoint_zone"] == "us-east1-b"
    assert "jumpoint_host_id" not in meta


def test_shared_never_writes_jumpoint_name():
    """The assertion that protects the whole shared-host estate.

    jumpoint_name is what _run_destroy's paired branch keys on, and its sibling count
    only looks at gce_deploy rows — so a shared host recorded there would be deleted
    when the last GCE VM went, taking every cloud-DB / k8s / VDI tunnel with it."""
    meta = {}
    gvs._JumpointRef("shared", name="clouddb-shared-jumpoint", region="us-east1").record(meta)
    assert "jumpoint_name" not in meta, meta
    assert "jumpoint_zone" not in meta, meta
    assert meta["jumpoint_mode"] == "shared"
    assert meta["jumpoint_host_id"] == "clouddb-shared-jumpoint"
    assert meta["jumpoint_region"] == "us-east1"


def test_an_error_is_recorded_without_claiming_a_host():
    """A failed ensure must not leave a reference behind, or _active_gce_count would
    pin the shared host on behalf of a VM that never used it."""
    meta = {}
    gvs._JumpointRef("shared", region="us-east1", error="deploy key missing").record(meta)
    assert meta["jumpoint_error"] == "deploy key missing"
    assert "jumpoint_host_id" not in meta
    assert "jumpoint_mode" not in meta


def test_a_ref_with_neither_name_nor_error_records_nothing():
    meta = {}
    gvs._JumpointRef("paired").record(meta)
    assert meta == {}


# ── the legacy inference (why no data migration is needed) ───────────────────

def _infer(meta):
    """The rule _run_destroy applies. Mirrored here because the real one is embedded
    in a long async function that needs a live session to reach."""
    mode = meta.get("jumpoint_mode")
    if not mode and meta.get("jumpoint_name"):
        mode = "paired"
    return mode


def test_a_pre_change_row_infers_as_paired():
    """Rows written before jumpoint_mode existed only ever had a paired jumpoint:
    nothing wrote jumpoint_host_id on a gce_deploy, and the sole writer of
    jumpoint_name was the paired block. So the inference is total, not a guess."""
    assert _infer({"jumpoint_name": "bt-jumpoint-web", "jumpoint_zone": "us-east1-b"}) == "paired"


def test_a_row_with_no_jumpoint_at_all_infers_nothing():
    assert _infer({}) is None
    assert _infer({"jumpoint_error": "it broke"}) is None


def test_explicit_modes_win_over_the_inference():
    assert _infer({"jumpoint_mode": "shared", "jumpoint_host_id": "h"}) == "shared"
    assert _infer({"jumpoint_mode": "paired", "jumpoint_name": "bt-jumpoint-web"}) == "paired"


def test_record_output_round_trips_through_the_inference():
    """Whatever record() writes, the destroy branch must classify the same way."""
    for ref in (gvs._JumpointRef("paired", name="bt-jumpoint-web", zone="z"),
                gvs._JumpointRef("shared", name="host", region="r")):
        meta = {}
        ref.record(meta)
        assert _infer(meta) == ref.mode


# ── _active_gce_count ────────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


def _row(**meta):
    return types.SimpleNamespace(metadata_dict=meta)


def test_only_rows_that_actually_borrowed_the_host_are_counted():
    db = _FakeDB([
        _row(jumpoint_host_id="shared-host"),                  # counts
        _row(jumpoint_mode="paired", jumpoint_name="bt-x"),    # paired: must not count
        _row(),                                                # no jumpoint at all
    ])
    assert jhs._active_gce_count(db) == 1


def test_a_destroyed_row_releases_its_reference():
    db = _FakeDB([
        _row(jumpoint_host_id="shared-host"),
        _row(jumpoint_host_id="shared-host", destroyed=True),
    ])
    assert jhs._active_gce_count(db) == 1


def test_a_failed_ensure_holds_no_reference():
    """jumpoint_mode='shared' with no host id means ensure failed — that VM never used
    the host, so it must not keep it alive."""
    db = _FakeDB([_row(jumpoint_mode="shared", jumpoint_error="unavailable")])
    assert jhs._active_gce_count(db) == 0


def test_the_gcp_teardown_actually_consults_the_gce_count():
    """The regression guard for the real bug: the GCP teardown summed databases, k8s
    and VDI but not GCE VMs, so a cloud-database decommission would delete the shared
    host out from under every running batch-deployed VM.

    Checked as an AST call node rather than a substring — the function's own comment
    names _active_gce_count, so a text search passes even after the call is deleted."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(jhs._teardown_jumpoint_host_if_idle_gcp).strip())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_active_gce_count" in called, (
        "the GCP idle-teardown no longer CALLS _active_gce_count — a cloud-DB "
        f"decommission will reclaim the shared Jumpoint while GCE VMs still use it. "
        f"Counted: {sorted(called)}")


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
