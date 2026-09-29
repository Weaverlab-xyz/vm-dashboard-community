"""Network-tunnel address pools, shared by all three clouds.

A PRA **Network Tunnel** leases the operator a real address ON the target network for
the life of the session. The Gateway asks DHCP for it first, which no cloud will answer
— cloud DHCP only serves an address already bound to a NIC — so it falls back to the
"Managed IP Addresses for Protocol Tunnel" pool configured on the Gateway in the
Pathfinder console, and then **ARPs to validate the address it picked**. An address the
fabric does not know about gets no ARP reply, and the agent refuses it:

    Timeout: No valid ARP reply received from 10.99.5.202 in 2000ms
    Allocated IP address [10.99.5.202] not an existing Azure resource?  Is it
      configured as a secondary IP address on the virtual NIC of the Gateway VM?
    setup: Exception - Address: 0.0.0.0 not found

So every pool address must be registered with the fabric. Each cloud spells that
differently — Azure secondary ipconfigs, AWS secondary private IPs on the ENI, GCP an
alias IP range — but *choosing* the addresses is identical everywhere, and that is what
lives here. A per-cloud copy of this arithmetic would drift, which is the only reason
this module exists rather than three private helpers.

**The pool must MATCH the Pathfinder console.** The dashboard can neither read nor write
that setting, so a mismatch surfaces only as the agent failing its ARP check. Callers
log the resolved pool and return it so an operator can copy it across.

Two shapes, because the clouds differ in what they accept:

* Azure and AWS register **individual addresses** -> ``derive_pool`` / ``parse_pool``
  return a list.
* GCP registers an **alias IP range**, which is a CIDR block, not a list ->
  ``derive_pool_cidr`` / ``parse_pool_cidr`` return an aligned prefix. A start-end range
  is refused there rather than silently widened, because widening hands you a pool that
  covers addresses you did not ask for and may collide with live hosts.

Reserved addresses differ too, and are parameters rather than assumptions.
``ipaddress.ip_network().hosts()`` already drops the network and broadcast addresses;
``reserved_head`` / ``reserved_tail`` drop what each cloud takes on top of that:

| Cloud | Also reserved            | head | tail |
|-------|--------------------------|------|------|
| Azure | ``.1`` ``.2`` ``.3``     | 3    | 0    |
| AWS   | ``.1`` ``.2`` ``.3``     | 3    | 0    |
| GCP   | ``.1``, second-to-last   | 1    | 1    |

Pools are taken from the **TOP** of the subnet in every cloud: all three allocate
dynamic addresses from the bottom (``.4`` upward), so the top stays clear of real hosts
the longest.
"""
import ipaddress
import logging

logger = logging.getLogger(__name__)

# How many addresses to carve out when deriving. Each is one registration on the NIC and
# one concurrent network-tunnel session; 8 is generous for a demo gateway and stays far
# below every cloud's per-NIC limit.
POOL_SIZE = 8
# Hard ceiling for an OPERATOR-SUPPLIED pool. Without it, pasting a /16 into the config
# field would try to create 65k addresses — a very expensive typo.
POOL_MAX = 32

# Per-cloud reserved-address counts, beyond the network/broadcast that hosts() drops.
RESERVED = {
    "azure": (3, 0),   # .1 gateway, .2/.3 DNS
    "aws":   (3, 0),   # .1 router, .2 DNS, .3 reserved for future use
    "gcp":   (1, 1),   # .1 gateway, second-to-last reserved
}

# The sentinel that turns the pool OFF for a caller that must not have one. Spelled
# explicitly rather than relying on a blank, because blank means "derive one".
DISABLED = "off"


def _reserved_for(cloud: str) -> tuple[int, int]:
    return RESERVED.get((cloud or "").lower(), (3, 0))


def derive_pool(subnet_cidr: str, cloud: str = "azure", size: int = POOL_SIZE) -> list[str]:
    """The last ``size`` usable addresses of ``subnet_cidr``, as dotted strings.

    A subnet too small to give ``size`` addresses yields however many it can rather
    than raising; an unparseable prefix yields [] so the gateway still comes up.
    """
    head, tail = _reserved_for(cloud)
    try:
        net = ipaddress.ip_network(subnet_cidr, strict=False)
    except ValueError:
        logger.warning("tunnel-pool: cannot parse subnet prefix %r", subnet_cidr)
        return []
    usable = list(net.hosts())[head:]
    if tail:
        usable = usable[:-tail]
    return [str(ip) for ip in usable[-size:]] if usable else []


