"""The Gateway registry: naming, ownership, and what may be deleted.

Operators can now deploy Gateway hosts to carry session load, alongside the one the
dashboard auto-ensures for its own tunnels. Two kinds of host in the same clouds means
two things have to hold, and neither is visible by reading a diff:

  * **the managed gateway is not the operator's to delete.** Its lifecycle is the
    reference-counted ensure/idle pair; removing it by request would leave the
    auto-ensure to recreate it while the row said it was gone.
  * **names must be unique and cloud-legal.** The name is not cosmetic — it is what
    keeps the managed and requested hosts apart *in the cloud*, because the managed
    teardown finds its host by name tag. A duplicate name is how a user gateway
    becomes collateral in an idle teardown.

Naming rules are delegated to `vm_naming` rather than re-derived: it already encodes
RFC1035 for GCP and 15 characters for Azure, the latter because both the user-VM and
the gateway paths derive the in-guest hostname as `name[:15]`.

`gateway_service` keeps its cloud and ORM imports function-local, so the pure parts
run here without sqlalchemy.

Run: python tests/test_gateway_registry.py   (or under pytest)
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from web_dashboard.services import gateway_service as gs  # noqa: E402
from web_dashboard.services import vm_naming  # noqa: E402


def _read(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


def _patch_managed_name(monkey_name="managed-gw"):
    """gateway_service asks jumpoint_host_service for the managed name, which reads
    config. Pin it so these tests don't need a DB-backed config."""
    from web_dashboard.services import jumpoint_host_service as jhs
    jhs.managed_host_name = lambda cloud: monkey_name
    return monkey_name


# ── naming ────────────────────────────────────────────────────────────────────

def test_a_blank_name_is_rejected_with_a_reason():
    _patch_managed_name()
    try:
        gs.check_name("aws", "  ", [])
    except gs.GatewayError as e:
        assert "required" in str(e).lower()
    else:
        raise AssertionError("a blank gateway name was accepted")


def test_gcp_names_must_be_rfc1035():
    """GCE enforces this itself, so accepting it here just moves the failure to a
    place where it costs a job."""
    _patch_managed_name()
    for bad in ("Gateway-01", "1gw", "gw_01"):
        try:
            gs.check_name("gcp", bad, [])
        except gs.GatewayError:
            pass
        else:
            raise AssertionError(f"GCP accepted the illegal instance name {bad!r}")
    assert gs.check_name("gcp", "gw-us-central1-01", []) == "gw-us-central1-01"


def test_azure_names_are_bounded_by_the_hostname_truncation():
    """azure_service derives computer_name as name[:15] on the gateway path too, so
    two gateways differing only past character 15 would share an in-guest hostname."""
    _patch_managed_name()
    assert vm_naming.name_limit("azure") == 15
    try:
        gs.check_name("azure", "gateway-that-is-far-too-long", [])
    except gs.GatewayError as e:
        assert "15" in str(e)
    else:
        raise AssertionError("an over-long Azure gateway name was accepted")


def test_a_duplicate_name_is_rejected():
    _patch_managed_name()
    try:
        gs.check_name("aws", "GW-01", ["gw-01"])   # case-insensitive
    except gs.GatewayError as e:
        assert "already exists" in str(e)
    else:
        raise AssertionError("a duplicate gateway name was accepted")


def test_the_managed_name_cannot_be_taken_by_a_requested_gateway():
    """The sharpest one. The managed teardown finds its host by name tag, so a user
    gateway wearing that name is a host the idle teardown will terminate."""
    managed = _patch_managed_name("dashboard-sandbox-jumpoint-host")
    try:
        gs.check_name("aws", managed, [managed])
    except gs.GatewayError as e:
        assert "already exists" in str(e)
    else:
        raise AssertionError(
            "a requested gateway was allowed to take the managed name — the idle "
            "teardown would terminate it as though it were the managed host")


def test_suggested_names_are_free_and_legal_for_every_cloud():
    _patch_managed_name()
    for cloud, region in (("aws", "us-east-2"), ("azure", "centralus"), ("gcp", "us-central1")):
        name = gs.next_free_name(cloud, region, [])
        assert len(name) <= vm_naming.name_limit(cloud), f"{cloud}: {name!r} too long"
        vm_naming.validate_base(name, cloud)          # raises if illegal
        gs.check_name(cloud, name, [])                # raises if taken


