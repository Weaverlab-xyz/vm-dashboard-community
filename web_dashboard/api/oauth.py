"""OAuth 2.0 token endpoint for workload identities (client_credentials grant only).

The dashboard as a MINIMAL authorization server for its own API: one grant, one kind of
client, no user consent, no refresh tokens. That is the whole of what a workload needs
and nothing an attacker could use to reach a person's session.

    POST /api/oauth/token
      grant_type=client_credentials
      [scope=vms:read jobs:read]
      client authentication: HTTP Basic (client_id:client_secret), or the same two as
      form fields -- RFC 6749 section 2.3.1 allows both, and SDKs disagree on which
      they send.

    GET /.well-known/oauth-authorization-server   (RFC 8414 metadata)

Errors are the RFC 6749 section 5.2 JSON shape (``error``, ``error_description``), not
FastAPI's ``detail``, because OAuth client libraries parse exactly that.

Why no ``authorization_code`` or ``refresh_token`` grants: people already sign in through
the login page or an OIDC provider (``api/auth``), and a workload re-runs this grant
instead of refreshing. Every grant not implemented is a flow nobody has to review.
"""
import base64
import logging
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from jose import jwt as jose_jwt
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import login_guard, public_url, service_accounts, spiffe_assertion
from ..services.service_accounts import ServiceAccountError
from .auth import PERMISSION_SCOPE_LEVELS, _client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oauth", tags=["oauth"])
wellknown_router = APIRouter(tags=["oauth"])

TOKEN_PATH = "/api/oauth/token"
GRANT_TYPES = ["client_credentials"]

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(code: str, description: str, status: int = 400,
           basic: bool = False) -> JSONResponse:
    headers = dict(_NO_STORE)
    if basic and status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="vm-dashboard"'
    return JSONResponse({"error": code, "error_description": description},
                        status_code=status, headers=headers)


def _basic_credentials(request: Request) -> Optional[tuple]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except Exception:  # noqa: BLE001 -- a malformed header is a failed client auth
        return ("", "")
    cid, _, secret = decoded.partition(":")
    # RFC 6749 2.3.1: both halves are form-urlencoded before being Basic-encoded.
    return unquote(cid), unquote(secret)


def _check_scope_catalogue(scope: dict) -> Optional[str]:
    for name, levels in scope.items():
        allowed = PERMISSION_SCOPE_LEVELS.get(name)
        if allowed is None:
            return f"Unknown scope {name!r}."
        bad = [lvl for lvl in levels if lvl not in allowed]
        if bad:
            return f"Scope {name!r} has no level {bad[0]!r} (it offers {', '.join(allowed)})."
    return None


