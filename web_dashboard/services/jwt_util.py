"""The one place the dashboard touches a JWT library.

python-jose was replaced by PyJWT (GHSA-3qf3-8w2g-rqmx: python-jose <= 3.5.0 lets a
DER-encoded public key bypass its algorithm-confusion guard, and no fixed release exists).
Every call site goes through here, so a library swap is a one-file change and the three
places PyJWT differs from jose are handled once instead of at each caller:

* **Errors.** ``JWTError`` is ``jwt.PyJWTError``, so the existing ``except JWTError`` blocks
  keep catching every verification failure (bad signature, expiry, claims, malformed).
* **JWKS.** jose accepted ``{"keys": [...]}`` as a key and tried each; PyJWT wants one key
  object, so :func:`decode_with_jwks` selects by ``kid`` and tries the candidates in order.
* **Options.** jose's ``require_exp`` / ``verify_at_hash`` / ``leeway`` options become
  PyJWT's ``require`` list and ``leeway`` argument.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence, Union

import jwt as _jwt
from jwt import PyJWTError as JWTError  # noqa: F401 - re-exported for callers

__all__ = ["JWTError", "encode", "decode", "decode_with_jwks",
           "get_unverified_header", "get_unverified_claims"]


def encode(payload: dict, key, algorithm: str, headers: Optional[dict] = None) -> str:
    return _jwt.encode(payload, key, algorithm=algorithm, headers=headers)


def decode(token: str, key, algorithms: Sequence[str], **kwargs) -> dict:
    """Verify with a single key (the dashboard's own HS* tokens)."""
    return _jwt.decode(token, key, algorithms=list(algorithms), **kwargs)


def get_unverified_header(token: str) -> dict:
    """The JOSE header, unverified. Routing only; nothing in it is trusted."""
    return _jwt.get_unverified_header(token)


def get_unverified_claims(token: str) -> dict:
    """The claims, unverified, with no expiry or audience check. Routing only."""
    return _jwt.decode(token, options={"verify_signature": False})


def _candidates(jwks: dict, header: dict) -> list:
    keys = [k for k in (jwks or {}).get("keys", []) if isinstance(k, dict)]
    kid = header.get("kid")
    if kid is not None:
        matched = [k for k in keys if k.get("kid") == kid]
        if matched:
            return matched
    return keys


def decode_with_jwks(token: str, jwks: dict, *, algorithms: Sequence[str],
                     audience: Union[str, Iterable[str], None] = None,
                     issuer: Union[str, Sequence[str], None] = None,
                     leeway: float = 0, require: Sequence[str] = (),
                     verify_aud: bool = True) -> dict:
    """Verify against a JWKS: the key whose ``kid`` matches, else each key in turn.

    A key that cannot verify this token (wrong type, wrong curve, unusable JWK) is skipped
    and the next is tried; a token that verifies but fails a CLAIM check (expired, wrong
    issuer, ...) raises at once, since another key cannot change that.
    """
    header = _jwt.get_unverified_header(token)
    options = {"verify_aud": verify_aud, "require": list(require)}
    last: Exception = _jwt.InvalidSignatureError("no key in the key set verified this token")
    for jwk in _candidates(jwks, header):
        try:
            key = _jwt.PyJWK.from_dict(jwk).key
        except _jwt.PyJWTError as exc:
            last = exc
            continue
        try:
            return _jwt.decode(token, key, algorithms=list(algorithms), audience=audience,
                               issuer=issuer, leeway=leeway, options=options)
        except (_jwt.InvalidSignatureError, _jwt.InvalidKeyError) as exc:
            last = exc
    raise last
