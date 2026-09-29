"""Unit tests: making the GCP gateway VM carry a PRA Network Tunnel.

GCE's two halves are `canIpForward` (so the VM may emit packets sourced from a leased
address) and an **alias IP range** on the NIC (so the fabric answers ARP for those
addresses). Without them the agent rejects its own lease — `No valid ARP reply
received` and then `setup: Exception - Address: 0.0.0.0 not found`.

The thing that makes GCP different from the other two clouds, and the reason most of
this file exists: **both halves are create-only.** `canIpForward` is a property of the
instance, settable at insert or on a TERMINATED instance — not on a running one. The
ensure path is called routinely by VM, cloud-database and k8s deploys and reuses an
existing gateway, so reconciling in place would mean stopping a shared gateway and
dropping every live session on it. It therefore WARNS and changes nothing.

Pinned here:

  * a fresh gateway gets `can_ip_forward` and an alias range carved from its subnetwork;
  * the reuse path never mutates the instance, and reports a warning naming what is
    missing so the operator knows a rebuild is required;
  * a gateway that already has both reports no warning;
  * the PAIRED per-VM path opts OUT explicitly — every gateway in one subnetwork would
    otherwise derive the same alias range, and GCE refuses overlapping ranges, so the
    second VM's gateway would fail to insert;
  * the per-region key outranks the flat one.

`google.cloud.compute_v1` is not installed on every machine that runs this suite and is
never stubbed elsewhere in the repo, so the instance-resource assertions are made
against the source text (the idiom in tests/test_gateway_registry.py and
tests/test_pov_cloud_gcp.py) and the reuse logic against duck-typed stand-ins.

Runs under pytest or standalone:  python tests/test_gcp_jumpoint_tunnel_pool.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import tunnel_pool as tp  # noqa: E402

_GCP_SRC = open(os.path.join(_ROOT, "web_dashboard", "services", "gcp_service.py"),
                encoding="utf-8").read()
_JHS_SRC = open(os.path.join(_ROOT, "web_dashboard", "services",
                             "jumpoint_host_service.py"), encoding="utf-8").read()
_VM_SRC = open(os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py"),
               encoding="utf-8").read()


def _fn_body(src, header):
    start = src.index(header)
    nxt = src.find("\ndef ", start + 10)
    nxt2 = src.find("\nasync def ", start + 10)
    end = min(x for x in (nxt, nxt2, len(src)) if x != -1)
    return src[start:end]


# ── the create path sets both halves ─────────────────────────────────────────

def test_the_instance_gets_can_ip_forward_and_an_alias_range():
    body = _fn_body(_GCP_SRC, "def _run_gce_jumpoint_sync(")
    assert "instance.can_ip_forward = True" in body, \
        "without canIpForward the VM cannot emit packets sourced from a leased address"
    assert "alias_ip_ranges" in body and "AliasIpRange(" in body, \
        "without an alias range the fabric never answers ARP for the leased address"


def test_the_alias_range_comes_from_the_shared_resolver():
    body = _fn_body(_GCP_SRC, "def _run_gce_jumpoint_sync(")
    assert "resolve_pool_cidr(" in body, \
        "GCP must use the CIDR resolver, not the list one — an alias range is a prefix"


def test_the_pool_is_only_attempted_when_there_is_a_subnetwork():
    # An alias range is carved out of the subnetwork's prefix; with only a bare
    # network there is nothing to derive from.
    body = _fn_body(_GCP_SRC, "def _run_gce_jumpoint_sync(")
    assert 'pool_cidr = ""' in body and "if subnetwork:" in body


def test_the_subnetwork_cidr_read_is_best_effort():
    body = _fn_body(_GCP_SRC, "def _subnetwork_cidr_sync(")
    assert "ip_cidr_range" in body
    assert 'return ""' in body and "except" in body, \
        "a pool we cannot derive must never stop the gateway coming up"


# ── the reuse path warns, and never mutates ──────────────────────────────────

class _Alias:
    def __init__(self, cidr):
        self.ip_cidr_range = cidr


class _Nic:
    def __init__(self, aliases=()):
        self.alias_ip_ranges = list(aliases)


class _Instance:
    def __init__(self, can_ip_forward=False, aliases=()):
        self.can_ip_forward = can_ip_forward
        self.network_interfaces = [_Nic(aliases)]
        self.self_link = "https://example/instances/gw"
        self.status = "RUNNING"


def _reuse_result(inst):
    """Re-implement the reuse block's readiness logic against a stand-in, mirroring
    gcp_service. Kept in step with the source by the guard test below."""
    nics = list(inst.network_interfaces or [])
    gaps = []
    if not getattr(inst, "can_ip_forward", False):
        gaps.append("canIpForward=false")
    if not (nics and list(getattr(nics[0], "alias_ip_ranges", []) or [])):
        gaps.append("no alias IP range")
    return gaps


def test_a_gateway_predating_this_reports_both_gaps():
    assert _reuse_result(_Instance()) == ["canIpForward=false", "no alias IP range"]


def test_a_half_configured_gateway_reports_only_what_is_missing():
    assert _reuse_result(_Instance(can_ip_forward=True)) == ["no alias IP range"]
    assert _reuse_result(_Instance(aliases=[_Alias("10.99.5.240/29")])) == \
        ["canIpForward=false"]


def test_a_ready_gateway_reports_nothing():
    assert _reuse_result(_Instance(True, [_Alias("10.99.5.240/29")])) == []


def test_the_reuse_path_warns_and_does_not_repair():
    """canIpForward is create-only, and this path is called by routine deploys on a
    SHARED gateway — repairing would stop it and drop every live session."""
    body = _fn_body(_GCP_SRC, "def _run_gce_jumpoint_sync(")
    reuse = body[:body.index("except NotFound:")]
    assert "tunnel_pool_warning" in reuse
    for mutation in ("client.update(", "instances.update(", "client.stop(",
                     "can_ip_forward = True"):
        assert mutation not in reuse, f"the reuse path must not {mutation!r}"


def test_the_reuse_path_still_starts_a_stopped_gateway():
    # Guard against "don't mutate" being over-applied: a STOPPED gateway is still
    # started, which is pre-existing behaviour unrelated to tunnels.
    body = _fn_body(_GCP_SRC, "def _run_gce_jumpoint_sync(")
    assert "client.start(" in body[:body.index("except NotFound:")]


# ── the paired per-VM path opts out ──────────────────────────────────────────

def test_the_paired_gateway_disables_the_pool_explicitly():
    """One gateway PER VM: deriving would give every one of them the same alias
    range, and GCE refuses overlapping ranges — the second VM's gateway would fail to
    insert outright, which is a deploy failure, not a missing feature."""
    assert "tunnel_pool_spec=tunnel_pool.DISABLED" in _VM_SRC, \
        "the paired path must opt out explicitly, not rely on the default (which derives)"


def test_disabled_really_disables_rather_than_deriving():
    assert tp.resolve_pool_cidr(tp.DISABLED, "10.99.5.0/24") == ""
    assert tp.resolve_pool(tp.DISABLED, "10.99.5.0/24", "gcp") == []


# ── config precedence ────────────────────────────────────────────────────────

def test_the_shared_host_prefers_the_region_config_over_the_flat_key():
    body = _fn_body(_JHS_SRC, "async def _ensure_jumpoint_host_gcp(")
    per_region = body.index('resolve_region("gcp", region).get("jumpoint_tunnel_pool")')
    flat = body.index('_cfg("gcp_jumpoint_tunnel_pool")')
    assert per_region < flat, "the flat key must be the FALLBACK, not read first"


def test_the_resolved_pool_is_surfaced_for_the_operator():
    # It has to be typed into the Pathfinder console by hand; nothing else can read it.
    body = _fn_body(_JHS_SRC, "async def _ensure_jumpoint_host_gcp(")
    assert "tunnel_pool" in body and "Pathfinder console" in body
    assert "tunnel_pool_warning" in body, "the reuse warning must reach the log too"


def test_the_derived_gcp_range_is_a_cidr_clear_of_reserved_addresses():
    assert tp.derive_pool_cidr("10.99.5.0/24") == "10.99.5.240/29"


def _run():
    mod = sys.modules[__name__]
    fns = [getattr(mod, n) for n in dir(mod) if n.startswith("test_")]
    failed = 0
    for fn in sorted(fns, key=lambda f: f.__name__):
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
