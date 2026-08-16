"""Unit tests: the shared-host-vs-ACI Jumpoint discriminator on Azure deploys.

Sibling of test_gcp_jumpoint_modes.py. A single Azure VM deploy now borrows the
ref-counted ``clouddb-jumpoint`` VM that cloud databases, k8s tunnels and VDI seats
already share, instead of starting its own ACI container group. Two live failures drove
the change, and both are pinned here:

  * ACI is serverless and cannot protocol-tunnel (no NET_ADMIN / NET_RAW / IPC_LOCK, no
    /dev/net/tun), so an ACI-brokered VM never gets a Protocol Tunnel;
  * every ACI group gets a random name but they all mount ONE ``/jpt`` Azure File share
    — the Jumpoint's persistent identity store. Successive groups contended over one
    install, and once the ``.installed-<key-hash>`` marker disagreed with the install on
    disk the container crash-looped (ExitCode 1, no output) and never registered with
    PRA at all.

Destroy has to tell the two shapes apart, and both ways of getting it wrong are
expensive:

  * treat the shared host as ACI-owned and a VM destroy stops the wrong thing — or, via
    the untracked-orphan sweep, every ACI group in the resource group;
  * treat an ACI group as shared and it is orphaned, billing forever.

So the load-bearing assertion is that ``_AciRef.record`` never writes a borrowed host
under ``aci_group_name`` — that key drives the ACI teardown, and its ABSENCE is what
arms the sweep. ``_active_azure_vm_count`` is covered too: it is the term that stops a
cloud-database decommission reclaiming the host out from under running Azure VMs, which
the Azure teardown summed without before this change.

Imported as real package modules with their heavy dependencies stubbed in sys.modules,
so this runs without the azure SDKs or a database (both modules use relative imports, so
loading them by file path is not an option). Runs under pytest, or standalone:
    python tests/test_azure_jumpoint_modes.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_stubs():
    """Stub everything azure_vm_service / jumpoint_host_service pull in at import time."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules.setdefault(name, m)
        return m

    sa_orm = mod("sqlalchemy.orm", Session=type("Session", (), {}))
    mod("sqlalchemy", orm=sa_orm)
    mod("web_dashboard.config", settings=types.SimpleNamespace(
        pra_enabled=False, azure_vm_jumpoint_mode="shared",
        azure_aci_subnet_id="", azure_aci_cpu=1.0, azure_aci_memory=2.0,
        azure_aci_storage_account="", azure_aci_storage_account_rg="",
        azure_aci_file_share="jpt"))
    # Job needs the columns the count's filter() expression touches; _FakeQuery
    # ignores the resulting comparisons, they just have to evaluate.
    mod("web_dashboard.database",
        Job=type("Job", (), {"job_type": None, "status": None, "id": None}),
        SessionLocal=lambda: None,
        CloudDatabase=type("CloudDatabase", (), {}),
        K8sCluster=type("K8sCluster", (), {}),
        VirtualDesktop=type("VirtualDesktop", (), {}))
    mod("web_dashboard.models.azure",
        AzureDeployRequest=type("AzureDeployRequest", (), {"model_fields": {}}),
        AzureBulkDeployRequest=type("AzureBulkDeployRequest", (), {}),
        AzureCreateImageRequest=type("AzureCreateImageRequest", (), {}))
    azure_service = mod("web_dashboard.services.azure_service",
                        AzureError=type("AzureError", (Exception,), {}))
    mod("web_dashboard.services.azure_service", **{})
    for svc in ("cache_service", "job_service"):
        mod(f"web_dashboard.services.{svc}")
    return azure_service


_install_stubs()

try:
    from web_dashboard.services import azure_vm_service as avs
    from web_dashboard.services import jumpoint_host_service as jhs
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── _AciRef.record ───────────────────────────────────────────────────────────

def test_an_owned_aci_records_the_key_the_aci_teardown_reads():
    result = {}
    avs._AciRef("owned", group_name="bt-jumpoint-azure-0c8b10cb").record(result)
    assert result["aci_group_name"] == "bt-jumpoint-azure-0c8b10cb"
    assert "jumpoint_host_id" not in result
    assert "jumpoint_mode" not in result


def test_a_batch_shared_aci_records_the_same_key():
    """Batch mode still owns a real container group — only the teardown responsibility
    differs, and that is decided by the sibling count, not by the key written."""
    result = {}
    avs._AciRef("shared", group_name="bt-jumpoint-azure-deadbeef").record(result)
    assert result["aci_group_name"] == "bt-jumpoint-azure-deadbeef"


def test_a_borrowed_host_never_writes_aci_group_name():
    """The assertion that protects the whole shared-host estate.

    aci_group_name is what _run_destroy's ACI branch keys on, and its sibling count
    only looks at azure_deploy rows — so a borrowed host recorded there would be
    stopped as though it were this deploy's container."""
    result = {}
    avs._AciRef("host", host_id="clouddb-jumpoint", region="centralus").record(result)
    assert "aci_group_name" not in result, result
    assert "aci_error" not in result, result
    assert result["jumpoint_mode"] == "host"
    assert result["jumpoint_host_id"] == "clouddb-jumpoint"
    assert result["jumpoint_region"] == "centralus"


def test_a_failed_host_ensure_records_no_reference():
    """A failed ensure must not leave a reference behind, or _active_azure_vm_count
    would pin the shared host on behalf of a VM that never used it."""
    result = {}
    avs._AciRef("host", region="centralus", error="deploy key missing").record(result)
    assert result["jumpoint_error"] == "deploy key missing"
    assert "jumpoint_host_id" not in result
    assert result["jumpoint_mode"] == "host"


