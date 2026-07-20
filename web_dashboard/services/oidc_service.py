"""Generic OpenID Connect login.

The dashboard's original SSO was Entra-specific: hardcoded
``login.microsoftonline.com`` URLs, ``azure_oauth_*`` config keys, and MSAL. Every
additional provider added that way is another bespoke code path.

This module drives the flow from the provider's **discovery document**
(``<issuer>/.well-known/openid-configuration``) instead, so one implementation
covers any compliant IdP — Okta, Auth0, Keycloak, Authentik, Authelia, Google
Workspace, JumpCloud, Ping, GitLab, and Entra itself.

Note GitHub is *not* OIDC (no discovery document, no ID token); supporting it
would need a separate OAuth2 path and is deliberately out of scope here.

The existing Entra routes are untouched — this is additive, so installs already
using ``azure_oauth_*`` keep working exactly as before.
"""
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from jose import jwt
from jose.exceptions import JWTError

logger = logging.getLogger(__name__)

# Discovery documents and JWKS are cached — they change rarely and every login
# would otherwise cost two extra round trips to the IdP.
_DISCOVERY_TTL = 3600
_cache: dict = {}


class OIDCError(Exception):
    """Configuration or protocol failure in the OIDC flow."""


def _cfg(key: str, default: str = "") -> str:
    from . import config_service
    return (config_service.get(key) or default).strip()


def is_configured() -> bool:
    """True when enough is set to attempt a login."""
    return bool(_cfg("oidc_issuer") and _cfg("oidc_client_id"))


def provider_label() -> str:
    """Display name for the login button; falls back to the issuer host."""
    label = _cfg("oidc_provider_name")
    if label:
        return label
    issuer = _cfg("oidc_issuer")
    if not issuer:
        return "SSO"
    return issuer.split("://", 1)[-1].split("/", 1)[0]


def _fetch(url: str) -> dict:
    resp = httpx.get(url, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def discovery() -> dict:
    """The provider's OIDC discovery document (cached).

    Raises ``OIDCError`` with the issuer in the message — a typo'd issuer is the
    single most common misconfiguration, and the bare httpx error doesn't say
    which URL failed.
    """
    issuer = _cfg("oidc_issuer").rstrip("/")
    if not issuer:
        raise OIDCError("No OIDC issuer configured.")
    hit = _cache.get(issuer)
    if hit and hit["expires"] > time.time():
        return hit["doc"]
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        doc = _fetch(url)
    except Exception as e:
        raise OIDCError(f"Could not fetch the OIDC discovery document from {url}: {e}") from e
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(required):
            raise OIDCError(f"Discovery document at {url} is missing {required!r}.")
    _cache[issuer] = {"doc": doc, "expires": time.time() + _DISCOVERY_TTL}
    return doc


def _jwks() -> dict:
    doc = discovery()
    uri = doc["jwks_uri"]
    hit = _cache.get(uri)
    if hit and hit["expires"] > time.time():
        return hit["doc"]
    keys = _fetch(uri)
    _cache[uri] = {"doc": keys, "expires": time.time() + _DISCOVERY_TTL}
    return keys


def scopes() -> str:
    """Requested scopes. ``openid`` is mandatory; the rest are configurable so an
    operator can add whatever their IdP needs to emit a groups claim."""
    configured = _cfg("oidc_scopes") or "openid profile email groups"
    parts = configured.split()
    if "openid" not in parts:
        parts.insert(0, "openid")
    return " ".join(parts)


def authorization_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    """Authorization-code + PKCE. PKCE is always sent: it costs nothing for a
    confidential client and is required by some IdPs (and by Swagger UI)."""
    doc = discovery()
    return doc["authorization_endpoint"] + "?" + urlencode({
        "client_id": _cfg("oidc_client_id"),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scopes(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })


def exchange_code(code: str, redirect_uri: str, code_verifier: str) -> dict:
    """Trade the authorization code for tokens."""
    doc = discovery()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _cfg("oidc_client_id"),
        "code_verifier": code_verifier,
    }
    secret = _cfg("oidc_client_secret")
    if secret:
        data["client_secret"] = secret
    try:
        resp = httpx.post(doc["token_endpoint"], data=data, timeout=15.0)
    except Exception as e:
        raise OIDCError(f"Token endpoint request failed: {e}") from e
    if resp.status_code >= 400:
        # The body carries the provider's error description, which is the useful
        # part; it contains no token material at this point.
        raise OIDCError(f"Token exchange rejected ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if not payload.get("id_token"):
        raise OIDCError("Token response contained no id_token — is the 'openid' scope granted?")
    return payload


def validate_id_token(id_token: str) -> dict:
    """Verify signature, issuer and audience, and return the claims.

    Signature verification is the load-bearing part: without it, anyone who can
    reach the callback could mint their own token and log in as anyone.
    """
    doc = discovery()
    try:
        return jwt.decode(
            id_token,
            _jwks(),
            algorithms=doc.get("id_token_signing_alg_values_supported") or ["RS256"],
            audience=_cfg("oidc_client_id"),
            issuer=doc.get("issuer") or _cfg("oidc_issuer").rstrip("/"),
            options={"verify_at_hash": False},  # no access-token hash check; we don't use it
        )
    except JWTError as e:
        raise OIDCError(f"ID token failed validation: {e}") from e


def claim_identity(claims: dict) -> tuple:
    """Pull (subject, email, display_name, groups) out of provider claims.

    Providers disagree on which claim carries the email and the groups, so the
    email falls through the common alternatives and the groups claim name is
    configurable (``groups`` for Keycloak/Authentik, ``roles`` for some Okta
    setups, ``groups`` for Entra).
    """
    subject = claims.get("sub") or ""
    email = (claims.get("email")
             or claims.get("preferred_username")
             or claims.get("upn")
             or claims.get("unique_name")
             or "")
    display_name = claims.get("name") or claims.get("given_name") or ""
    groups_claim = _cfg("oidc_groups_claim") or "groups"
    raw_groups = claims.get(groups_claim) or []
    if isinstance(raw_groups, str):
        raw_groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
    return subject, email, display_name, [str(g) for g in raw_groups]


def clear_cache() -> None:
    """Drop cached discovery/JWKS — called when the config changes so an operator
    doesn't have to wait out the TTL after fixing an issuer."""
    _cache.clear()