@router.post("/token")
async def token(request: Request, db: Session = Depends(get_db)):
    """Exchange client credentials -- a secret, or a SPIFFE JWT-SVID assertion -- for a
    short-lived workload access token."""
    form = await request.form()
    grant_type = (form.get("grant_type") or "").strip()
    assertion_type = (form.get("client_assertion_type") or "").strip()
    assertion = (form.get("client_assertion") or "").strip()

    basic = _basic_credentials(request)
    if basic is not None:
        client_id, secret = basic
    else:
        client_id = (form.get("client_id") or "").strip()
        secret = form.get("client_secret") or ""

    if grant_type not in GRANT_TYPES:
        return _error("unsupported_grant_type",
                      "Only grant_type=client_credentials is supported.")

    using_assertion = bool(assertion_type or assertion)
    if using_assertion:
        # RFC 6749 2.3: one authentication method per request. A secret beside an
        # assertion would leave which one was checked up to the reader.
        if secret:
            return _error("invalid_request",
                          "Use either a client secret or a client assertion, not both.")
        if assertion_type not in spiffe_assertion.ASSERTION_TYPES or not assertion:
            return _error("invalid_client", "Unsupported client assertion.", 401)
    elif not client_id or not secret:
        return _error("invalid_client", "Client authentication is required.", 401,
                      basic=basic is not None)

    # The same failure budget the sign-in page uses, keyed so a client id and a username
    # can never share one. Checked before any verification, so a throttled caller costs
    # nothing. An assertion is keyed on its (unverified) subject, so garbage presented
    # for one SPIFFE ID does not spend another's budget.
    ip = _client_ip(request)
    if using_assertion:
        try:
            _sub = str(jose_jwt.get_unverified_claims(assertion).get("sub", ""))[:100]
        except Exception:  # noqa: BLE001 -- malformed; still throttled, by address
            _sub = "?"
        throttle_key = f"oauth-svid:{_sub}"
    else:
        throttle_key = f"oauth-client:{client_id[:100]}"
    try:
        login_guard.check(db, username=throttle_key, ip=ip)
    except login_guard.LoginThrottled as exc:
        resp = _error("invalid_client", "Too many failed attempts. Try again shortly.", 429)
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp

    if using_assertion:
        issuer = public_url.resolve(request).rstrip("/")
        try:
            client = spiffe_assertion.authenticate(
                db, assertion, (issuer, issuer + TOKEN_PATH), client_id=client_id)
        except spiffe_assertion.AssertionError_ as exc:
            login_guard.record_failure(db, username=throttle_key, ip=ip)
            logger.warning("oauth token: SPIFFE assertion refused from %s: %s", ip or "?", exc)
            return _error("invalid_client", "Client assertion was not accepted.", 401)
    else:
        client = service_accounts.authenticate_client(db, client_id, secret)
        if not client:
            login_guard.record_failure(db, username=throttle_key, ip=ip)
            logger.warning("oauth token: client authentication failed for %r from %s",
                           client_id[:40], ip or "?")
            return _error("invalid_client", "Client authentication failed.", 401,
                          basic=basic is not None)

    user = db.query(User).filter(User.id == client.user_id).first()

    try:
        requested = service_accounts.parse_scope(form.get("scope"))
    except ServiceAccountError as exc:
        return _error("invalid_scope", str(exc))
    if requested is not None:
        problem = _check_scope_catalogue(requested)
        if problem:
            return _error("invalid_scope", problem)
    granted = service_accounts.granted_scope(user, requested)
    if requested is not None and not granted:
        return _error("invalid_scope",
                      "None of the requested scope is held by this service account.")

    access_token, expires_in = service_accounts.issue_access_token(user, client, granted)
    service_accounts.touch_client(db, client)
    login_guard.clear(db, username=throttle_key)
    db.commit()
    logger.info("oauth token issued to %s via client %s (scope=%s, ttl=%ss)",
                user.username, client.client_id,
                service_accounts.format_scope(granted) or "<account>", expires_in)

    body = {"access_token": access_token, "token_type": "Bearer", "expires_in": expires_in}
    if granted is not None:
        body["scope"] = service_accounts.format_scope(granted)
    return JSONResponse(body, headers=_NO_STORE)


@wellknown_router.get("/.well-known/oauth-authorization-server")
def metadata(request: Request):
    """RFC 8414 authorization-server metadata, so SDKs and MCP clients can discover the
    token endpoint instead of having it hard-coded."""
    issuer = public_url.resolve(request).rstrip("/")
    return {
        "issuer": issuer,
        "token_endpoint": issuer + TOKEN_PATH,
        "grant_types_supported": GRANT_TYPES,
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post",
                                                  "private_key_jwt"],
        "token_endpoint_auth_signing_alg_values_supported": list(spiffe_assertion.ASYMMETRIC_ALGS),
        "response_types_supported": [],
        "scopes_supported": sorted(f"{s}:{lvl}" for s, levels in PERMISSION_SCOPE_LEVELS.items()
                                   for lvl in levels),
    }


# ── SPIFFE trust domains (admin) ──────────────────────────────────────────────
# Where a JWT-SVID client assertion's signing keys come from. See
# services/spiffe_assertion and the SpiffeTrustDomain model for the two sources.

from datetime import datetime  # noqa: E402
from typing import Optional as _Opt  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .auth import require_admin  # noqa: E402


