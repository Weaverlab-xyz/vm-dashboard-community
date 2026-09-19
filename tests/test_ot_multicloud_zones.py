"""The OT cell's Purdue zone, on all three clouds, holds the same shape.

The GCP zone is VPC firewall rules on network tags. AWS and Azure say the same thing
with primitives that behave differently, and each difference is a way to ship a zone
that reads correctly and enforces nothing:

  * **AWS security groups union their allows.** Attaching a restrictive group to an
    instance that still carries a permissive one restricts precisely nothing, while the
    console shows the zone sitting there. So the zone has to REPLACE the instance's
    groups, not join them.
  * **AWS creates a group with egress already open.** A new group allows everything
    outbound to 0.0.0.0/0. The plant's air gap is therefore made true by REVOKING that
    rule -- declining to add one leaves the cell with a full route out.
  * **Azure gives every VM default outbound access.** A cell with no public IP reaches
    the internet anyway until an explicit outbound Deny exists. On Azure that Deny is
    not hardening on top of an air gap; it IS the air gap.
  * **Azure NSG rules are ordered.** The broker's outbound allows have to outrank its
    outbound deny, or the agent has no channel.

Every one of those failures is silent -- the deploy goes green, the console looks
right, and the demo's central claim is false. None of them is visible in a unit test
that mocks the cloud, so these are structural: they read the source.

Run: python tests/test_ot_multicloud_zones.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OT = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_AWS = os.path.join(_ROOT, "web_dashboard", "services", "aws_service.py")
_AZURE = os.path.join(_ROOT, "web_dashboard", "services", "azure_service.py")
_AWS_VM = os.path.join(_ROOT, "web_dashboard", "services", "aws_vm_service.py")
_AZURE_VM = os.path.join(_ROOT, "web_dashboard", "services", "azure_vm_service.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "ot.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _fn_src(path, name):
    """One function's source by AST line span.

    The AST rather than a regex because most of these are `async def`, which the
    obvious `def NAME\\(.*?\\n\\ndef ` pattern does not match -- it would silently run
    to the end of the file and make every assertion below pass on unrelated code.
    """
    src = _read(path)
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


def _fn_body(path, name):
    """One function's CODE, docstring stripped.

    Needed for every absence check here: these functions explain the trap they avoid
    in prose, so a raw-text search for "egress" finds the comment that says egress must
    be revoked and passes on code that never revokes it.
    """
    src = _read(path)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


# ── AWS: the two traps its primitive sets ────────────────────────────────────

def test_the_aws_cell_zone_asks_for_no_egress_at_all():
    """The plant floor's air gap, in the one place it is expressed."""
    body = _fn_body(_OT, "_wire_zones_aws")
    assert re.search(r"name=names\['cell'\][^)]*egress=\[\]", body, re.S), (
        "the cell's security group is not created with an EMPTY egress set — the plant "
        "floor would keep whatever route out the group was created with")


def test_the_aws_zone_revokes_egress_it_did_not_ask_for():
    """Declining to ADD an egress rule is not the same as having none: AWS creates a
    security group allowing all egress, so the air gap exists only if that is revoked."""
    body = _fn_body(_AWS, "_ensure_ot_zone_security_group_sync")
    assert "revoke_security_group_egress" in body, (
        "nothing revokes egress, so a freshly created zone group still carries the "
        "allow-all AWS gave it and the cell has a full route out")
    assert "have_egress - want_egress" in body, (
        "the egress revoke is not driven by the difference between what the group has "
        "and what the zone wants, so the default allow-all would survive")


def test_the_aws_zone_replaces_the_instances_groups_rather_than_joining_them():
    """Security groups union their allows. A zone that is merely ATTACHED restricts
    nothing at all while looking exactly right in the console."""
    body = _fn_body(_OT, "_wire_zones_aws")
    assert "set_instance_security_groups" in body, (
        "the zone is never bound to the instance, so it is a group nothing is in")
    helper = _fn_src(_AWS, "set_instance_security_groups")
    assert "modify_instance_attribute" in _fn_body(_AWS, "_set_instance_security_groups_sync")
    assert "Replace, not add" in helper or "REPLACE" in helper, (
        "the helper no longer documents that it replaces rather than adds — that is "
        "the whole reason it exists")


