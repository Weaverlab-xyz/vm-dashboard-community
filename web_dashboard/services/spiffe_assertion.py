"""SPIFFE JWT-SVIDs as OAuth client assertions: a workload that holds NO secret.

Phases 1 and 2 still leave the agent worker holding something: a dashboard client secret,
or an IdP credential that gets it a token. SPIRE already gives the worker an identity it
does not have to store -- it attests to the local agent and receives short-lived SVIDs.
This module lets that identity authenticate at the dashboard's own token endpoint:

    POST /api/oauth/token
      grant_type=client_credentials
      client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe
      client_assertion=<JWT-SVID, aud = this dashboard's token endpoint>
      [client_id=vmsa_...]

``jwt-spiffe`` is the IETF OAuth SPIFFE client-authentication profile; the RFC 7523
``jwt-bearer`` type is accepted too, for client libraries that only know that one. The
SVID's ``sub`` (its SPIFFE ID) names an OAuth client created with
``auth_method="spiffe_jwt"``, and the rest is Phase 1 unchanged: a short-lived dashboard
token, the client re-checked on every request, the service account's permissions.

What is verified, and why each matters:

  * **signature** against the trust domain's JWT-SVID keys (``SpiffeTrustDomain``: a live
    JWKS URL, or a stored SPIFFE bundle). Asymmetric algorithms only, for the reason
    ``external_workload.ASYMMETRIC_ALGS`` gives;
  * **audience** is this dashboard's token endpoint (or its issuer URL). An SVID minted
    for any other relying party -- the k3s API server, Workload Credentials -- must not be
    replayable here, and that is the only thing that stops it;
  * **lifetime** is capped: an assertion is a proof of possession for one exchange, and a
    long-lived one is a bearer credential with extra steps;
  * **single use**: SPIRE's JWT-SVIDs carry no ``jti``, so the assertion's own hash is
    recorded until it expires and a second presentation is refused.
"""
import hashlib
import json
import logging
import ssl
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
from jose import jwt
from jose.exceptions import JWTError

from .external_workload import ASYMMETRIC_ALGS

logger = logging.getLogger(__name__)

ASSERTION_TYPES = (
    "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
)
AUTH_METHOD = "spiffe_jwt"

# An assertion lives for one exchange. SPIRE's default JWT-SVID TTL is five minutes; an
# hour is the ceiling so a server configured longer still works, and anything past it is
# refused rather than trusted as a standing credential.
MAX_ASSERTION_LIFETIME = 3600
LEEWAY = 30

# Live JWKS: short cache, because the point of the URL source is being current across a
# rotation; an unknown kid refetches at most once a minute per trust domain.
_JWKS_TTL = 300
_KID_REFETCH_INTERVAL = 60
_cache: dict = {}
_last_forced: dict = {}

_REPLAY_PREFIX = "vmcli:spiffe:assertion:"

# A bundle older than this is flagged in the UI: SPIRE rotates JWT keys within ca_ttl
# (the lab's is 168h), publishing the next key ahead of time, so a week-old capture is
# about when it starts refusing valid SVIDs.
BUNDLE_STALE_AFTER = timedelta(days=5)


class AssertionError_(Exception):
    """A refused assertion. The message is for the log, never the client: the endpoint
    answers every failure with the same ``invalid_client``."""


# ── SPIFFE IDs ───────────────────────────────────────────────────────────────

def trust_domain_of(spiffe_id: str) -> str:
    """``spiffe://td/path`` → ``td``; "" for anything that is not a SPIFFE ID."""
    sid = (spiffe_id or "").strip()
    if not sid.startswith("spiffe://"):
        return ""
    td = sid[len("spiffe://"):].split("/", 1)[0]
    return td.lower() if td and "@" not in td and ":" not in td else ""


def valid_spiffe_id(spiffe_id: str) -> bool:
    sid = (spiffe_id or "").strip()
    return bool(trust_domain_of(sid)) and len(sid) <= 500 and not any(
        c.isspace() for c in sid) and "?" not in sid and "#" not in sid