class TrustDomainRequest(BaseModel):
    trust_domain: str
    jwks_url: _Opt[str] = None
    ca_pem: _Opt[str] = None
    bundle_json: _Opt[str] = None


def _td_item(row) -> dict:
    keys = None
    try:
        keys = len(spiffe_assertion.bundle_keys(row.bundle_json)) if row.bundle_json else None
    except spiffe_assertion.AssertionError_:
        keys = 0
    return {
        "id": row.id, "trust_domain": row.trust_domain,
        "source": "url" if row.jwks_url else ("bundle" if row.bundle_json else "none"),
        "jwks_url": row.jwks_url or "", "ca_pinned": bool((row.ca_pem or "").strip()),
        "bundle_jwt_keys": keys,
        "bundle_captured_at": row.bundle_captured_at.isoformat() if row.bundle_captured_at else None,
        "stale": spiffe_assertion.is_stale(row),
        "spire_lab_id": row.spire_lab_id or "",
    }


def _validate_td(body: TrustDomainRequest) -> tuple:
    td = (body.trust_domain or "").strip().lower()
    if not spiffe_assertion.trust_domain_of(f"spiffe://{td}/x") == td or "/" in td:
        raise HTTPException(status_code=400, detail=f"{body.trust_domain!r} is not a trust domain name.")
    url = (body.jwks_url or "").strip()
    bundle = (body.bundle_json or "").strip()
    if url and not url.startswith("https://"):
        raise HTTPException(status_code=400,
                            detail="The JWKS URL must be https — keys fetched in the clear are not keys.")
    if bundle:
        try:
            if not spiffe_assertion.bundle_keys(bundle):
                raise HTTPException(
                    status_code=400,
                    detail="That bundle has no JWT-SVID keys (use: jwt-svid). Export it with "
                           "`spire-server bundle show -format spiffe`.")
        except spiffe_assertion.AssertionError_ as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if not url and not bundle:
        raise HTTPException(status_code=400, detail="Give a JWKS URL, a SPIFFE bundle, or both.")
    return td, url, bundle


@router.get("/spiffe-trust-domains")
def list_trust_domains(_admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from ..database import SpiffeTrustDomain
    rows = db.query(SpiffeTrustDomain).order_by(SpiffeTrustDomain.trust_domain).all()
    return [_td_item(r) for r in rows]


@router.put("/spiffe-trust-domains")
def upsert_trust_domain(body: TrustDomainRequest, admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Create or replace a trust domain's key source. A bundle given here is stamped as
    captured now."""
    from ..database import SpiffeTrustDomain
    from ..services import job_service
    td, url, bundle = _validate_td(body)
    row = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == td).first()
    if not row:
        row = SpiffeTrustDomain(trust_domain=td, created_by=admin.username)
        db.add(row)
    row.jwks_url = url or None
    row.ca_pem = (body.ca_pem or "").strip() or None
    if bundle:
        row.bundle_json = bundle
        row.bundle_captured_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    spiffe_assertion.clear_state()
    job_service.log_audit(db, admin.username, "spiffe_trust_domain.upsert",
                          details={"trust_domain": td, "source": _td_item(row)["source"]})
    return _td_item(row)


@router.delete("/spiffe-trust-domains/{trust_domain}")
def delete_trust_domain(trust_domain: str, admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Stop trusting a trust domain. Every SVID client in it is refused from the next
    exchange; tokens already issued live out their (minutes-long) TTL."""
    from ..database import SpiffeTrustDomain
    from ..services import job_service
    row = (db.query(SpiffeTrustDomain)
           .filter(SpiffeTrustDomain.trust_domain == trust_domain.lower()).first())
    if not row:
        raise HTTPException(status_code=404, detail="Trust domain not registered")
    db.delete(row)
    db.commit()
    spiffe_assertion.clear_state()
    job_service.log_audit(db, admin.username, "spiffe_trust_domain.delete",
                          details={"trust_domain": trust_domain.lower()})
    return {"detail": "Trust domain removed"}