def test_the_aws_broker_may_reach_the_entitle_ports_and_dns_and_nothing_else():
    body = _fn_body(_OT, "_wire_zones_aws")
    assert "ENTITLE_AGENT_PORTS" in body, (
        "the broker's egress does not come from the agent's port list — 8080 is the "
        "agent's primary channel, not telemetry, and dropping it is silent")
    assert "_AWS_RESOLVER_CIDR" in body, (
        "the broker gets no DNS hole, so it cannot resolve the endpoint it is allowed "
        "to reach")
    assert "0.0.0.0/0" not in body, (
        "a literal 0.0.0.0/0 in the AWS wiring — the open-ports escape hatch belongs "
        "in _entitle_egress_targets, where it is recorded as the provenance")


# ── Azure: the posture nobody had ────────────────────────────────────────────

def test_the_azure_cell_denies_outbound_explicitly():
    """The finding this phase exists for. Azure's default outbound access gives a VM
    with no public IP a route to the internet, and nothing created an NSG for a cell at
    all — so until this rule exists the air gap is a sentence in the docs."""
    body = _fn_body(_OT, "_wire_zones_azure")
    # EVERY egress-deny in the function, not the first one found: the broker's rule
    # list is built before the cell's, so a regex that stops at the first match reads
    # the broker's deny and passes while the CELL's has been turned into an Allow —
    # which is exactly the mutation this file was written to catch.
    denies = re.findall(r"'name': 'egress-deny'.{0,220}", body, re.S)
    assert len(denies) == 2, (
        f"expected an outbound deny in both the broker's and the cell's rule list, "
        f"found {len(denies)}")
    for rule in denies:
        assert "'access': 'Deny'" in rule, (
            "an egress-deny rule is not actually a Deny — on Azure that rule IS the "
            "air gap, because default outbound access gives a VM with no public IP a "
            "route to the internet")
        assert "'direction': 'Outbound'" in rule


def test_the_azure_outbound_allows_outrank_the_outbound_deny():
    """Azure evaluates by priority. An Entitle allow numbered above the deny is a
    broker with no channel, and the symptom is an agent that never connects."""
    src = _read(_OT)
    prio = re.search(r"_AZ_PRIO = \{(.+?)\}", src, re.S)
    assert prio, "_AZ_PRIO is gone; the priorities are no longer stated in one place"
    table = dict(re.findall(r'"([a-z_]+)":\s*(\d+)', prio.group(1)))
    for allow in ("egress_entitle", "egress_dns_udp", "egress_dns_tcp"):
        assert int(table[allow]) < int(table["egress_deny"]), (
            f"{allow} ({table[allow]}) does not outrank egress_deny "
            f"({table['egress_deny']}) — the broker would have no way out")
    assert int(table["ingress_allow"]) < int(table["ingress_deny"]), (
        "the ingress allow does not outrank the ingress deny — nothing could reach "
        "the cell, including the Gateway brokering the session to fix it")


def test_the_azure_priorities_are_unique_within_a_direction():
    """Azure rejects two rules at the same priority in the same direction, and the
    failure arrives as a 400 in the middle of wiring a cell that is already up."""
    src = _read(_OT)
    prio = re.search(r"_AZ_PRIO = \{(.+?)\}", src, re.S)
    table = dict(re.findall(r'"([a-z_]+)":\s*(\d+)', prio.group(1)))
    outbound = [v for k, v in table.items() if k.startswith("egress")]
    inbound = [v for k, v in table.items() if k.startswith("ingress")]
    assert len(set(outbound)) == len(outbound), f"duplicate outbound priorities: {table}"
    assert len(set(inbound)) == len(inbound), f"duplicate inbound priorities: {table}"


def test_the_azure_zone_is_attached_to_the_nic():
    body = _fn_body(_OT, "_wire_zones_azure")
    assert "attach_nsg_to_vm" in body, (
        "the NSG is written but never attached, so it governs nothing")


def test_the_azure_dns_hole_is_the_platform_resolver():
    body = _fn_body(_OT, "_wire_zones_azure")
    assert "_AZURE_RESOLVER_TAG" in body, (
        "the broker's DNS hole is not the AzurePlatformDNS service tag — a public "
        "resolver would be a second destination the plant boundary has to admit")


# ── Neither cloud re-zones a cell that never asked ───────────────────────────

