"""The OT cell's Purdue-zone firewall rules (services/ot_service).

The GCP cell has always carried the `ot-sim` network tag that the docs called "the
forward hook for Purdue-zone firewalling", and nothing consumed it: the cell's
isolation was the sandbox's posture (no NAT on the VM subnet, no public IP) rather
than anything the cell owned. That posture is one toggle from evaporating —
`gcp_vm_nat_enabled` adds a priority-900 EGRESS ALLOW on the VM network tag every
cell also carries, so enabling on-demand egress for one ordinary VM gives every
plant cell in the sandbox a route to the internet, with nothing in the UI saying so.

These pin the two properties that make the rules safe to turn on:

* the priorities really do outrank the rules they are meant to beat, and
* the catch-all ingress DENY is never created without its paired Gateway ALLOW —
  the failure that would fence a cell away from the very Gateway brokering the
  session you would use to fix it.

Run: python tests/test_ot_purdue_firewall.py   (or under pytest)
"""
import ast
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OT = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_GCP_VM = os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py")
_GCP = os.path.join(_ROOT, "web_dashboard", "services", "gcp_service.py")

# The standing rules the cell's own rules have to outrank, from gcp_nat_service and
# the sandbox setup script. Repeated here so a change to either side breaks a test
# rather than silently un-fencing every cell.
ON_DEMAND_EGRESS_ALLOW_PRIORITY = 900
SANDBOX_VM_EGRESS_DENY_PRIORITY = 1000


def _load():
    spec = importlib.util.spec_from_file_location("ot_service_purdue_under_test", _OT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fn_src(path, name):
    src = open(path, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{os.path.basename(path)}: {name} not found")


def test_the_priorities_outrank_the_rules_they_must_beat():
    ot = _load()
    assert ot._PURDUE_EGRESS_PRIORITY < ON_DEMAND_EGRESS_ALLOW_PRIORITY, (
        "the cell's egress deny must outrank the on-demand NAT ALLOW, or switching on "
        "gcp_vm_nat_enabled for an unrelated VM opens every plant cell's route out")
    assert ot._PURDUE_EGRESS_PRIORITY < SANDBOX_VM_EGRESS_DENY_PRIORITY
    # Lower number wins in GCP, so the ALLOW must be numerically BELOW the DENY.
    assert ot._PURDUE_INGRESS_ALLOW_PRIORITY < ot._PURDUE_INGRESS_DENY_PRIORITY, (
        "the Gateway ingress ALLOW must outrank the catch-all ingress DENY, or the "
        "cell refuses the Gateway too and nothing can reach it")


def test_the_ingress_deny_is_never_created_before_its_allow():
    src = _fn_src(_OT, "_wire_purdue_firewall")
    allow_at = src.index('names["ingress_allow"]')
    deny_at = src.index('names["ingress_deny"]')
    assert allow_at < deny_at, (
        "the catch-all ingress DENY must be created AFTER the Gateway ALLOW")
    # And the allow's failure path must not fall through to the deny.
    between = src[allow_at:deny_at]
    assert "return" in between, (
        "a failed Gateway ALLOW must return before the catch-all DENY is created — "
        "otherwise the cell is fenced away from the Gateway that would fix it")


def test_the_gateway_is_matched_by_tag_not_by_address():
    src = _fn_src(_OT, "_wire_purdue_firewall")
    assert "source_tags=[GATEWAY_NETWORK_TAG]" in src, (
        "the Gateway must be matched by network tag: the shared Gateway is "
        "ref-counted and recreated on demand, so a pinned address stops matching the "
        "day it comes back — and the symptom is a timing-out Web Jump, which the "
        "troubleshooting table teaches operators to read as an undersized gateway")
    for pinned in ("private_ip", "egress_ip", "source_ranges=[gateway"):
        assert pinned not in src, f"the ingress allow pins {pinned!r} instead of a tag"


def test_the_gateway_tag_matches_what_the_gateway_vm_actually_carries():
    ot = _load()
    gcp = open(_GCP, encoding="utf-8").read()
    assert f'_JUMPOINT_LABEL = "{ot.GATEWAY_NETWORK_TAG}"' in gcp, (
        "ot_service.GATEWAY_NETWORK_TAG and gcp_service._JUMPOINT_LABEL have drifted — "
        "the ingress allow would match no source and the cell would be unreachable")


def test_every_rule_that_exists_is_recorded_on_the_child():
    src = _fn_src(_OT, "_wire_purdue_firewall")
    # One _record per ensure call, so destroy removes exactly what is there and a
    # rewire creates only what is missing — the Web Jump / tunnel contract.
    assert src.count("ensure_segmentation_rule") == 3
    assert src.count("_record(names[") == 3, (
        "each rule must be recorded onto the child the moment it exists")
    assert "update_metadata" in _fn_src(_OT, "_wire_purdue_firewall")


def test_the_destroy_path_removes_the_recorded_rules():
    src = _fn_src(_GCP_VM, "_run_destroy")
    assert "ot_firewall_rules" in src, (
        "the GCP destroy path no longer removes the cell's firewall rules — a leftover "
        "rule targets the network tag, so it would fence the next cell that reuses the "
        "VM name, and the egress deny outranks the sandbox's own rules")
    assert "delete_firewall_rule" in src
    fw_at = src.index("ot_firewall_rules")
    term_at = src.index("terminate_instance")
    assert fw_at < term_at, "the rules must be removed before the instance is deleted"


def test_the_cell_ports_cover_every_protocol_the_image_answers():
    ot = _load()
    ports = ot.purdue_cell_ports({"ot_params": {"plc_port": 502}})
    assert 22 in ports, "Shell Jump would stop working"
    assert 1881 in ports, "the Web Jump renders the HMI on 1881"
    for preset in ot.OT_PORT_PRESETS.values():
        assert preset["port"] in ports, (
            f"port {preset['port']} is a tunnel preset the cell may be asked to serve; "
            "an allow-list that only knew about the cell's own tunnel would make a "
            "standalone tunnel to it fail for no visible reason")


def test_a_custom_hmi_port_is_admitted():
    ot = _load()
    assert 9000 in ot.purdue_cell_ports({"ot_hmi_port": 9000, "ot_params": {}})


def test_rule_names_are_per_cell():
    ot = _load()
    a = set(ot._purdue_rule_names("ot-cell-01").values())
    b = set(ot._purdue_rule_names("ot-cell-02").values())
    assert not (a & b), "two cells would fight over one set of firewall rules"


def test_the_feature_is_off_until_asked_for():
    src = _fn_src(_OT, "purdue_firewall_enabled")
    assert 'get_bool("ot_purdue_firewall_enabled", False)' in src, (
        "these rules change the network posture of a running demo — they must be a "
        "deliberate choice, not something a dashboard upgrade switches on")


def test_only_gcp_wires_them():
    src = _fn_src(_OT, "_wire_cell")
    assert 'cloud == "gcp" and purdue_firewall_enabled()' in src, (
        "AWS security groups and Azure NSGs need their own shape of this; the tag "
        "hook and these rules are GCP's")


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
