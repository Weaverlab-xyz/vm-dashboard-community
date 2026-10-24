"""The POV router spans api/pov.py and the route modules that register on it.

The routes under /managed/{env_id}/ for wiring, VMs, handoff, scheduling and close-out live
in api/pov_wiring.py, pov_vms.py, pov_handoff.py, pov_schedule.py and pov_closeout.py. Three
ways that comes apart without any other test noticing:

  * a route module that builds its own APIRouter() escapes the router-level gates (the
    pov:read floor and the per-POV instance gate) that test_pov_instance_grants pins on
    api/pov.py's declaration,
  * a module pov.py stops importing is never registered, and its routes 404,
  * a new route module missing from tests/_pov_api_src.py is invisible to every test that
    sweeps the router's source, so their "every route ..." checks pass without reading it.

Run: python tests/test_pov_route_modules.py   (or under pytest)
"""
import glob
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pov_api_src import POV_ROUTE_MODULES  # noqa: E402

_API = os.path.join(_ROOT, "web_dashboard", "api")


def _read(name):
    with open(os.path.join(_API, name), encoding="utf-8") as fh:
        return fh.read()


def _route_modules():
    return POV_ROUTE_MODULES[1:]


def test_each_route_module_registers_on_the_pov_router():
    for name in _route_modules():
        src = _read(name)
        assert re.search(r"^from \.pov import [^\n]*\brouter\b", src, re.M), (
            f"{name} does not take its router from api/pov.py")
        assert "APIRouter(" not in src, (
            f"{name} builds its own router, so the POV router's gates do not cover it")


def test_pov_imports_every_route_module():
    src = _read("pov.py")
    m = re.search(r"^from \. import ([^\n#]+)", src.split("\n# The routes under", 1)[1], re.M)
    assert m, "api/pov.py no longer imports its route modules"
    imported = {f"{n.strip()}.py" for n in m.group(1).split(",")}
    assert imported == set(_route_modules()), (
        f"api/pov.py imports {sorted(imported)}, the helper lists {sorted(_route_modules())}")


def test_the_helper_lists_every_module_on_the_router():
    on_router = []
    for path in sorted(glob.glob(os.path.join(_API, "pov_*.py"))):
        if re.search(r"^from \.pov import [^\n]*\brouter\b", _read(os.path.basename(path)), re.M):
            on_router.append(os.path.basename(path))
    missing = sorted(set(on_router) - set(_route_modules()))
    assert not missing, (
        f"route modules on the POV router missing from tests/_pov_api_src.py: {missing}")


def test_every_route_module_has_routes_on_the_live_router():
    try:
        from web_dashboard.api import pov
    except ModuleNotFoundError as exc:
        print(f"SKIP: {exc}")
        return
    modules = {getattr(r.endpoint, "__module__", "") for r in pov.router.routes}
    for name in _route_modules():
        assert f"web_dashboard.api.{name[:-3]}" in modules, f"no route from {name} is registered"


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