def test_a_failed_aci_records_aci_error_only():
    result = {}
    avs._AciRef("owned", error="quota exceeded").record(result)
    assert result["aci_error"] == "quota exceeded"
    assert "aci_group_name" not in result


def test_a_ref_with_neither_name_nor_error_records_nothing():
    result = {}
    avs._AciRef("owned").record(result)
    assert result == {}


def test_only_an_owned_group_is_torn_down_on_a_failed_vm_create():
    """`owned` is what decides whether a failed deploy_vm stops the container. A batch's
    group must survive for its siblings, and a borrowed host must never be stopped."""
    assert avs._AciRef("owned", group_name="g").owned is True
    assert avs._AciRef("shared", group_name="g").owned is False
    assert avs._AciRef("host", host_id="clouddb-jumpoint").owned is False
    assert avs._AciRef("owned").owned is False          # nothing was created


# ── the legacy inference (why no data migration is needed) ───────────────────

def _infer(meta):
    """The rule _run_destroy applies: an explicit jumpoint_mode of "host" releases a
    shared reference; anything else falls through to the ACI branch, which keys on
    aci_group_name. Mirrored here because the real one is embedded in a long async
    function that needs a live session to reach."""
    return "host" if meta.get("jumpoint_mode") == "host" else "aci"


def test_a_pre_change_row_infers_as_aci():
    """Rows written before jumpoint_mode existed only ever had an ACI jumpoint: nothing
    wrote jumpoint_host_id on an azure_deploy, and the sole writer of aci_group_name was
    the ACI path. So the inference is total, not a guess — which is what lets this ship
    without a data migration."""
    assert _infer({"aci_group_name": "bt-jumpoint-azure-0c8b10cb"}) == "aci"
    assert _infer({"aci_error": "it broke"}) == "aci"
    assert _infer({}) == "aci"


def test_record_output_round_trips_through_the_inference():
    """Whatever record() writes, the destroy branch must classify the same way."""
    cases = [
        (avs._AciRef("owned", group_name="g"), "aci"),
        (avs._AciRef("shared", group_name="g"), "aci"),
        (avs._AciRef("host", host_id="clouddb-jumpoint", region="centralus"), "host"),
    ]
    for ref, expected in cases:
        result = {}
        ref.record(result)
        assert _infer(result) == expected, (ref.mode, result)


# ── _active_azure_vm_count ───────────────────────────────────────────────────

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
        _row(jumpoint_mode="host", jumpoint_host_id="clouddb-jumpoint"),  # counts
        _row(aci_group_name="bt-jumpoint-azure-0c8b10cb"),                # ACI: must not
        _row(),                                                           # no jumpoint
    ])
    assert jhs._active_azure_vm_count(db) == 1


def test_a_destroyed_row_releases_its_reference():
    db = _FakeDB([
        _row(jumpoint_host_id="clouddb-jumpoint"),
        _row(jumpoint_host_id="clouddb-jumpoint", destroyed=True),
    ])
    assert jhs._active_azure_vm_count(db) == 1


def test_a_failed_ensure_holds_no_reference():
    """jumpoint_mode='host' with no host id means ensure failed — that VM never used the
    host, so it must not keep it alive."""
    db = _FakeDB([_row(jumpoint_mode="host", jumpoint_error="unavailable")])
    assert jhs._active_azure_vm_count(db) == 0


def test_the_azure_teardown_actually_consults_the_vm_count():
    """The regression guard for the real bug: the Azure teardown summed databases, k8s
    and VDI but not Azure VMs, so a cloud-database decommission would delete the shared
    host out from under every running VM that borrowed it.

    Checked as an AST call node rather than a substring — the function's own comment
    names _active_azure_vm_count, so a text search passes even after the call is
    deleted."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(jhs._teardown_jumpoint_host_if_idle_azure).strip())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_active_azure_vm_count" in called, (
        "the Azure idle-teardown no longer CALLS _active_azure_vm_count — a cloud-DB "
        "decommission will reclaim the shared Jumpoint while Azure VMs still use it. "
        f"Counted: {sorted(called)}")


# ── which jumpoint a SINGLE deploy gets ──────────────────────────────────────

class _Request:
    def __init__(self, docker_deploy_key_ref=None):
        self.docker_deploy_key_ref = docker_deploy_key_ref


def _with_mode(mode):
    """Point azure_vm_service._cfg at a fixed azure_vm_jumpoint_mode."""
    original = avs._cfg
    avs._cfg = lambda key, fallback="": (mode if key == "azure_vm_jumpoint_mode"
                                         else original(key, fallback))
    return original


def test_singles_default_to_the_shared_host():
    original = _with_mode("")          # unset — the common case
    try:
        assert avs._aci_requested(_Request()) is False
    finally:
        avs._cfg = original


def test_aci_mode_is_honoured():
    original = _with_mode("aci")
    try:
        assert avs._aci_requested(_Request()) is True
    finally:
        avs._cfg = original


def test_mode_parsing_is_forgiving():
    for raw in ("ACI", " aci ", "Aci"):
        original = _with_mode(raw)
        try:
            assert avs._aci_requested(_Request()) is True, raw
        finally:
            avs._cfg = original
    for raw in ("shared", "", "nonsense"):
        original = _with_mode(raw)
        try:
            assert avs._aci_requested(_Request()) is False, raw
        finally:
            avs._cfg = original


def test_a_per_deploy_key_forces_aci_even_in_shared_mode():
    """The shared host is one VM serving many resources and resolves its deploy key from
    config, so there is nowhere to honour a per-deploy override on it. The Azure deploy
    form exposes that override as a secret picker — letting shared mode swallow it would
    make a visible field silently do nothing."""
    original = _with_mode("shared")
    try:
        assert avs._aci_requested(_Request(docker_deploy_key_ref="akv://key")) is True
    finally:
        avs._cfg = original


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
