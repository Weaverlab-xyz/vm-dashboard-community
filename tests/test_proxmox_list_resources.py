"""Proxmox resource listing must actually list.

``_list_all_resources_sync`` loops over the nodes and calls ``_list_node_resources_sync``
for each, inside ``except Exception: logger.warning(...)``. The call passed only the node,
so ``conn`` was missing: every node raised ``TypeError``, the handler logged a warning and
moved on, and the function returned ``[]`` for every cluster. Nothing failed, and the page
showed an empty Proxmox. Found by the pyright ratchet (``Argument missing for parameter
"node"``); no test exercised the loop.

Runs under pytest, or standalone:
    python tests/test_proxmox_list_resources.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-proxmox-list")

try:
    from web_dashboard.services import proxmox_service as px
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001 -- reported by __main__
    px = None
    _IMPORT_ERR = exc


class _Leaf:
    def __init__(self, rows):
        self._rows = rows

    def get(self):
        return self._rows


class _Node:
    def __init__(self, name):
        self.qemu = _Leaf([{"vmid": 101, "name": f"vm-on-{name}", "status": "running"}])
        self.lxc = _Leaf([{"vmid": 201, "name": f"ct-on-{name}", "status": "stopped"}])


class _FakePve:
    def __init__(self, names):
        self._names = names
        self.nodes = self._Nodes(self)

    class _Nodes:
        def __init__(self, outer):
            self._outer = outer

        def __call__(self, name):
            return _Node(name)

        def get(self):
            return [{"node": n} for n in self._outer._names]


def _with_fake(names, fn):
    real = px._client
    px._client = lambda conn: _FakePve(names)
    try:
        return fn()
    finally:
        px._client = real


def test_listing_every_node_returns_its_resources():
    out = _with_fake(["pve1", "pve2"], lambda: px._list_all_resources_sync(object()))
    got = sorted((r["node"], r["type"], r["vmid"]) for r in out)
    assert got == [("pve1", "lxc", 201), ("pve1", "qemu", 101),
                   ("pve2", "lxc", 201), ("pve2", "qemu", 101)], got


def test_an_explicit_node_list_is_honoured():
    out = _with_fake(["pve1", "pve2"], lambda: px._list_all_resources_sync(object(), ["pve2"]))
    assert {r["node"] for r in out} == {"pve2"}, out


if __name__ == "__main__":
    if px is None:
        print(f"SKIP all: proxmox_service unavailable ({_IMPORT_ERR})")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    sys.exit(1 if failures else 0)
