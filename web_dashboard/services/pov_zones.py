"""Purdue zones for a cloud POV: three subnets and the flows allowed between them.

The OT demo cell's argument is the network's shape: the plant has no route in and no
route out, and everything reaches it through one brokered path. A POV built from one
subnet cannot show that, because every guest sees every other guest. A template with
``purdue_zones`` on builds three subnets instead, and each cloud driver translates the
rules below into its own firewall.

**The zone comes from the guest's cell role, never from a separate field.** A plant zone
holding an ordinary target, or an OT simulator left outside it, would be a template that
contradicts the story it was built to tell. So ``ot-sim`` is the plant, ``ot-broker`` is
the DMZ, and everything else, including the POV broker VM that runs the dashboard agent
and the PRA Gateway, is the enterprise zone.

The zones and the allowed flows:

* **enterprise** (level 4/5): the POV broker and ordinary targets. Unrestricted egress,
  because the broker pulls images and dials the dashboard, PRA and Password Safe.
  Ingress from itself, and SSH from the DMZ, where the Entitle agent mints accounts.
* **dmz** (level 3.5): the OT broker, with the Entitle agent and the FUXA adapter.
  Ingress from itself and SSH from enterprise (the broker agent's Ansible runs).
  Egress on web, Entitle and DNS ports only, plus anything inside the POV network.
* **plant** (levels 0-2): the OT simulator. Ingress from itself, from enterprise only on
  the plant ports (SSH, the HMI, the PLC protocols: the PRA Gateway's jump items), and
  from the DMZ only on SSH and the HMI (the Entitle agent and the adapter). **No internet
  egress and no public address.** Its egress is limited to the POV network, which
  stateful firewalls only need for the replies they already allow.

Rules are cloud-neutral dicts. A driver adds its cloud's default deny, and never needs to
know why a rule exists. All four clouds' firewalls are stateful, so a reply to an allowed
connection is never a rule here.

This module holds no session and makes no call. ``tests/test_pov_zones.py`` asserts on
what it returns.
"""
from __future__ import annotations

import ipaddress

ENTERPRISE = "enterprise"
DMZ = "dmz"
PLANT = "plant"
ZONES = (ENTERPRISE, DMZ, PLANT)

LABELS = {
    ENTERPRISE: "Enterprise (POV broker and targets)",
    DMZ: "DMZ (OT broker, Entitle agent)",
    PLANT: "Plant (OT simulator)",
}

# Each zone takes one /24, in this order, from the template's network. So the network has
# to hold three of them, which is the check ``check_network`` makes.
ZONE_PREFIX = 24
MIN_NETWORK_PREFIX = 22

# What enterprise may reach in the plant: SSH (Shell Jump, the broker agent's Ansible,
# Password Safe rotation through the Resource Broker), the FUXA HMI's Web Jump, and the
# PLC protocol tunnels. Read off the same tables the cell role's PRA items are built from,
# so a tunnel the wire-up creates is never one the firewall drops.
SSH = 22
DMZ_TO_PLANT_PORTS = (SSH,)        # + the HMI port, added in plant_ports_from_dmz()

# DMZ egress to the outside: HTTPS (the Entitle agent's API, k3s, helm and the chart
# repository), the Entitle agent's second port, and DNS. Ports, not destinations: the
# installs reach several CDNs whose addresses change, so pinning destinations here would
# be a list that is wrong by the next release.
DMZ_EGRESS_TCP = (53, 443, 8080)
DMZ_EGRESS_UDP = (53,)

ANYWHERE = "0.0.0.0/0"


class ZoneError(Exception):
    """A zoned template that cannot be built. The message names the remedy."""


def zone_of(cell_role: str | None) -> str:
    """The zone a guest lands in, decided by its cell role alone."""
    role = (cell_role or "").strip().lower()
    if role == "ot-sim":
        return PLANT
    if role == "ot-broker":
        return DMZ
    return ENTERPRISE


def check_network(network_cidr: str) -> None:
    """Refuse a network that cannot hold the three zone subnets."""
    net = ipaddress.ip_network(network_cidr, strict=False)
    if net.prefixlen > MIN_NETWORK_PREFIX:
        raise ZoneError(
            f"a Purdue-zoned template needs room for three /{ZONE_PREFIX} subnets, so "
            f"its network must be /{MIN_NETWORK_PREFIX} or larger; {network_cidr} is "
            f"/{net.prefixlen}")


