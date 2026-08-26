"""Behaviour of a multi-protocol OT cell's tunnel bookkeeping (services/ot_service).

The cell serves Modbus, S7comm, EtherNet/IP and OPC UA at once, and gets ONE PRA
protocol tunnel per selected protocol. That turned five singular ``ot_tunnel_*``
metadata scalars into an ``ot_tunnels`` list, which is the part with teeth:

* a cell deployed BEFORE multi-protocol has only the singular keys, and its tunnel
  must still be found (for teardown) and adopted (rather than provisioned twice);
* the list must win over the singular keys when both are present, or the primary
  tunnel would be destroyed twice;
* ``Re-wire`` must provision exactly the missing protocols — and must not crash on
  a cell that is already complete. It did: the summary dict read the provisioning
  loop's variables, which are unbound when the loop body never runs, so Re-wire on
  a fully-wired cell raised UnboundLocalError instead of reporting the cell;
* "wired" must mean EVERY selected protocol has a tunnel. While that test lived
  (twice, copied) in the cells endpoint and the home-page tile, it went green on
  the first tunnel of several.

These are runtime checks rather than a source scan, because the failure modes are
in the bookkeeping, not in which strings appear where.

Run: python tests/test_ot_multi_tunnel.py   (or under pytest)
"""
import asyncio
import builtins
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")