def parse_pool(spec: str) -> list[str]:
    """Expand an operator-supplied pool into addresses. Accepts ``a.b.c.d-a.b.c.e``
    (inclusive), a CIDR, or a single address. Returns [] for anything unparseable —
    the caller falls back to deriving, because refusing to build the Gateway over a
    malformed optional setting would be the worse failure."""
    spec = (spec or "").strip()
    if not spec or spec.lower() == DISABLED:
        return []
    try:
        if "-" in spec:
            lo_s, hi_s = (p.strip() for p in spec.split("-", 1))
            lo, hi = ipaddress.ip_address(lo_s), ipaddress.ip_address(hi_s)
            if hi < lo:
                lo, hi = hi, lo
            out = [str(ipaddress.ip_address(i)) for i in range(int(lo), int(hi) + 1)]
        elif "/" in spec:
            out = [str(ip) for ip in ipaddress.ip_network(spec, strict=False).hosts()]
        else:
            out = [str(ipaddress.ip_address(spec))]
    except ValueError:
        logger.warning("tunnel-pool: cannot parse pool spec %r — deriving instead", spec)
        return []
    if len(out) > POOL_MAX:
        logger.warning("tunnel-pool: spec %r expands to %d addresses; capping at %d",
                       spec, len(out), POOL_MAX)
        out = out[:POOL_MAX]
    return out


def resolve_pool(spec: str, subnet_cidr: str, cloud: str = "azure",
                 size: int = POOL_SIZE) -> list[str]:
    """The pool to register: an explicit ``spec`` when it parses, else derived from the
    subnet. ``spec == "off"`` disables the pool entirely and derives nothing — that is
    how a caller that must not own a pool (a per-VM paired gateway) opts out.

    Keeping both behind one call is what stops the two paths drifting."""
    if (spec or "").strip().lower() == DISABLED:
        return []
    return parse_pool(spec) or derive_pool(subnet_cidr, cloud, size)


# ── GCP: an alias IP range is a CIDR block, not a list ───────────────────────

def derive_pool_cidr(subnet_cidr: str, cloud: str = "gcp",
                     size: int = POOL_SIZE) -> str:
    """The highest aligned prefix of at least ``size`` addresses that fits inside
    ``subnet_cidr`` without touching that cloud's reserved addresses. "" when the
    subnet is unparseable or too small.

    Aligned, because an alias IP range must be a real prefix — you cannot hand GCP
    "the last 8 addresses" unless they happen to start on an 8-boundary. For a /24
    with GCP's reserved second-to-last address, the top aligned /29 that stays clear
    is ``.240/29`` rather than ``.248/29``.
    """
    head, tail = _reserved_for(cloud)
    try:
        net = ipaddress.ip_network(subnet_cidr, strict=False)
    except ValueError:
        logger.warning("tunnel-pool: cannot parse subnet prefix %r", subnet_cidr)
        return ""
    usable = list(net.hosts())[head:]
    if tail:
        usable = usable[:-tail]
    if not usable:
        return ""
    lo, hi = int(usable[0]), int(usable[-1])
    # Smallest prefix length that holds `size` addresses (8 -> /29 on IPv4), then
    # SHRINK if it will not fit. A /28 gateway subnet cannot seat an aligned /29 once
    # the reserved addresses are excluded, and half a pool beats refusing one.
    ideal = max(net.prefixlen, net.max_prefixlen - max(size - 1, 1).bit_length())
    for bits in range(ideal, net.max_prefixlen - 1):   # ... down to a /30 (2 usable)
        block = 1 << (net.max_prefixlen - bits)
        # Walk down from the top usable address to the highest aligned block that fits.
        start = ((hi + 1 - block) // block) * block
        while start >= lo:
            if start + block - 1 <= hi:
                return f"{ipaddress.ip_address(start)}/{bits}"
            start -= block
    return ""


def parse_pool_cidr(spec: str) -> str:
    """An operator-supplied GCP pool. **CIDR only** — a start-end range is refused
    rather than widened to the enclosing prefix, because widening silently hands you
    addresses you did not ask for. Returns "" so the caller derives instead."""
    spec = (spec or "").strip()
    if not spec or spec.lower() == DISABLED:
        return ""
    if "-" in spec:
        logger.warning("tunnel-pool: %r is a range; GCP alias IP ranges must be a CIDR "
                       "(e.g. 10.99.5.240/29) — deriving instead", spec)
        return ""
    try:
        net = ipaddress.ip_network(spec, strict=False)
    except ValueError:
        logger.warning("tunnel-pool: cannot parse pool CIDR %r — deriving instead", spec)
        return ""
    if net.num_addresses > POOL_MAX:
        logger.warning("tunnel-pool: %r holds %d addresses; refusing anything above %d "
                       "— deriving instead", spec, net.num_addresses, POOL_MAX)
        return ""
    return str(net)


def resolve_pool_cidr(spec: str, subnet_cidr: str, cloud: str = "gcp",
                      size: int = POOL_SIZE) -> str:
    """The GCP alias range to attach: an explicit CIDR when it parses, else derived.
    ``spec == "off"`` disables it entirely."""
    if (spec or "").strip().lower() == DISABLED:
        return ""
    return parse_pool_cidr(spec) or derive_pool_cidr(subnet_cidr, cloud, size)