def test_aws_and_azure_zoning_needs_a_broker():
    """Protects the two clouds with live miles on them. Their zoning REPLACES an
    instance's groups or a NIC's NSG, so it may not arrive under a deploy that merely
    has the GCP toggle switched on."""
    body = _fn_body(_OT, "_wire_cell")
    for cloud in ("aws", "azure"):
        assert re.search(rf"cloud == '{cloud}' and broker_id", body), (
            f"{cloud} zoning is not gated on a broker being present — an existing "
            f"{cloud} cell's network posture would change underneath it")


def test_the_gcp_zone_is_not_gated_on_a_broker():
    """The mirror: GCP's rules are independent of the instance and predate the broker,
    so gating them would silently drop the hardening those cells already have."""
    body = _fn_body(_OT, "_wire_cell")
    assert re.search(r"cloud == 'gcp':\s*\n\s*firewall_note = await _wire_purdue_firewall",
                     body), (
        "the GCP zone now depends on something other than the Purdue toggle")


# ── The broker is a broker, everywhere ───────────────────────────────────────

def test_every_cloud_marks_the_broker_row_as_a_broker_not_a_cell():
    """`ot_cell` is what the cells list and the home tile count. A broker carrying it
    would double every number an operator reads."""
    src = _read(_API)
    assert src.count('"ot_broker":') == 3, (
        f"expected one ot_broker child per cloud, found {src.count('ot_broker')}")
    for fn in ("deploy_cell", "deploy_cell_aws", "deploy_cell_azure"):
        body = _fn_src(_API, fn)
        if '"ot_broker"' not in body:
            continue
        broker_block = body.split('"ot_broker"', 1)[1][:400]
        assert '"ot_cell"' not in broker_block, f"{fn}: the broker row also claims ot_cell"


def test_every_cloud_refuses_the_broker_before_launching_anything():
    for fn in ("deploy_cell", "deploy_cell_aws", "deploy_cell_azure"):
        body = _fn_src(_API, fn)
        if "register_in_entitle" not in body or "broker" not in body:
            continue
        assert "in_plant_agent_problem" in body, (
            f"{fn} creates a broker without the preflight that names the remedy")


def test_the_broker_shape_floor_is_per_cloud():
    """gateway_mem_mb only knows GCP families. A t3.large scored by the GCP table is
    an unknown, and an unknown must not be refused on a guess -- nor must a genuinely
    small AWS shape sail through on one."""
    body = _fn_body(_OT, "broker_shape_problem")
    assert "aws_gateway_mem_mb" in body and "azure_gateway_mem_mb" in body, (
        "the broker's memory floor is still scored with the GCP table on every cloud")


# ── Teardown removes what wiring created ─────────────────────────────────────

def test_both_clouds_remove_the_zone_and_the_token_on_destroy():
    for path, key in ((_AWS_VM, "ot_zone_group"), (_AZURE_VM, "ot_zone_nsg")):
        src = _read(path)
        assert key in src, (
            f"{os.path.basename(path)} never removes {key} — on AWS an unreferenced "
            f"security group blocks a later VPC delete with an opaque error")
        assert "destroy_agent_token" in src, (
            f"{os.path.basename(path)} leaves the plant's Entitle agent token in the "
            f"tenant, where nothing else will ever collect it")


def test_the_aws_teardown_can_still_find_the_group():
    """An ec2_deploy records the subnet and not the VPC, and a security group is found
    by (name, VPC). Without the recorded id the group is created and then orphaned."""
    assert "ot_zone_vpc_id" in _read(_AWS_VM), (
        "the AWS teardown does not read the recorded VPC id")
    assert "ot_zone_vpc_id" in _fn_body(_OT, "_wire_zones_aws"), (
        "the wiring does not record the VPC id it resolved")
    assert "subnet_vpc_id" in _read(_AWS), "aws_service has no subnet -> VPC resolver"


# ── The zone can always name the Gateway ─────────────────────────────────────

def test_each_cloud_refuses_when_it_cannot_name_the_gateway():
    """The expensive direction. A zone applied without a Gateway source denies the
    Gateway along with everything else, and the cell is unreachable by the only path
    the demo has -- including the session an operator would use to undo it."""
    body = _fn_body(_OT, "in_plant_agent_problem")
    assert "bt_ecs_jumpoint_security_group_id" in body
    assert "azure_jumpoint_name" in body
    for fn in ("_wire_zones_aws", "_wire_zones_azure"):
        zone = _fn_body(_OT, fn)
        assert "skipped" in zone, (
            f"{fn} does not bail out when the Gateway source is missing — it would "
            f"write a zone that locks the cell away from the Gateway")


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
    print(f"{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
