"""OT protocol preset invariants (services/ot_service).

The preset table is the contract between the tunnel form, the cell orchestrator
and the docs: a silently-changed port would produce a tunnel that dials the wrong
thing while everything reports green. Pure-function tests — the module is loaded
by file path so no app dependency (fastapi/sqlalchemy/pydantic) is needed.

Run: python tests/test_ot_ports.py   (or under pytest)
"""
import importlib.util
import os
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
