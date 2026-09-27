"""Workload access tokens issued by an EXTERNAL IdP, mapped to a service account.

Phase 1 (``services/service_accounts``) made the dashboard its own authorization server:
a workload holds a dashboard client secret and trades it at ``/api/oauth/token``. This is
the other half. A workload that already has an identity at the organisation's IdP -- an
Entra app registration or managed identity, an Okta or Keycloak service client -- calls
the dashboard with the access token THAT IdP issued, and holds no dashboard secret at all.

The trust is three settings (config-only; nothing is accepted until the audience is set):

  ``workload_idp_issuer``          blank = the SSO issuer (``oidc_issuer``)
  ``workload_idp_audience``        what the token's ``aud`` must be. REQUIRED -- a token
                                   minted for some other API at the same IdP must not work
                                   here. Space-separated to accept more than one spelling
                                   (Entra: ``api://<app-id>`` and the bare app id).
  ``workload_idp_extra_issuers``   more ``iss`` values signed by the same keys (Entra's v1
                                   ``https://sts.windows.net/<tid>/`` beside the v2 issuer).

and the mapping is ``ExternalWorkloadIdentity`` -- ``(issuer, sub)`` to a service account.
Why ``sub`` and never ``azp`` is argued on the model: it is the difference between "this
client" and "anyone who signs in through this client".

What a verified token does NOT carry is permission. IdP scopes and roles are not
translated: the mapped service account's grants are the whole of what it may do, exactly
as for a Phase-1 token, so there is one place to look.
"""
import logging
import time
from datetime import datetime, timedelta

from jose import jwt
from jose.exceptions import JWTError

from . import oidc_service

logger = logging.getLogger(__name__)

# Asymmetric only. The dashboard's own tokens are HS256 under ``jwt_secret_key``; if an
# HMAC alg were accepted here, the IdP's PUBLIC key bytes could be used as an HMAC secret
# to sign a token of anyone's choosing -- the classic algorithm-confusion attack. A token
# that says HS* never reaches this module (``api/auth.resolve_bearer`` routes it away).
ASYMMETRIC_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                   "ES256", "ES384", "ES512")

# A token naming a ``kid`` the cached JWKS lacks is either a key rotation or garbage. A
# rotation needs ONE refetch; garbage must not turn every request into a round trip to the
# IdP, so the refetch is allowed at most this often per issuer.
_KID_REFETCH_INTERVAL = 60
_last_forced: dict = {}

# `last_used_at` is written at most this often per mapping -- it is a hint for an admin,
# and a write on every API call would be a write on every API call.
_TOUCH_INTERVAL = timedelta(minutes=1)

_CLIENT_CLAIMS = ("azp", "appid", "client_id", "cid")


def _cfg(key: str) -> str:
    from . import config_service
    return (config_service.get(key) or "").strip()


def issuer() -> str:
    return _cfg("workload_idp_issuer") or _cfg("oidc_issuer")


def accepted_issuers() -> tuple:
    """Exact ``iss`` strings. NOT normalised: Entra's v1 issuer ends in a slash and its v2
    one does not, and the comparison has to be exact to mean anything."""
    primary = issuer()
    extra = _cfg("workload_idp_extra_issuers").split()
    return tuple(dict.fromkeys([i for i in [primary, *extra] if i]))


def audiences() -> tuple:
    return tuple(_cfg("workload_idp_audience").split())


def is_configured() -> bool:
    return bool(issuer() and audiences())


def is_candidate(token: str) -> bool:
    """Could this be an external workload token? Header only -- nothing is trusted yet.
    Used by ``resolve_bearer`` to route; an HS* token is always the dashboard's own."""
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return False
    return header.get("alg") in ASYMMETRIC_ALGS


def client_of(claims: dict) -> str:
    for key in _CLIENT_CLAIMS:
        if claims.get(key):
            return str(claims[key])
    return ""


def _keys_for(header: dict) -> dict:
    """The issuer's JWKS, refetched once if the token's ``kid`` is not in it."""
    iss = issuer()
    keys = oidc_service.jwks_for(iss)
    kid = header.get("kid")
    known = {k.get("kid") for k in keys.get("keys", [])}
    if kid and kid not in known:
        now = time.time()
        if now - _last_forced.get(iss, 0) >= _KID_REFETCH_INTERVAL:
            _last_forced[iss] = now
            keys = oidc_service.jwks_for(iss, force=True)
    return keys


def verify(token: str) -> dict:
    """Verified claims, or raise ``JWTError``/``OIDCError``. Signature, issuer, audience,
    expiry and not-before are all checked; ``sub`` and ``exp`` must be present."""
    header = jwt.get_unverified_header(token)
    alg = header.get("alg")
    if alg not in ASYMMETRIC_ALGS:
        raise JWTError(f"algorithm {alg!r} is not accepted for workload tokens")
    claims = jwt.decode(
        token,
        _keys_for(header),
        algorithms=[alg],
        issuer=accepted_issuers(),
        options={"verify_aud": False, "verify_at_hash": False, "leeway": 60,
                 "require_exp": True, "require_iss": True, "require_sub": True},
    )
    aud = claims.get("aud")
    got = set(aud if isinstance(aud, list) else [aud] if aud else [])
    if not got & set(audiences()):
        raise JWTError("token audience is not this dashboard's workload audience")
    return claims


def resolve(token: str, db):
    """The service account an external workload token maps to, or None.

    Every failure is the same None -- bad signature, wrong audience, no mapping, disabled
    mapping, a disabled or non-service account -- so the caller answers one 401.
    """
    from ..database import ExternalWorkloadIdentity, User
    from .service_accounts import service_account_problem

    if not is_configured():
        return None
    try:
        claims = verify(token)
    except (JWTError, oidc_service.OIDCError) as exc:
        logger.info("external workload token refused: %s", exc)
        return None
    except Exception:  # noqa: BLE001 -- a malformed token is a failed auth, not a 500
        logger.warning("external workload token refused: malformed", exc_info=True)
        return None

    mapping = (db.query(ExternalWorkloadIdentity)
               .filter(ExternalWorkloadIdentity.issuer == claims["iss"],
                       ExternalWorkloadIdentity.subject == str(claims["sub"]),
                       ExternalWorkloadIdentity.is_active == True)  # noqa: E712
               .first())
    if not mapping:
        logger.info("external workload token for unmapped subject %r (client %r)",
                    claims.get("sub"), client_of(claims))
        return None
    if mapping.expected_client and client_of(claims) != mapping.expected_client:
        logger.warning("external workload token for %r came from client %r, expected %r",
                       mapping.subject, client_of(claims), mapping.expected_client)
        return None
    user = db.query(User).filter(User.id == mapping.user_id).first()
    if service_account_problem(user):
        return None

    now = datetime.utcnow()
    if not mapping.last_used_at or now - mapping.last_used_at > _TOUCH_INTERVAL:
        mapping.last_used_at = now
        db.commit()
        db.refresh(user)
    # Unscoped: the service account's own grants are the whole of it. Set explicitly so an
    # instance that was resolved earlier in this session by a scoped token cannot keep that
    # scope -- or lose it -- by accident.
    user._token_scope = None
    return user


def clear_state() -> None:
    """For config changes and tests."""
    _last_forced.clear()
