"""Unit tests: the network-tunnel address pool on the managed Azure Gateway VM.

A PRA **Network Tunnel** leases the operator a real address ON the target network.
The Gateway asks DHCP first, which Azure never answers (its DHCP only serves an
address already bound to a NIC), falls back to the pool configured in the Pathfinder
console, and then ARPs to validate the address. An address Azure does not know about
gets no ARP reply and the agent refuses it:

    Timeout: No valid ARP reply received from 10.99.5.202 in 2000ms
    Allocated IP address [10.99.5.202] not an existing Azure resource?  Is it
      configured as a secondary IP address on the virtual NIC of the Gateway VM?
    setup: Exception - Address: 0.0.0.0 not found

So the dashboard registers every pool address as a secondary ipconfig. What is pinned
here is the part that is easy to get wrong later:

  * the pool comes off the TOP of the subnet — Azure allocates dynamically from the
    bottom (.4 up), so the top stays clear of real hosts longest;
  * Azure's four reserved addresses (network, gateway, two DNS) and the broadcast are
    never handed out;
  * an explicit spec beats derivation, a malformed one falls BACK to derivation rather
    than leaving the Gateway with no pool at all;
  * an operator-supplied range is capped, because a pasted /16 would otherwise try to
    create 65k ipconfigs;
  * ``_ensure_tunnel_ipconfigs`` is additive and idempotent — it must never rewrite the
    primary ipconfig, which carries the Gateway's egress public IP.

Runs under pytest or standalone:  python tests/test_azure_jumpoint_tunnel_pool.py
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

from web_dashboard.services import azure_service as az  # noqa: E402


# ── deriving from the subnet ─────────────────────────────────────────────────

def test_derive_takes_the_top_of_the_subnet():
    pool = az.derive_tunnel_pool("10.99.5.0/24")
    assert pool == [f"10.99.5.{n}" for n in range(247, 255)]


def test_derive_never_returns_azure_reserved_or_broadcast():
    pool = az.derive_tunnel_pool("10.99.5.0/24")
    for reserved in ("10.99.5.0", "10.99.5.1", "10.99.5.2", "10.99.5.3", "10.99.5.255"):
        assert reserved not in pool


def test_derive_honours_an_explicit_size():
    assert az.derive_tunnel_pool("10.99.5.0/24", size=3) == \
        ["10.99.5.252", "10.99.5.253", "10.99.5.254"]


def test_derive_on_a_subnet_too_small_returns_what_it_can():
    # /29 = .0-.7; hosts() gives .1-.6, minus Azure's .1/.2/.3 leaves three.
    assert az.derive_tunnel_pool("10.99.5.0/29") == ["10.99.5.4", "10.99.5.5", "10.99.5.6"]


def test_derive_on_garbage_returns_empty_rather_than_raising():
    # A Gateway must still come up when the pool cannot be worked out.
    assert az.derive_tunnel_pool("not-a-subnet") == []
    assert az.derive_tunnel_pool("") == []


# ── an explicit spec ─────────────────────────────────────────────────────────

def test_parse_inclusive_range():
    assert az.parse_tunnel_pool("10.99.5.200-10.99.5.203") == \
        ["10.99.5.200", "10.99.5.201", "10.99.5.202", "10.99.5.203"]


def test_parse_range_given_backwards_is_still_read_as_a_range():
    assert az.parse_tunnel_pool("10.99.5.203-10.99.5.200") == \
        ["10.99.5.200", "10.99.5.201", "10.99.5.202", "10.99.5.203"]


def test_parse_cidr_excludes_network_and_broadcast():
    pool = az.parse_tunnel_pool("10.99.5.200/29")
    assert pool[0] == "10.99.5.201" and pool[-1] == "10.99.5.206"
    assert "10.99.5.200" not in pool and "10.99.5.207" not in pool


def test_parse_single_address():
    assert az.parse_tunnel_pool("10.99.5.200") == ["10.99.5.200"]


def test_parse_caps_a_huge_range():
    # The whole point: a pasted /16 must not become 65k ipconfigs.
    assert len(az.parse_tunnel_pool("10.99.0.0/16")) == az._TUNNEL_POOL_MAX


def test_parse_blank_or_malformed_is_empty_so_the_caller_derives():
    for bad in ("", "   ", "nonsense", "10.99.5.999-10.99.5.1000"):
        assert az.parse_tunnel_pool(bad) == []


# ── resolve: spec wins, derivation is the floor ──────────────────────────────

def test_resolve_prefers_an_explicit_spec():
    assert az.resolve_tunnel_pool("10.99.5.10-10.99.5.11", "10.99.5.0/24") == \
        ["10.99.5.10", "10.99.5.11"]


def test_resolve_falls_back_to_derivation_when_the_spec_is_blank():
    assert az.resolve_tunnel_pool("", "10.99.5.0/24") == az.derive_tunnel_pool("10.99.5.0/24")


def test_resolve_falls_back_to_derivation_when_the_spec_is_malformed():
    # A typo in an OPTIONAL setting must not cost the Gateway its pool entirely.
    assert az.resolve_tunnel_pool("10.99.5.oops", "10.99.5.0/24") == \
        az.derive_tunnel_pool("10.99.5.0/24")


def test_resolve_with_neither_is_empty_not_an_exception():
    assert az.resolve_tunnel_pool("", "") == []


# ── parsing an ARM subnet id ─────────────────────────────────────────────────

def test_subnet_parts_reads_an_arm_id():
    sid = ("/subscriptions/abc/resourceGroups/RG1/providers/Microsoft.Network/"
           "virtualNetworks/VNET1/subnets/jumpoint-subnet")
    assert az._subnet_parts(sid) == ("RG1", "VNET1", "jumpoint-subnet")


def test_subnet_parts_on_nonsense_is_blank_not_an_exception():
    assert az._subnet_parts("") == ("", "", "")
    assert az._subnet_parts("/subscriptions/abc") == ("", "", "")


# ── registering the ipconfigs ────────────────────────────────────────────────

class _IpCfg:
    def __init__(self, name, private_ip_address, primary=False):
        self.name = name
        self.private_ip_address = private_ip_address
        self.primary = primary


class _StubIpCfg:
    """Stand-in for azure.mgmt.network.models.NetworkInterfaceIPConfiguration, which
    `_ensure_tunnel_ipconfigs` constructs. The Azure SDK is not installed on every
    machine that runs this suite (azure_service imports it in a try/except and carries
    on), so install the stub ONLY when the real symbol is missing — that way this file
    passes identically with and without the SDK."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.primary = kw.get("primary", False)


