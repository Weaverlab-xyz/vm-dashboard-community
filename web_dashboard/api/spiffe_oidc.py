"""OIDC discovery for the dashboard's own SPIFFE issuer, and its status for Settings.

docs/design/dashboard-workload-identity.md, Slice 1.

    GET /spiffe/.well-known/openid-configuration   public
    GET /spiffe/keys                               public — JWT signing keys, public members only
    GET /api/spiffe-identity                       agents:read — what Settings shows

**Public by construction, and deliberately so.** AWS, Azure and GCP fetch these two
documents anonymously from the internet when they validate a token this dashboard
minted; they carry public keys and nothing else. Putting them behind SSO would break every
federation at once. Both answer 404 unless ``dashboard_spiffe_identity_enabled`` is on, so
an install that never turned the feature on publishes nothing new.

They are also on the agent gateway's vhost (``examples/remote-agent/Caddyfile``), because
that is the part of an install built to be reachable from outside, and the default issuer
is derived from it.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from ..database import User
from ..services import dashboard_identity
from .auth import require_explicit_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix=dashboard_identity.ISSUER_PATH, tags=["spiffe"])
admin_router = APIRouter(prefix="/api/spiffe-identity", tags=["spiffe"])

# Short enough that a key rotation reaches a verifier well inside the window SPIRE
# publishes the next key ahead of, long enough that a cloud's validation burst is cheap.
_CACHE = {"Cache-Control": "public, max-age=300"}


def _require_enabled() -> None:
    if not dashboard_identity.enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/.well-known/openid-configuration")
def discovery():
    _require_enabled()
    doc = dashboard_identity.discovery_document()
    if not doc["issuer"]:
        raise HTTPException(status_code=503, detail="No issuer is configured yet.")
    return JSONResponse(content=doc, headers=_CACHE)


@router.get("/keys")
def keys():
    _require_enabled()
    found = dashboard_identity.jwks()
    if not found:
        # 503, not an empty set: a verifier that caches "no keys" refuses every token
        # until its cache expires, which is worse than retrying a server error.
        raise HTTPException(status_code=503, detail="No signing keys are available yet.")
    return JSONResponse(content={"keys": found}, headers=_CACHE)


@admin_router.get("")
def identity_status(current_user: User = Depends(require_explicit_permission("agents", "read"))):
    return dashboard_identity.status()