def _load():
    """ot_service standalone — it imports its collaborators lazily inside
    functions, so the module itself needs no app dependencies."""
    spec = importlib.util.spec_from_file_location("ot_service_multi_tunnel", _SVC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakePra:
    def __init__(self, calls):
        self.calls = calls

    async def provision_web_jump(self, **kw):
        return {"web_jump_id": "WJ", "tf_state_json": "WJSTATE"}

    async def provision_api_tunnel(self, **kw):
        self.calls.append(kw["name"])
        n = len(self.calls)
        return {"tunnel_jump_id": f"J{n}", "tf_state_json": f"S{n}"}


class _FakeJobs:
    def update_progress(self, *a, **k):
        pass

    def update_metadata(self, db, cid, data):
        pass


def _wire(ot, cmeta, calls):
    """Run _wire_cell with its lazy `from . import ...` satisfied by fakes."""
    stub = types.ModuleType("stub")
    stub.config_service = types.SimpleNamespace(get=lambda k: "secret",
                                                get_bool=lambda k, d=None: False)
    stub.job_service = _FakeJobs()
    stub.terraform_pra_service = _FakePra(calls)

    ot.ps_checkout_skip_reason = lambda m: "skipped — disabled"
    ot._cell_has_gateway_ref = lambda meta, cloud: True
    ot.resolve_jump_targets = lambda jg, jp, cloud="gcp": ("JG", "JP")

    real_import = builtins.__import__

    def _stub_import(name, glob=None, loc=None, fromlist=(), level=0):
        # Only ot_service's own relative imports; anything else (asyncio's
        # internals, for instance) must still resolve normally.
        if level == 1 and glob is not None and glob.get("__name__") == ot.__name__:
            return stub
        return real_import(name, glob, loc, fromlist, level)

    loop = asyncio.new_event_loop()
    builtins.__import__ = _stub_import
    try:
        return loop.run_until_complete(ot._wire_cell(None, "P", "C", cmeta, "gcp"))
    finally:
        builtins.__import__ = real_import
        loop.close()


# ── the protocol list ─────────────────────────────────────────────────────────

def test_protocols_resolve_with_the_singular_key_as_a_fallback():
    ot = _load()
    assert ot.resolve_cell_protocols({"protocols": ["s7", "modbus"]}) == ["s7", "modbus"]
    # A cell deployed before multi-protocol carries only `protocol`.
    assert ot.resolve_cell_protocols({"protocol": "ethernet-ip"}) == ["ethernet-ip"]
    assert ot.resolve_cell_protocols({}) == ["modbus"]
    # De-duplicated and normalised, so a repeated tick cannot double-provision.
    assert ot.resolve_cell_protocols({"protocols": ["S7 ", " s7", "MODBUS"]}) == ["s7", "modbus"]


# ── the legacy projection ─────────────────────────────────────────────────────

def test_a_pre_multi_protocol_cell_still_has_a_findable_tunnel():
    """Its tunnel lives in the singular keys. If this projection breaks, every
    destroy path silently stops tearing those cells' jump items down."""
    ot = _load()
    legacy = {"ot_tunnel_tf_state": "STATE", "ot_tunnel_protocol": "modbus",
              "ot_tunnel_jump_id": "77", "ot_tunnel_local_port": 502,
              "ot_tunnel_remote_port": 502}
    assert ot.cell_tunnels(legacy) == [
        {"protocol": "modbus", "jump_id": "77", "tf_state": "STATE",
         "local_port": 502, "remote_port": 502}]
    assert ot.cell_tunnels({}) == []


def test_the_list_wins_over_the_singular_keys():
    """The singular keys mirror the FIRST list entry, so counting both would
    destroy the primary tunnel twice."""
    ot = _load()
    meta = {"ot_tunnels": [{"protocol": "s7", "tf_state": "A", "jump_id": "1",
                            "local_port": 102, "remote_port": 102}],
            "ot_tunnel_tf_state": "A", "ot_tunnel_protocol": "s7"}
    assert len(ot.cell_tunnels(meta)) == 1


# ── "wired" means every selected protocol ─────────────────────────────────────

def test_a_cell_is_wired_only_when_every_protocol_has_a_tunnel():
    ot = _load()
    ot.ps_checkout_skip_reason = lambda m: "skipped — disabled"
    base = {"ot_web_jump_tf_state": "W",
            "ot_params": {"protocols": ["modbus", "s7"]}}
    assert not ot.cell_wiring_complete(base), "no tunnels yet"
    one = dict(base, ot_tunnels=[{"protocol": "modbus", "tf_state": "A"}])
    assert not ot.cell_wiring_complete(one), (
        "one of two tunnels reported the cell as fully wired — the bug the shared "
        "predicate exists to prevent")
    both = dict(base, ot_tunnels=[{"protocol": "modbus", "tf_state": "A"},
                                  {"protocol": "s7", "tf_state": "B"}])
    assert ot.cell_wiring_complete(both)
    no_web_jump = {k: v for k, v in both.items() if k != "ot_web_jump_tf_state"}
    assert not ot.cell_wiring_complete(no_web_jump)


# ── the provisioning loop ─────────────────────────────────────────────────────

def test_one_tunnel_per_protocol_on_its_canonical_port():
    ot = _load()
    calls = []
    cmeta = {"instance_name": "cell1", "private_ip": "10.0.0.5",
             "ot_params": {"protocols": ["modbus", "s7", "ethernet-ip"]}}
    summary = _wire(ot, cmeta, calls)
    assert calls == ["ot-cell1-modbus", "ot-cell1-s7", "ot-cell1-ethernet-ip"], calls
    assert [t["remote_port"] for t in cmeta["ot_tunnels"]] == [502, 102, 44818]
    # The singular keys mirror the first tunnel, for older readers.
    assert cmeta["ot_tunnel_protocol"] == "modbus"
    assert [t["protocol"] for t in summary["tunnels"]] == ["modbus", "s7", "ethernet-ip"]


def test_rewiring_a_complete_cell_provisions_nothing_and_does_not_raise():
    """Re-wire is a button on every cell, including healthy ones. Reporting from
    the provisioning loop's variables raised UnboundLocalError here, because a
    complete cell never enters that loop."""
    ot = _load()
    calls = []
    cmeta = {"instance_name": "cell1", "private_ip": "10.0.0.5",
             "ot_params": {"protocols": ["modbus", "s7"]}}
    _wire(ot, cmeta, calls)
    calls.clear()
    summary = _wire(ot, cmeta, calls)          # must not raise
    assert calls == [], f"re-wire re-provisioned tunnels: {calls}"
    assert len(summary["tunnels"]) == 2


def test_rewire_adopts_a_legacy_tunnel_and_adds_only_the_missing_one():
    ot = _load()
    calls = []
    legacy = {"instance_name": "old", "private_ip": "10.0.0.6",
              "ot_web_jump_tf_state": "W",
              "ot_tunnel_tf_state": "OLDSTATE", "ot_tunnel_protocol": "modbus",
              "ot_tunnel_jump_id": "9", "ot_tunnel_local_port": 502,
              "ot_tunnel_remote_port": 502,
              "ot_params": {"protocols": ["modbus", "s7"]}}
    _wire(ot, legacy, calls)
    assert calls == ["ot-old-s7"], f"the existing modbus tunnel was re-created: {calls}"
    assert legacy["ot_tunnels"][0]["tf_state"] == "OLDSTATE", "legacy tunnel not adopted"
    assert len(legacy["ot_tunnels"]) == 2
    # The singular keys must keep describing the original tunnel, not the new one.
    assert legacy["ot_tunnel_tf_state"] == "OLDSTATE"


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