if not hasattr(az, "NetworkInterfaceIPConfiguration"):
    az.NetworkInterfaceIPConfiguration = _StubIpCfg


class _Nic:
    def __init__(self, ip_configurations):
        self.ip_configurations = list(ip_configurations)


class _Poller:
    def __init__(self, v):
        self._v = v

    def result(self):
        return self._v


class _Nics:
    def __init__(self, nic, fail=False):
        self._nic = nic
        self.fail = fail
        self.written = None

    def get(self, rg, name):
        return self._nic

    def begin_create_or_update(self, rg, name, nic):
        if self.fail:
            raise RuntimeError("ARM said no")
        self.written = nic
        return _Poller(nic)


class _Net:
    def __init__(self, nics):
        self.network_interfaces = nics


def _primary():
    return _IpCfg("ipconfig1", "10.99.5.4", primary=True)


def test_ensure_adds_only_the_missing_addresses():
    nic = _Nic([_primary(), _IpCfg("ipconfig-tnl247", "10.99.5.247")])
    nics = _Nics(nic)
    got = az._ensure_tunnel_ipconfigs(_Net(nics), "RG", "gw-nic", "/sub/id",
                                      ["10.99.5.247", "10.99.5.248"])
    assert got == ["10.99.5.247", "10.99.5.248"]
    written = [c.private_ip_address for c in nics.written.ip_configurations]
    # .247 was already there and must not be duplicated; .248 is appended.
    assert written == ["10.99.5.4", "10.99.5.247", "10.99.5.248"]


def test_ensure_never_touches_the_primary_ipconfig():
    # The primary carries the Gateway's stable egress public IP. Rewriting it would
    # change the address the Rancher/Portainer node firewalls are allow-listing.
    nic = _Nic([_primary()])
    nics = _Nics(nic)
    az._ensure_tunnel_ipconfigs(_Net(nics), "RG", "gw-nic", "/sub/id", ["10.99.5.254"])
    first = nics.written.ip_configurations[0]
    assert first.name == "ipconfig1" and first.private_ip_address == "10.99.5.4"
    assert first.primary is True


def test_ensure_is_a_noop_when_everything_is_already_registered():
    nic = _Nic([_primary(), _IpCfg("ipconfig-tnl254", "10.99.5.254")])
    nics = _Nics(nic)
    got = az._ensure_tunnel_ipconfigs(_Net(nics), "RG", "gw-nic", "/sub/id", ["10.99.5.254"])
    assert got == ["10.99.5.254"]
    assert nics.written is None, "no ARM write when there is nothing to add"


def test_ensure_with_an_empty_pool_does_nothing():
    nics = _Nics(_Nic([_primary()]))
    assert az._ensure_tunnel_ipconfigs(_Net(nics), "RG", "gw-nic", "/sub/id", []) == []
    assert nics.written is None


def test_ensure_reports_empty_when_arm_refuses_rather_than_raising():
    # Protocol tunnels and every other Gateway function work without the pool, so a
    # failure here must not take the Gateway down with it.
    nics = _Nics(_Nic([_primary()]), fail=True)
    assert az._ensure_tunnel_ipconfigs(_Net(nics), "RG", "gw-nic", "/sub/id",
                                       ["10.99.5.254"]) == []


def test_ensure_names_each_ipconfig_after_its_last_octet():
    nics = _Nics(_Nic([_primary()]))
    az._ensure_tunnel_ipconfigs(_Net(nics), "RG", "gw-nic", "/sub/id", ["10.99.5.254"])
    added = nics.written.ip_configurations[-1]
    assert added.name == "ipconfig-tnl254"
    assert added.private_ip_allocation_method == "Static"


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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