def test_a_suggested_name_skips_one_already_taken():
    _patch_managed_name()
    assert gs.next_free_name("aws", "us-east-2", ["gw-us-east-2-01"]) != "gw-us-east-2-01"


# ── ownership ─────────────────────────────────────────────────────────────────

def test_the_teardown_helper_refuses_the_managed_gateway():
    """Defence in depth behind the API's own check: the service must not delete the
    managed host even if something else calls it.

    The refusal must come *before* any cloud call, so this blocks the first thing the
    AWS branch touches with a marker. Asserting only "ValueError was raised" would be
    weaker than it looks — without the guard this path reaches a native dependency
    that aborts the interpreter, so a broken guard would take the whole run down
    rather than fail this test."""
    import asyncio
    from web_dashboard.services import jumpoint_host_service as jhs

    class Reached(Exception):
        pass

    jhs.managed_host_name = lambda cloud: "managed-gw"
    original = jhs._aws_region_cfg
    jhs._aws_region_cfg = lambda region: (_ for _ in ()).throw(Reached("cloud path entered"))
    try:
        asyncio.run(jhs.teardown_gateway("aws", "us-east-2", "managed-gw"))
    except Reached:
        raise AssertionError(
            "teardown_gateway reached the cloud path for the auto-managed gateway — "
            "it must refuse before touching AWS")
    except ValueError as e:
        assert "auto-managed" in str(e)
    else:
        raise AssertionError("teardown_gateway accepted the auto-managed gateway")
    finally:
        jhs._aws_region_cfg = original


def test_the_delete_endpoint_refuses_the_managed_row():
    src = _read("web_dashboard", "api", "gateways.py")
    body = src[src.index("async def destroy_gateway"):]
    assert "row.managed" in body, "the delete endpoint does not check `managed`"
    assert "400" in body, "refusing a managed gateway should be a 400, not a silent no-op"


def test_deploy_is_queued_not_run_in_request():
    """A gateway launch is a cloud call with a registration wait; in-request execution
    is what strands it when the worker recycles."""
    src = _read("web_dashboard", "api", "gateways.py")
    assert "background_tasks" not in src, "the gateway API dispatches in-process"
    worker = _read("web_dashboard", "jobs_worker.py")
    for t in ("gateway_deploy", "gateway_teardown"):
        assert f'"{t}"' in worker, f"{t} is not wired into the runner"


def test_the_service_exposes_the_runner_entry_point():
    src = _read("web_dashboard", "services", "gateway_service.py")
    import re
    fn = re.search(r"async def run\((.*?)\) -> None:", src, re.S)
    assert fn, "gateway_service has no run() entry point"
    for expected in ("db", "job_id", "meta"):
        assert expected in fn.group(1), f"run() is missing {expected}"


# ── registry shape ────────────────────────────────────────────────────────────

def test_the_managed_row_is_adopted_by_the_ensure_path_only():
    """A requested gateway already owns its row; adopting it again under managed=True
    would make it undeletable."""
    src = _read("web_dashboard", "services", "jumpoint_host_service.py")
    block = src[src.index("async def ensure_jumpoint_host"):src.index("def _adopt_managed_row")]
    assert "requested = bool(name)" in block
    assert "not requested" in block, (
        "ensure_jumpoint_host adopts a managed row unconditionally — a requested "
        "gateway would be recorded as managed and then refuse deletion")


def test_the_gateway_table_records_which_kind_it_is():
    src = _read("web_dashboard", "database.py")
    block = src[src.index("class Gateway(Base)"):src.index("# ========== DATABASE UTILITIES")]
    for col in ("cloud", "region", "name", "status", "managed", "host_id"):
        assert f"{col} = Column(" in block, f"Gateway has no {col} column"
    assert "index=True" in block


def test_no_cap_is_imposed_on_gateway_count():
    """The stated requirement: three in us-central1, two in us-east-2, operator's
    discretion. A cap would have to come from somewhere, so assert nothing invented
    one."""
    api = _read("web_dashboard", "api", "gateways.py")
    svc = _read("web_dashboard", "services", "gateway_service.py")
    for src, where in ((api, "api"), (svc, "service")):
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(
                isinstance(c, ast.Constant) and isinstance(c.value, int) and c.value > 1
                for c in node.comparators
            ):
                seg = ast.get_source_segment(src, node) or ""
                assert "len(" not in seg or "gateway" not in seg.lower(), (
                    f"{where} appears to cap the gateway count: {seg}")


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
