"""Unit tests: the cloud-agnostic network-tunnel address-pool arithmetic.

A PRA Network Tunnel leases the operator a real address ON the target network. The
Gateway asks DHCP first (no cloud answers — their DHCP only serves an address already
bound to a NIC), falls back to the configured pool, and then ARPs to validate the
address it picked. An address the fabric has never heard of gets no reply and the
agent refuses its own lease:

    Timeout: No valid ARP reply received from 10.99.5.202 in 2000ms
    setup: Exception - Address: 0.0.0.0 not found

Registering the pool is per-cloud (Azure secondary ipconfigs, AWS secondary private
IPs, GCP an alias range) but *choosing* it is identical, which is why it lives in one
module. What is pinned here is the part that is easy to get subtly wrong later:

  * the pool comes off the TOP of the subnet — every cloud allocates dynamically from
    the bottom, so the top stays clear of real hosts longest;
  * each cloud's reserved addresses are excluded, and they DIFFER: Azure and AWS take
    .1/.2/.3, GCP takes .1 and the SECOND-TO-LAST;
  * Azure's output is unchanged by the extraction from azure_service — a /24 must
    still give .247-.254, because that is what live gateways are registered with;
  * a malformed spec falls BACK to deriving rather than leaving a gateway with no
    pool, and an oversized one is capped (a pasted /16 would be 65k addresses);
  * GCP takes a CIDR, not a list, because an alias range is a prefix — and a
    start-end range is REFUSED there rather than silently widened, since widening
    hands you addresses you did not ask for;
  * "off" disables the pool entirely, which is how a per-VM paired gateway opts out
    of colliding with every other gateway in its subnetwork.

Runs under pytest or standalone:  python tests/test_tunnel_pool.py
"""
import ipaddress
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services import tunnel_pool as tp  # noqa: E402


# ── deriving a list (Azure, AWS) ─────────────────────────────────────────────

def test_azure_output_is_unchanged_by_the_extraction():
    # Live Azure gateways are registered with exactly these. If this moves, a rebuilt
    # gateway silently stops matching the Pathfinder console.
    assert tp.derive_pool("10.99.5.0/24", "azure") == \
        [f"10.99.5.{n}" for n in range(247, 255)]


def test_aws_matches_azure_because_both_reserve_the_first_four():
    assert tp.derive_pool("10.99.5.0/24", "aws") == tp.derive_pool("10.99.5.0/24", "azure")


def test_gcp_shifts_down_one_for_its_reserved_second_to_last():
    # GCE reserves .1 and the second-to-last (.254 on a /24) on top of network and
    # broadcast — so its window ends one lower than Azure's.
    assert tp.derive_pool("10.99.5.0/24", "gcp") == \
        [f"10.99.5.{n}" for n in range(246, 254)]


def test_no_cloud_ever_returns_a_reserved_address():
    for cloud, reserved in (("azure", ("10.99.5.1", "10.99.5.2", "10.99.5.3")),
                            ("aws", ("10.99.5.1", "10.99.5.2", "10.99.5.3")),
                            ("gcp", ("10.99.5.1", "10.99.5.254"))):
        pool = tp.derive_pool("10.99.5.0/24", cloud)
        for bad in reserved + ("10.99.5.0", "10.99.5.255"):
            assert bad not in pool, f"{cloud} handed out {bad}"


def test_an_explicit_size_is_honoured():
    assert tp.derive_pool("10.99.5.0/24", "azure", size=3) == \
        ["10.99.5.252", "10.99.5.253", "10.99.5.254"]


def test_a_subnet_too_small_yields_what_it_can_rather_than_raising():
    assert tp.derive_pool("10.99.5.0/29", "azure") == ["10.99.5.4", "10.99.5.5", "10.99.5.6"]


def test_garbage_yields_empty_so_the_gateway_still_comes_up():
    for bad in ("", "not-a-subnet", "10.99.5.0/33"):
        assert tp.derive_pool(bad, "azure") == []


def test_an_unknown_cloud_falls_back_to_the_conservative_rule():
    # Better to skip an address that was actually usable than hand out a reserved one.
    assert tp.derive_pool("10.99.5.0/24", "nimbus") == tp.derive_pool("10.99.5.0/24", "aws")


# ── parsing an explicit list spec ────────────────────────────────────────────

def test_parse_inclusive_range():
    assert tp.parse_pool("10.99.5.200-10.99.5.203") == \
        ["10.99.5.200", "10.99.5.201", "10.99.5.202", "10.99.5.203"]


def test_parse_a_backwards_range_is_still_a_range():
    assert tp.parse_pool("10.99.5.203-10.99.5.200") == tp.parse_pool("10.99.5.200-10.99.5.203")


