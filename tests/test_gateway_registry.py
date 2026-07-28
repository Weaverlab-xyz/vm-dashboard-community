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


def test_a_failed_deploy_records_the_host_it_already_built():
    """The AWS ensure launches the EC2 host and only then waits for its ECS agent, so a
    failure after that point leaves a host running. Recording host_id only on success
    meant the row showed an errored gateway with nothing to remove, and the running
    t3.small was findable only by the Name-tag lookup inside teardown_gateway."""
    src = _read("web_dashboard", "services", "gateway_service.py")
    body = src[src.index("async def _run_deploy"):src.index("async def _run_teardown")]
    assert "_record_partial_host" in body, (
        "a failed gateway deploy does not record the host it already created")
    # Anchor on the awaited call, not the name — it is also mentioned in the comment
    # above the guard, which would make this pass with no guard at all.
    ensure = body.index("await jumpoint_host_service")
    assert "try:" in body[:ensure], "the ensure call is not guarded"
    recorder = src[src.index("async def _record_partial_host"):src.index("async def _run_deploy")]
    assert "find_gateway_host_id" in recorder, "the recorder does not look the host up"
    assert "not row.host_id" in recorder, (
        "the recorder overwrites a host_id the row already had")


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


def test_the_gateway_table_records_the_egress_ip():
    """The column existed but nothing ever wrote it, so the node firewalls had no way
    to learn about a gateway they had to admit."""
    src = _read("web_dashboard", "database.py")
    block = src[src.index("class Gateway(Base)"):src.index("# ========== DATABASE UTILITIES")]
    assert "egress_ip = Column(" in block
    svc = _read("web_dashboard", "services", "gateway_service.py")
    assert "def record_egress_ip(" in svc, "nothing records a gateway's egress IP"
    jhs = _read("web_dashboard", "services", "jumpoint_host_service.py")
    assert "record_egress_ip" in jhs, (
        "the ensure path never writes the egress IP it just learned")


# ── the firewall consequences of a gateway existing ───────────────────────────

def test_a_gateway_deploy_reapplies_the_node_firewalls():
    """The bug this whole area turns on. A gateway is a new SOURCE address in front of
    every source-restricted node, so a deploy that doesn't re-apply the rules produces a
    gateway that cannot reach the node it was deployed to serve."""
    src = _read("web_dashboard", "services", "gateway_service.py")
    deploy = src[src.index("async def _run_deploy"):src.index("async def refresh_node_firewalls")]
    assert "refresh_node_firewalls" in deploy, (
        "gateway deploy never refreshes the node firewalls — the new gateway's /32 is "
        "missing from the allow list")
    teardown = src[src.index("async def _run_teardown"):]
    assert "refresh_node_firewalls" in teardown, (
        "gateway teardown leaves the departed gateway's /32 in the allow list")
    refresh = src[src.index("async def refresh_node_firewalls"):
                  src.index("async def _run_teardown")]
    for node in ("rancher", "portainer"):
        assert node in refresh, f"the {node} node firewall is not refreshed"


def test_every_live_gateway_is_allowed_not_just_the_shared_one():
    """Gateway hosts in a cloud join ONE PRA Gateway cluster and PRA distributes
    sessions across its nodes, so the broker may be any of them. Allowing a single
    remembered /32 is a coin flip."""
    for module in ("portainer_node_service", "rancher_node_service"):
        src = _read("web_dashboard", "services", f"{module}.py")
        assert "def _jumpoint_cidrs(" in src, f"{module} still allows one gateway /32"
        block = src[src.index("def _jumpoint_cidrs("):]
        block = block[:block.index("\ndef ")]
        assert "live_egress_ips" in block, (
            f"{module} does not read the gateway registry, so a user-deployed gateway "
            f"is never admitted")


def test_a_torn_down_gateway_stops_being_allowed():
    """A remembered IP that outlives its host keeps a dead VM in the rule while the
    gateway that replaced it goes unallowed."""
    src = _read("web_dashboard", "services", "jumpoint_host_service.py")
    assert "def _clear_managed_egress_ip(" in src
    for cloud in ("aws", "gcp", "azure"):
        assert f'_clear_managed_egress_ip("{cloud}"' in src, (
            f"the {cloud} idle teardown leaves the deleted gateway's IP in the firewall")
    svc = _read("web_dashboard", "services", "gateway_service.py")
    live = svc[svc.index("def live_egress_ips("):svc.index("def record_egress_ip(")]
    assert 'status != "deleted"' in live, (
        "live_egress_ips counts deleted gateways, so their /32s never leave the rule")


def test_a_web_jump_keeps_the_shared_gateway_alive():
    """What actually broke: the idle teardown reaped the gateway while a provisioned
    Web Jump was brokering through it, because nothing counted Web Jumps."""
    src = _read("web_dashboard", "services", "jumpoint_host_service.py")
    assert "def _active_web_jump_count(" in src, "Web Jumps hold no reference on the gateway"
    for fn in ("_teardown_jumpoint_host_if_idle_aws",
               "_teardown_jumpoint_host_if_idle_gcp",
               "_teardown_jumpoint_host_if_idle_azure"):
        block = src[src.index(f"async def {fn}("):]
        nxt = block.index("\nasync def ", 10) if "\nasync def " in block[10:] else len(block)
        block = block[:nxt]
        assert "_active_web_jump_count" in block, (
            f"{fn} can delete a gateway a Web Jump is using")
    count = src[src.index("def _active_web_jump_count("):
                src.index("async def teardown_jumpoint_host_if_idle")]
    assert "web_jump_id" in count, (
        "the count keys on the enabled flag alone, so an enabled-but-unprovisioned Web "
        "Jump would pin the gateway forever")


def test_the_gateway_launcher_falls_back_to_a_sibling_zone():
    """One capacity-exhausted zone left the deployment with NO gateway: the ensure path
    is best-effort, so the 503 was only logged."""
    src = _read("web_dashboard", "services", "gcp_service.py")
    block = src[src.index("def _run_gce_jumpoint_sync("):src.index("async def run_gce_jumpoint(")]
    assert "_is_zone_capacity_error" in block, (
        "the gateway launcher has no zone fallback, unlike the Portainer/Rancher ones")
    assert "_find_instance_zone_in_region" in block, (
        "with zone fallback, a zone-scoped reuse check would create a duplicate gateway "
        "in a sibling zone")


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
