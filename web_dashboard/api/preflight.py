"""One endpoint that answers "what is actually reachable right now?".

The eight per-panel test buttons stay exactly where they are — see
``services/preflight`` for what this covers, what it deliberately does not, and why
sending a real notification and stamping a hypervisor row are both disqualifying for a
sweep somebody might trigger by reloading a page.

**Admin, via the dependency rather than a hand-rolled check.** The eight existing probes
use three different admin styles between them, one of which decodes the JWT inline. This
uses ``require_admin``, which consults ``is_effective_admin`` so an Entitle JIT grant
counts, and which refuses an accessor outright. Every one of these results names an
integration and carries an upstream error message, so it is admin-only for the same reason
each individual probe is.

**A read that changes nothing.** GET, no body, no writes. That is what makes it safe to
put behind a refresh button, and it is why the two side-effecting probes are excluded
rather than made optional — an endpoint whose safety depends on a query parameter is one
copied URL away from not being safe.
"""
import logging

from fastapi import APIRouter, Depends

from ..database import User
from ..services import preflight
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/preflight", tags=["preflight"])


@router.get("")
async def run_preflight(current_user: User = Depends(require_admin)) -> dict:
    """Probe every registered integration and report each one's answer.

    Always 200. A failing integration is the ANSWER to the question this endpoint asks,
    not a fault in the endpoint — the same convention the Skytap and storage probes chose,
    and the opposite of the OIDC one, which 400s. Picking the 200 side here is deliberate:
    a sweep that raised on the first unreachable integration could never report the other
    five, which is the entire reason this exists.
    """
    results = await preflight.run_all()
    return {"checks": [r._asdict() for r in results],
            "summary": preflight.summarize(results)}