# ── Keys ─────────────────────────────────────────────────────────────────────

def _fetch_jwks(url: str, ca_pem: str = "", server_name: str = "") -> dict:
    """GET a JWKS, optionally verifying TLS for ``server_name`` instead of the URL's host.

    ``server_name`` is for a provider reached by address whose certificate names
    something that only resolves elsewhere -- a Workload Lab's ``oidc.<trust-domain>``.
    It becomes the TLS SNI and the name the certificate is verified against (httpx's
    ``sni_hostname`` extension), and the Host header, because the SPIRE OIDC provider
    refuses a Host that is not in its ``domains``. Verification is never switched off:
    the pinned CA and the name are both still checked.
    """
    verify = ssl.create_default_context(cadata=ca_pem) if ca_pem.strip() else True
    headers, extensions = {}, {}
    if server_name:
        from urllib.parse import urlsplit
        port = urlsplit(url).port
        extensions["sni_hostname"] = server_name
        headers["Host"] = f"{server_name}:{port}" if port and port != 443 else server_name
    with httpx.Client(verify=verify, timeout=10.0, follow_redirects=False) as client:
        resp = client.get(url, headers=headers, extensions=extensions)
    resp.raise_for_status()
    return resp.json()


def bundle_keys(bundle_json: str) -> list:
    """The JWT-SVID keys of a SPIFFE bundle. Its X.509 roots are dropped: they sign
    certificates, not JWTs, and a verifier that tried them would only be slower."""
    try:
        doc = json.loads(bundle_json or "{}")
    except ValueError as exc:
        raise AssertionError_(f"stored bundle is not JSON: {exc}") from exc
    return [k for k in doc.get("keys", []) if k.get("use") == "jwt-svid"]


def _url_keys(row, force: bool = False) -> list:
    hit = _cache.get(row.trust_domain)
    if hit and hit["expires"] > time.time() and not force:
        return hit["keys"]
    try:
        doc = _fetch_jwks(row.jwks_url, row.ca_pem or "",
                          getattr(row, "tls_server_name", None) or "")
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refused assertion
        raise AssertionError_(f"could not fetch JWKS for {row.trust_domain}: {exc}") from exc
    # An OIDC Discovery Provider marks its keys "sig"; a SPIFFE bundle endpoint uses
    # "jwt-svid" and also carries X.509 roots, which have no business here.
    keys = [k for k in doc.get("keys", []) if k.get("use") in (None, "sig", "jwt-svid")]
    _cache[row.trust_domain] = {"keys": keys, "expires": time.time() + _JWKS_TTL}
    return keys


def keys_for(row, kid: Optional[str] = None) -> list:
    if row.jwks_url:
        try:
            keys = _url_keys(row)
            if kid and kid not in {k.get("kid") for k in keys}:
                now = time.time()
                if now - _last_forced.get(row.trust_domain, 0) >= _KID_REFETCH_INTERVAL:
                    _last_forced[row.trust_domain] = now
                    keys = _url_keys(row, force=True)
            return keys
        except AssertionError_ as exc:
            # The URL is the better source, but a lab's provider can be down or its
            # certificate lapsed; a stored bundle beside it keeps verification working,
            # said out loud because it will go stale.
            if not row.bundle_json:
                raise
            logger.warning("JWKS URL for %s failed (%s); using the stored bundle from %s",
                           row.trust_domain, exc, row.bundle_captured_at)
    if row.bundle_json:
        return bundle_keys(row.bundle_json)
    raise AssertionError_(f"trust domain {row.trust_domain} has neither a JWKS URL nor a bundle")


def is_stale(row) -> bool:
    return bool(not row.jwks_url and row.bundle_captured_at
                and datetime.utcnow() - row.bundle_captured_at > BUNDLE_STALE_AFTER)


# ── Verification ─────────────────────────────────────────────────────────────

