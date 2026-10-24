"""The POV router's source as one text: api/pov.py plus the route modules it registers.

api/pov.py declares the router and the routes on the POV itself; the routes under
/managed/{env_id}/ for wiring, VMs, handoff, scheduling and close-out live in sibling
modules that register on that same router. Tests that sweep the router's source (every
``@router.`` line, a route's body by its def line, a word the routes must not say) read
it through ``pov_api_source()`` so a route is checked wherever it now lives.

Not a test file (no ``test_`` prefix): imported by tests, run by none.
"""
import os

_API = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "web_dashboard", "api")
#: The router's own module first, so ``router = APIRouter(`` is found before any route.
POV_ROUTE_MODULES = ("pov.py", "pov_wiring.py", "pov_vms.py", "pov_handoff.py",
                     "pov_schedule.py", "pov_closeout.py")


def pov_api_paths():
    return [os.path.join(_API, name) for name in POV_ROUTE_MODULES]


def pov_api_source():
    """Every module on the POV router, in that order, joined by blank lines."""
    parts = []
    for path in pov_api_paths():
        with open(path, encoding="utf-8") as fh:
            parts.append(fh.read())
    return "\n\n".join(parts)
