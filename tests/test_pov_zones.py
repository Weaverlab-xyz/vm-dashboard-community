"""Purdue zones on a cloud POV: the layout, the rules, and each cloud's translation.

The plant's promise is "no route in but the brokered one, and no route out", so what is
pinned here is mostly that promise surviving four translations:

  * **A guest's zone comes from its cell role alone**: ot-sim is the plant, ot-broker the
    DMZ, everything else (the POV broker included) enterprise.
  * **The plant has no internet egress and no public address**, on every cloud.
  * **Every port the wire-up builds a jump item for is one the plant admits** from the
    enterprise zone, where the PRA Gateway runs. A tunnel the firewall drops would fail
    at session launch, in front of the customer.
  * **Each cloud's rule set is complete**: Azure overrides its default VNet allow and
    internet egress, GCP writes an egress deny, OCI and AWS list only what is allowed.
  * **An unzoned template reaches every driver exactly as before**, and a zone that is
    missing from a network is refused rather than placed in the flat subnet.

Pure functions and fakes only. No cloud SDK, no network.

Runs under pytest, or standalone:
    python tests/test_pov_zones.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-zones")

from web_dashboard.services import (pov_cell_roles, pov_cloud_env,  # noqa: E402
                                    pov_cloud_template_service as tpl, pov_zones as z)

_NET = "10.20.0.0/16"


def _zone(name):
    return next(e for e in z.layout(_NET) if e["name"] == name)


def _rules(name, direction):
    return [r for r in _zone(name)["rules"] if r["direction"] == direction]


# ── the layout ───────────────────────────────────────────────────────────────

def test_a_guest_is_placed_by_its_cell_role_alone():
    assert z.zone_of("ot-sim") == z.PLANT
    assert z.zone_of("ot-broker") == z.DMZ
    for role in (None, "", "vyos", "anything"):
        assert z.zone_of(role) == z.ENTERPRISE, role


def test_three_subnets_and_enterprise_keeps_the_unzoned_subnet():
    nets = z.subnets(_NET)
    assert nets == {"enterprise": "10.20.0.0/24", "dmz": "10.20.1.0/24",
                    "plant": "10.20.2.0/24"}
    # The broker's range does not move when a template gains zones.
    assert nets["enterprise"] == pov_cloud_env.subnet_cidr(_NET)


def test_a_network_too_small_for_three_zones_is_refused():
    try:
        z.subnets("10.20.0.0/23")
        raise AssertionError("a /23 was accepted for three /24 zones")
    except z.ZoneError as exc:
        assert "/22" in str(exc)
    z.subnets("10.20.0.0/22")


# ── the rules ────────────────────────────────────────────────────────────────

def test_the_plant_has_no_internet_and_no_public_address():
    plant = _zone(z.PLANT)
    assert plant["internet_egress"] is False and plant["public_ip"] is False
    for rule in _rules(z.PLANT, "egress"):
        assert rule["peer"] != z.ANYWHERE, rule


def test_every_jump_item_port_is_admitted_from_enterprise():
    """The PRA Gateway runs on the POV broker, in enterprise. A tunnel to a port the plant
    refuses fails at session launch."""
    class _Sim:
        cell_role = "ot-sim"
    built = {pov_cell_roles.OT_HMI_PORT}
    built |= {spec["remote_port"] for _k, spec in pov_cell_roles.planned(_Sim())
              if spec["kind"] == "tunnel"}
    built.add(22)
    ent = z.subnets(_NET)["enterprise"]
    admitted = set()
    for rule in _rules(z.PLANT, "ingress"):
        if rule["peer"] == ent:
            admitted |= set(rule["ports"])
    assert built <= admitted, f"not admitted: {sorted(built - admitted)}"


def test_the_dmz_reaches_the_plant_only_on_ssh_and_the_hmi():
    dmz = z.subnets(_NET)["dmz"]
    from_dmz = [r for r in _rules(z.PLANT, "ingress") if r["peer"] == dmz]
    assert len(from_dmz) == 1 and from_dmz[0]["protocol"] == "tcp"
    assert from_dmz[0]["ports"] == sorted({22, pov_cell_roles.OT_HMI_PORT})


def test_the_dmz_egress_is_ports_not_the_whole_internet():
    for rule in _rules(z.DMZ, "egress"):
        if rule["peer"] == z.ANYWHERE:
            assert rule["protocol"] in ("tcp", "udp") and rule["ports"], rule


def test_the_entitle_agent_can_still_reach_enterprise_targets_over_ssh():
    dmz = z.subnets(_NET)["dmz"]
    assert any(r["peer"] == dmz and r["ports"] == [22]
               for r in _rules(z.ENTERPRISE, "ingress"))


# ── each cloud's translation ─────────────────────────────────────────────────

def test_aws_plant_group_has_no_egress_to_the_internet():
    from web_dashboard.services import pov_cloud_aws as aws
    egress = aws._zone_permissions(_zone(z.PLANT)["rules"], "egress")
    assert egress, "the plant group was given no egress at all, not even the POV network"
    for perm in egress:
        assert all(r["CidrIp"] != z.ANYWHERE for r in perm["IpRanges"]), perm
    ingress = aws._zone_permissions(_zone(z.PLANT)["rules"], "ingress")
    assert {p.get("FromPort") for p in ingress} >= set(z.plant_ports())


def test_aws_rule_descriptions_use_only_characters_ec2_accepts():
    """One apostrophe fails the whole AuthorizeSecurityGroupIngress call."""
    from web_dashboard.services import pov_cloud_aws as aws
    for zone in z.layout(_NET):
        for direction in ("ingress", "egress"):
            for perm in aws._zone_permissions(zone["rules"], direction):
                for rng in perm["IpRanges"]:
                    bad = set(rng["Description"]) - aws._SG_DESCRIPTION_OK
                    assert not bad, (zone["name"], rng["Description"], bad)


def test_azure_overrides_its_defaults_with_explicit_denies():
    from web_dashboard.services import pov_cloud_azure as az
    for zone in z.layout(_NET):
        rules = az.zone_security_rules(zone["rules"],
                                       internet_egress=zone["internet_egress"])
        for direction in ("Inbound", "Outbound"):
            prios = [r["priority"] for r in rules if r["direction"] == direction]
            assert len(prios) == len(set(prios)), f"{zone['name']} {direction} clash"
        denies = {r["direction"] for r in rules if r["access"] == "Deny"}
        assert "Inbound" in denies, f"{zone['name']} keeps AllowVnetInBound"
        assert ("Outbound" in denies) == (not zone["internet_egress"]), zone["name"]
        allows = [r["priority"] for r in rules if r["access"] == "Allow"]
        assert max(allows) < az._ZONE_DENY_PRIORITY < 65000


def test_gcp_rules_target_the_zone_tag_and_the_plant_denies_egress():
    from web_dashboard.services import pov_cloud_gcp as gcp
    names = []
    for zone in z.layout(_NET):
        specs = gcp.zone_firewalls(zone)
        names += [suffix for suffix, _ in specs]
        for _suffix, spec in specs:
            assert spec["target_tags"] == [gcp.zone_tag(zone["name"])]
        deny = [s for _n, s in specs if "denied" in s]
        assert bool(deny) == (not zone["internet_egress"]), zone["name"]
    assert len(names) == len(set(names)), "two zone rules share a name"
    # Inside the name budget: the longest env id the other suffixes allow still fits.
    longest = max(len(n) for n in names)
    assert longest <= max(len(s) for s in gcp._SUFFIXES) + 2, longest


def test_oci_plant_egress_names_no_outside_destination():
    from web_dashboard.services import pov_cloud_oci as oci
    ingress, egress = oci.zone_security_rules(_zone(z.PLANT)["rules"])
    assert egress and all(r["peer"] != z.ANYWHERE for r in egress)
    tcp_ports = {r["port"] for r in ingress if r["protocol"] == "6"}
    assert set(z.plant_ports()) <= tcp_ports


# ── placement and the template ───────────────────────────────────────────────

def test_placement_picks_the_zone_and_refuses_a_missing_one():
    net = {"subnet_id": "flat", "zones": {"plant": {"subnet_id": "p"}}}
    assert pov_cloud_env.placement(net, {"zone": "plant"}) == {"subnet_id": "p"}
    assert pov_cloud_env.placement(net, {"zone": ""}) == {}
    flat = {"subnet_id": "flat"}
    # Enterprise falls back to the one subnet, so a broker can be rebuilt on an older POV.
    assert pov_cloud_env.placement(flat, {"zone": "enterprise", "name": "b"}) == {}
    try:
        pov_cloud_env.placement(flat, {"zone": "plant", "name": "plc01"})
        raise AssertionError("a plant guest was placed in a flat network")
    except pov_cloud_env.CloudEnvError as exc:
        assert "plc01" in str(exc)


def test_vm_specs_carry_the_zone_only_on_a_zoned_template():
    class _Row:
        def __init__(self, name, role="target", cell_role=None):
            self.name, self.role, self.cell_role = name, role, cell_role
            self.os_family, self.image_ref, self.image_id = "linux", None, "img"
            self.instance_type, self.disk_gb = "t3.small", 20

    class _Tpl:
        purdue_zones = True

    rows = [_Row("plc01", cell_role="ot-sim"), _Row("edge"), _Row("dmz1", cell_role="ot-broker")]
    zoned = pov_cloud_env.vm_specs(_Tpl(), rows, "aws", "us-east-1")
    assert {s["name"]: s["zone"] for s in zoned} == {
        "plc01": "plant", "edge": "enterprise", "dmz1": "dmz"}
    _Tpl.purdue_zones = False
    flat = pov_cloud_env.vm_specs(_Tpl(), rows, "aws", "us-east-1")
    assert all(s["zone"] == "" for s in flat)


def test_a_zoned_template_refuses_a_network_too_small():
    try:
        tpl._check_zones(True, "10.30.0.0/24")
        raise AssertionError("a /24 zoned template was accepted")
    except tpl.CloudTemplateError as exc:
        assert "/22" in str(exc)
    tpl._check_zones(True, "")          # blank = the default /16
    tpl._check_zones(False, "10.30.0.0/24")


def test_only_a_zoned_template_passes_zones_to_the_driver():
    seen = []

    class _Driver:
        @staticmethod
        def default_region():
            return "r1"

        @staticmethod
        async def create_network(env_id, region, cidr, sub_cidr, **kw):
            seen.append(kw)
            return {"subnet_id": "s"}

        @staticmethod
        async def create_vms(env_id, region, specs, network):
            return []

        @staticmethod
        async def read_environment(env_id, region):
            return {"id": env_id}

    class _Tpl:
        name, region, network_cidr = "t", "", ""

    class _Vm:
        name, role, cell_role, os_family = "plc01", "target", "ot-sim", "linux"
        image_ref, image_id, instance_type, disk_gb = None, "img", "t3.small", 20

    saved = (pov_cloud_env.driver, pov_cloud_env.load_template)
    pov_cloud_env.driver = lambda cloud: _Driver
    try:
        for zoned in (False, True):
            _Tpl.purdue_zones = zoned
            pov_cloud_env.load_template = lambda tid, cloud: (_Tpl(), [_Vm()])
            asyncio.run(pov_cloud_env.create_environment("aws", "tid", "poc-1"))
    finally:
        pov_cloud_env.driver, pov_cloud_env.load_template = saved
    assert seen[0] == {}, "an unzoned template changed the driver call"
    assert [e["name"] for e in seen[1]["zones"]] == list(z.ZONES)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