def verify(db, assertion: str, audiences, now: Optional[float] = None) -> dict:
    """Verified JWT-SVID claims, or raise ``AssertionError_``. Does NOT consume it; the
    caller does that once the client is resolved (``consume``)."""
    from ..database import SpiffeTrustDomain

    now = now or time.time()
    try:
        header = jwt.get_unverified_header(assertion)
        unverified = jwt.get_unverified_claims(assertion)
    except JWTError as exc:
        raise AssertionError_(f"malformed assertion: {exc}") from exc
    alg = header.get("alg")
    if alg not in ASYMMETRIC_ALGS:
        raise AssertionError_(f"algorithm {alg!r} is not accepted")
    td = trust_domain_of(str(unverified.get("sub", "")))
    if not td:
        raise AssertionError_("assertion subject is not a SPIFFE ID")
    row = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == td).first()
    if not row:
        raise AssertionError_(f"trust domain {td} is not registered")
    keys = keys_for(row, header.get("kid"))
    if not keys:
        raise AssertionError_(f"trust domain {td} has no JWT-SVID keys")
    try:
        claims = jwt.decode(
            assertion, {"keys": keys}, algorithms=[alg],
            options={"verify_aud": False, "verify_at_hash": False, "leeway": LEEWAY,
                     "require_exp": True, "require_sub": True})
    except JWTError as exc:
        raise AssertionError_(f"assertion failed verification: {exc}") from exc

    aud = claims.get("aud")
    got = set(aud if isinstance(aud, list) else [aud] if aud else [])
    if not got & set(audiences):
        raise AssertionError_(
            f"assertion audience {sorted(got)} is not this dashboard's token endpoint")
    if float(claims["exp"]) - now > MAX_ASSERTION_LIFETIME:
        raise AssertionError_(
            f"assertion lives {int(float(claims['exp']) - now)}s; the ceiling is "
            f"{MAX_ASSERTION_LIFETIME}s")
    return claims


def consume(db, assertion: str, exp: float) -> None:
    """Record the assertion as used, or raise if it already was.

    The primary key IS the lock: two workers presenting the same assertion at once both
    try to insert, and the second one's insert fails -- no read-then-write window.
    """
    from sqlalchemy.exc import IntegrityError
    from ..database import EphemeralState

    key = _REPLAY_PREFIX + hashlib.sha256(assertion.encode()).hexdigest()
    # Opportunistic sweep of this module's own expired rows, in the same transaction.
    # The login ceremonies sweep the table too, but an install whose only traffic is
    # agents would otherwise grow one row per exchange forever.
    db.query(EphemeralState).filter(
        EphemeralState.key.like(_REPLAY_PREFIX + "%"),
        EphemeralState.expires_at < datetime.utcnow()).delete(synchronize_session=False)
    try:
        db.add(EphemeralState(key=key, value="",
                              expires_at=datetime.utcfromtimestamp(float(exp) + LEEWAY)))
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        # An expired row can only be one whose assertion is now expired too, so a hit
        # here is a replay either way.
        raise AssertionError_("assertion was already used") from exc


def authenticate(db, assertion: str, audiences, client_id: str = ""):
    """The OAuth client a valid, unused JWT-SVID authenticates, or raise."""
    from ..database import OAuthClient, User
    from .service_accounts import service_account_problem

    claims = verify(db, assertion, audiences)
    sub = str(claims["sub"])
    q = (db.query(OAuthClient)
         .filter(OAuthClient.spiffe_id == sub, OAuthClient.auth_method == AUTH_METHOD,
                 OAuthClient.is_active == True))  # noqa: E712
    if client_id:
        q = q.filter(OAuthClient.client_id == client_id)
    client = q.first()
    if not client:
        raise AssertionError_(f"no active SVID client for {sub}")
    user = db.query(User).filter(User.id == client.user_id).first()
    problem = service_account_problem(user)
    if problem:
        raise AssertionError_(problem)
    consume(db, assertion, claims["exp"])
    return client


def clear_state() -> None:
    _cache.clear()
    _last_forced.clear()