def test_parse_cidr_excludes_network_and_broadcast():
    pool = tp.parse_pool("10.99.5.200/29")
    assert pool[0] == "10.99.5.201" and pool[-1] == "10.99.5.206"


def test_parse_single_address():
    assert tp.parse_pool("10.99.5.200") == ["10.99.5.200"]


def test_parse_caps_a_huge_range():
    assert len(tp.parse_pool("10.99.0.0/16")) == tp.POOL_MAX


def test_parse_blank_or_malformed_is_empty_so_the_caller_derives():
    for bad in ("", "   ", "nonsense", "10.99.5.999-10.99.5.1000"):
        assert tp.parse_pool(bad) == []


# ── resolve precedence ───────────────────────────────────────────────────────

def test_resolve_prefers_an_explicit_spec():
    assert tp.resolve_pool("10.99.5.10-10.99.5.11", "10.99.5.0/24", "aws") == \
        ["10.99.5.10", "10.99.5.11"]


def test_resolve_falls_back_to_derivation_when_blank_or_malformed():
    want = tp.derive_pool("10.99.5.0/24", "aws")
    assert tp.resolve_pool("", "10.99.5.0/24", "aws") == want
    # A typo in an OPTIONAL setting must not cost the gateway its pool entirely.
    assert tp.resolve_pool("10.99.5.oops", "10.99.5.0/24", "aws") == want


def test_resolve_with_neither_is_empty_not_an_exception():
    assert tp.resolve_pool("", "", "aws") == []


def test_off_disables_the_pool_and_derives_nothing():
    # The escape hatch for a per-VM paired gateway: every gateway in one subnet would
    # otherwise derive the same addresses and collide.
    assert tp.resolve_pool("off", "10.99.5.0/24", "aws") == []
    assert tp.resolve_pool("OFF", "10.99.5.0/24", "aws") == []
    assert tp.parse_pool("off") == []


# ── GCP: an alias range is a CIDR, not a list ────────────────────────────────

def test_derive_cidr_stays_clear_of_gcp_reserved_addresses():
    # .248/29 would swallow .254 (reserved) and .255 (broadcast), so the answer is
    # the next aligned block down.
    assert tp.derive_pool_cidr("10.99.5.0/24") == "10.99.5.240/29"


def test_derive_cidr_is_always_inside_the_usable_range():
    for cidr in ("10.99.5.0/24", "10.99.5.0/26", "10.99.5.0/28", "10.0.0.0/16"):
        got = tp.derive_pool_cidr(cidr)
        assert got, cidr
        net, block = ipaddress.ip_network(cidr), ipaddress.ip_network(got)
        usable = list(net.hosts())[1:-1]          # GCP: drop .1 and second-to-last
        assert block.network_address >= usable[0], (cidr, got)
        assert block.broadcast_address <= usable[-1], (cidr, got)


def test_derive_cidr_shrinks_the_block_rather_than_giving_up():
    # A /28 cannot seat an aligned /29 once reserved addresses are out. Half a pool
    # beats refusing one.
    got = tp.derive_pool_cidr("10.99.5.0/28")
    assert got and ipaddress.ip_network(got).prefixlen > 29


def test_derive_cidr_gives_up_on_a_subnet_with_no_room():
    for tiny in ("10.99.5.0/29", "10.99.5.0/30", "10.99.5.0/31"):
        assert tp.derive_pool_cidr(tiny) == ""


def test_parse_cidr_accepts_a_prefix():
    assert tp.parse_pool_cidr("10.99.5.240/29") == "10.99.5.240/29"


def test_parse_cidr_refuses_a_range_rather_than_widening_it():
    # Widening would hand back a pool covering addresses the operator never listed,
    # which is how you collide with a live host.
    assert tp.parse_pool_cidr("10.99.5.240-10.99.5.247") == ""


def test_parse_cidr_refuses_an_oversized_block():
    assert tp.parse_pool_cidr("10.99.0.0/16") == ""


def test_parse_cidr_blank_or_malformed_is_empty():
    for bad in ("", "  ", "nonsense", "10.99.5.0/33"):
        assert tp.parse_pool_cidr(bad) == ""


def test_resolve_cidr_precedence_and_off():
    assert tp.resolve_pool_cidr("10.99.5.16/29", "10.99.5.0/24") == "10.99.5.16/29"
    assert tp.resolve_pool_cidr("", "10.99.5.0/24") == tp.derive_pool_cidr("10.99.5.0/24")
    # A refused range still yields a working derived pool rather than none.
    assert tp.resolve_pool_cidr("10.99.5.240-10.99.5.247", "10.99.5.0/24") == \
        tp.derive_pool_cidr("10.99.5.0/24")
    assert tp.resolve_pool_cidr("off", "10.99.5.0/24") == ""


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
