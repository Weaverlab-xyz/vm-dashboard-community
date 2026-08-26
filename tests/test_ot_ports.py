"""OT protocol preset invariants (services/ot_service).

The preset table is the contract between the tunnel form, the cell orchestrator
and the docs: a silently-changed port would produce a tunnel that dials the wrong
thing while everything reports green. Pure-function tests — the module is loaded
by file path so no app dependency (fastapi/sqlalchemy/pydantic) is needed.

Run: python tests/test_ot_ports.py   (or under pytest)
"""
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")


def _load():
    spec = importlib.util.spec_from_file_location("ot_service_under_test", _SVC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_preset_table_carries_the_canonical_ot_ports():
    ot = _load()
    expected = {"modbus": 502, "opcua": 4840, "dnp3": 20000,
                "s7": 102, "ethernet-ip": 44818}
    actual = {k: v["port"] for k, v in ot.OT_PORT_PRESETS.items()}
    assert actual == expected, f"preset ports drifted: {actual}"
    for key, info in ot.OT_PORT_PRESETS.items():
        assert info.get("label"), f"preset {key} has no label"


def test_local_port_defaults_to_the_remote_port():
    """The operator's client config should read naturally (127.0.0.1:502 → :502),
    so the rep-side listen port follows the remote port unless overridden."""
    ot = _load()
    assert ot.resolve_ports("modbus") == (502, 502)
    assert ot.resolve_ports("opcua", local_port=14840) == (14840, 4840)
    assert ot.resolve_ports("modbus", remote_port=1502) == (1502, 1502)


def test_custom_requires_an_explicit_port_and_unknown_protocols_are_rejected():
    ot = _load()
    assert ot.resolve_ports("custom", remote_port=9600) == (9600, 9600)
    for bad_call in (lambda: ot.resolve_ports("custom"),
                     lambda: ot.resolve_ports("bacnet")):
        try:
            bad_call()
        except ot.OTError:
            continue
        raise AssertionError("expected OTError")


def test_the_slug_matches_the_pra_hcl_normalisation():
    """The config-key slug must use the same character class the terraform HCL
    resource name uses, so a tunnel's key and its HCL name can never disagree."""
    ot = _load()
    assert ot.tunnel_slug("Lab PLC #1") == "lab_plc__1"
    assert ot.tunnel_slug("ot-cell-01-modbus") == "ot_cell_01_modbus"
    assert ot.tunnel_slug("  ") == ""


# ── the presets the CELL offers must be the ones the image serves ─────────────

def test_every_cell_protocol_has_a_simulator_in_the_baked_image():
    """A cell form offering a protocol the image doesn't serve provisions a tunnel
    to a port with no listener — and that session failure is indistinguishable
    from a blocked firewall, which is the most expensive kind of demo failure.

    So the `cell` flag on each preset is held against the compose stack the
    provisioner actually writes: every cell protocol needs a service publishing
    its canonical port, and every published port (bar the HMI) needs a preset."""
    ot = _load()
    script = open(os.path.join(_ROOT, "provisioners", "ot", "ot-sim-debian.sh"),
                  encoding="utf-8").read()
    # The whole script, not one heredoc: the base compose carries plc + hmi and each
    # `sim_enabled` block APPENDS its service, so a per-heredoc scan would see only
    # Modbus and call every other simulator missing. Port mappings appear nowhere
    # else in the script, so scanning it whole is both correct and structure-proof.
    #
    # 1:1 mappings only ("502:502"): the baked stack deliberately never translates a
    # port, so a published host port IS the protocol's canonical port.
    published = {int(m.group(1)) for m in re.finditer(r'"(\d+):(\d+)"', script)
                 if m.group(1) == m.group(2)}

    for key in ot.cell_protocols():
        port = ot.OT_PORT_PRESETS[key]["port"]
        assert port in published, (
            f"preset {key!r} is marked cell-served but nothing in the baked compose "
            f"publishes :{port} — the cell would offer a tunnel to a dead port")

    hmi_port = 1881
    for port in published - {hmi_port}:
        assert any(p["port"] == port and p.get("cell")
                   for p in ot.OT_PORT_PRESETS.values()), (
            f"the image publishes :{port} but no cell-served preset names it — the "
            "simulator is unreachable from the cell form")


def test_dnp3_is_not_offered_on_the_cell():
    """DNP3 stays a standalone-tunnel-to-real-gear preset: dnp3-python's wheels are
    the least reliable of the OT stacks and a bad pin fails the whole bake. If a
    simulator is ever added, flip `cell` and the test above starts enforcing it."""
    ot = _load()
    assert ot.OT_PORT_PRESETS["dnp3"]["port"] == 20000
    assert not ot.OT_PORT_PRESETS["dnp3"].get("cell")
    assert "dnp3" not in ot.cell_protocols()


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