def subnets(network_cidr: str) -> dict:
    """``{zone: cidr}`` — the first three /24s of the network, in :data:`ZONES` order.

    Enterprise takes the first /24, which is the subnet an unzoned POV on the same
    network would have used, so the broker's address range does not move when a template
    gains zones.
    """
    check_network(network_cidr)
    net = ipaddress.ip_network(network_cidr, strict=False)
    blocks = net.subnets(new_prefix=ZONE_PREFIX)
    return {zone: str(next(blocks)) for zone in ZONES}


def plant_ports() -> list:
    """The TCP ports enterprise may reach in the plant, sorted and de-duplicated."""
    from . import pov_cell_roles
    ports = {SSH, pov_cell_roles.OT_HMI_PORT}
    for key, spec in pov_cell_roles.planned(_OtSimProbe()):
        if spec["kind"] == "tunnel":
            ports.add(int(spec["remote_port"]))
    return sorted(ports)


def plant_ports_from_dmz() -> list:
    from . import pov_cell_roles
    return sorted(set(DMZ_TO_PLANT_PORTS) | {pov_cell_roles.OT_HMI_PORT})


class _OtSimProbe:
    """Just enough of a VM row for ``pov_cell_roles.planned`` to list an ot-sim's items."""
    cell_role = "ot-sim"


def _rule(direction: str, protocol: str, ports, peer: str, why: str) -> dict:
    return {"direction": direction, "protocol": protocol,
            "ports": sorted({int(p) for p in (ports or ())}),
            "peer": peer, "why": why}


def rules(network_cidr: str) -> dict:
    """``{zone: {"rules": [...], "internet_egress": bool, "public_ip": bool}}``.

    ``rules`` are ALLOW rules only; everything else in that direction is denied, and the
    driver says so in its own terms. ``ports`` empty means every port of ``protocol``,
    and ``protocol`` "all" means every protocol. ``peer`` is a CIDR: the source of an
    ingress rule, the destination of an egress one.

    ``internet_egress`` False means the driver must DENY egress beyond the rules, which
    on Azure and AWS means overriding a default allow. ``public_ip`` False means the
    zone's guests get no public address, and the subnet refuses one where the cloud can
    say so.
    """
    nets = subnets(network_cidr)
    ent, dmz, plant = nets[ENTERPRISE], nets[DMZ], nets[PLANT]
    whole = str(ipaddress.ip_network(network_cidr, strict=False))
    return {
        ENTERPRISE: {
            "internet_egress": True,
            "public_ip": True,
            "rules": [
                _rule("ingress", "all", (), ent, "within the enterprise zone"),
                _rule("ingress", "tcp", (SSH,), dmz,
                      "the Entitle agent mints accounts over SSH"),
                _rule("egress", "all", (), ANYWHERE,
                      "the broker dials the dashboard, PRA and Password Safe"),
            ],
        },
        DMZ: {
            "internet_egress": False,
            "public_ip": True,
            "rules": [
                _rule("ingress", "all", (), dmz, "within the DMZ"),
                _rule("ingress", "tcp", (SSH,), ent,
                      "the broker agent's Ansible runs and the Shell Jump"),
                _rule("egress", "tcp", DMZ_EGRESS_TCP, ANYWHERE,
                      "the Entitle agent, its installs, and DNS"),
                _rule("egress", "udp", DMZ_EGRESS_UDP, ANYWHERE, "DNS"),
                _rule("egress", "all", (), whole, "inside the POV network"),
            ],
        },
        PLANT: {
            "internet_egress": False,
            "public_ip": False,
            "rules": [
                _rule("ingress", "all", (), plant, "within the plant"),
                _rule("ingress", "tcp", plant_ports(), ent,
                      "the PRA Gateway's jump items and the broker agent"),
                _rule("ingress", "tcp", plant_ports_from_dmz(), dmz,
                      "the Entitle agent and the HMI adapter"),
                _rule("egress", "all", (), whole, "inside the POV network only"),
            ],
        },
    }


def layout(network_cidr: str) -> list:
    """What a driver's ``create_network`` is given: one entry per zone, in order."""
    nets = subnets(network_cidr)
    spec = rules(network_cidr)
    return [{"name": zone, "cidr": nets[zone], **spec[zone]} for zone in ZONES]
