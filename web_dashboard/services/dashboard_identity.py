"""The dashboard as a SPIFFE workload: token files, its issuer, and the public JWKS.

docs/design/dashboard-workload-identity.md, Slice 1. Off unless
``dashboard_spiffe_identity_enabled``. When on, the dashboard keeps one JWT-SVID file per
configured audience in ``SPIFFE_TOKEN_DIR`` — the form every federated consumer reads:
boto3's ``AWS_WEB_IDENTITY_TOKEN_FILE``, a GCP ``external_account`` credential's
``credential_source.file``, Azure's assertion file, Workload Credentials' ``file``
platform. Nothing here changes which credential a cloud call uses: a stored key still
wins, exactly as before, and an operator retires it on purpose.

**One writer, any number of readers.** The token loop in ``main.py`` calls
:func:`refresh` every minute in each app worker; an ``flock`` on the directory makes the
second worker find fresh files and mint nothing. Files are written to a temporary name
and renamed, so a reader never sees half a token.

**A failed mint keeps the old file.** It may still be valid for minutes, and a consumer
that reads an expired one fails with an expired-token error — which is what happened. It
never falls back to anything else.

**The issuer must match the server's.** ``jwt_issuer`` in the SPIRE server config is set
from ``SPIRE_JWT_ISSUER`` at compose time; :func:`issuer` is what this side publishes
discovery under. A minted token whose ``iss`` differs is refused rather than written: a
cloud would reject it anyway, with an error that names neither file.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import threading
import time
from typing import Optional

from . import config_service

logger = logging.getLogger(__name__)

ENABLED = "dashboard_spiffe_identity_enabled"
ISSUER = "dashboard_spiffe_issuer"
AUD_AWS = "dashboard_spiffe_aud_aws"
AUD_AZURE = "dashboard_spiffe_aud_azure"
AUD_GCP = "dashboard_spiffe_aud_gcp"
AUD_WLC = "dashboard_spiffe_aud_wlc"

ISSUER_PATH = "/spiffe"
DEFAULT_TOKEN_DIR = "/run/spiffe-tokens"
TOKEN_TTL_S = 900                   # re-minted at half this
STATUS_FILE = "status.json"
LOCK_FILE = ".lock"

AWS_AUDIENCE = "sts.amazonaws.com"
AZURE_AUDIENCE = "api://AzureADTokenExchange"
GCP_AUDIENCE_PREFIX = "//iam.googleapis.com/"
# File names this module owns. Anything else in the directory is left alone.
TOKEN_NAMES = ("aws", "azure", "gcp", "wlc")

JWKS_CACHE_S = 300
_PUBLIC_JWK_MEMBERS = ("kty", "kid", "crv", "x", "y", "n", "e")
_ALG_BY_CURVE = {"P-256": "ES256", "P-384": "ES384", "P-521": "ES512"}

_jwks_cache: dict = {"at": 0.0, "keys": None}
_jwks_lock = threading.Lock()


class IdentityError(Exception):
    """A configuration problem an operator can fix. The message is shown as-is."""


def enabled() -> bool:
    return config_service.get_bool(ENABLED)


def token_dir() -> str:
    return os.environ.get("SPIFFE_TOKEN_DIR", "").strip() or DEFAULT_TOKEN_DIR


def issuer() -> str:
    """The issuer this dashboard publishes discovery under.

    The setting wins. Otherwise the pinned agent audience — the hostname the agent gateway
    publishes, which is the part of this install designed to be reachable from outside —
    plus ``/spiffe``. Empty when neither is known; nothing is minted until it is.
    """
    explicit = (config_service.get(ISSUER) or "").strip().rstrip("/")
    if explicit:
        return explicit
    from . import agent_service
    base = (config_service.get(agent_service.AUDIENCE_CONFIG) or "").strip().rstrip("/")
    return f"{base}{ISSUER_PATH}" if base else ""


def audiences() -> dict:
    """``{file name: audience}`` for every audience switched on. Raises IdentityError
    for one that is on but cannot be resolved, naming the setting to fix."""
    out = {}
    if config_service.get_bool(AUD_AWS):
        out["aws"] = AWS_AUDIENCE
    if config_service.get_bool(AUD_AZURE):
        out["azure"] = AZURE_AUDIENCE
    gcp = (config_service.get(AUD_GCP) or "").strip()
    if gcp:
        if not gcp.startswith(GCP_AUDIENCE_PREFIX):
            raise IdentityError(
                "The GCP audience must be the workload identity provider's full resource "
                "name, starting //iam.googleapis.com/projects/…/providers/….")
        out["gcp"] = gcp
    if config_service.get_bool(AUD_WLC):
        wlc = (config_service.get("wlc_identity_audience") or "").strip()
        if not wlc:
            raise IdentityError(
                "Workload Credentials is ticked but its identity audience is blank. Set it "
                "under Settings → Workload Credentials first.")
        out["wlc"] = wlc
    return out


def claims(token: str) -> dict:
    """The payload of a JWT, NOT verified. Only ever used on tokens this dashboard minted
    itself or wrote itself, to decide whether to mint again."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _aud_list(c: dict) -> list:
    aud = c.get("aud")
    return aud if isinstance(aud, list) else [aud] if aud else []


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


def _write_atomic(directory: str, name: str, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, os.path.join(directory, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _due(path: str, audience: str, now: float) -> bool:
    c = claims(_read(path))
    if not c or audience not in _aud_list(c):
        return True
    return float(c.get("exp") or 0) - now < TOKEN_TTL_S / 2


def _check_minted(token: str, audience: str, want_sub: str, want_iss: str) -> None:
    c = claims(token)
    if c.get("sub") != want_sub or audience not in _aud_list(c):
        raise IdentityError("the SPIRE server returned a token for a different subject "
                            "or audience than was asked for")
    if (c.get("iss") or "") != want_iss:
        raise IdentityError(
            f"the SPIRE server signs with issuer {c.get('iss') or '(none)'!r}, but this "
            f"dashboard publishes discovery as {want_iss!r}. Set SPIRE_JWT_ISSUER to the "
            f"same value (docker-compose.spire.yml) and restart the SPIRE server, or change "
            f"the issuer under Settings → Remote agents.")


def refresh(now: Optional[float] = None) -> dict:
    """Bring every configured token file up to date. Never raises: it runs from a
    background loop. Returns the status it recorded."""
    if not enabled():
        return {"enabled": False}
    now = time.time() if now is None else now
    directory = token_dir()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        lock = open(os.path.join(directory, LOCK_FILE), "a", encoding="ascii")
    except OSError as exc:
        logger.warning("dashboard identity: token directory %s is not writable: %s",
                       directory, exc)
        return {"enabled": True, "error": f"{directory} is not writable: {exc}"}
    with lock:
        _flock(lock)
        try:
            status = _refresh_locked(directory, now)
        except Exception as exc:  # noqa: BLE001 -- background loop: record and carry on
            logger.exception("dashboard identity: refresh failed")
            status = {"enabled": True, "checked_at": now,
                      "error": f"refresh failed: {type(exc).__name__}"}
        # GCP's external_account config beside its token, for Terraform and Packer
        # (services/cloud_federation). Not a secret; written by the same one writer.
        try:
            from . import cloud_federation
            cloud_federation.write_gcp_config()
        except OSError as exc:
            logger.warning("dashboard identity: could not write the GCP config: %s", exc)
        try:
            _write_atomic(directory, STATUS_FILE, json.dumps(status), 0o600)
        except OSError as exc:
            logger.warning("dashboard identity: could not record status: %s", exc)
    return status


def _flock(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows dev box; one worker there anyway
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _refresh_locked(directory: str, now: float) -> dict:
    from . import dashboard_spire
    status = {"enabled": True, "checked_at": now, "issuer": issuer(), "tokens": {}}
    previous = _read_status(directory).get("tokens") or {}
    try:
        wanted = audiences()
        if not status["issuer"]:
            raise IdentityError(
                "No issuer: set one under Settings → Remote agents, or pin the agent "
                "audience (the agent gateway's public URL) first.")
        if not status["issuer"].startswith("https://"):
            raise IdentityError(
                f"The issuer {status['issuer']!r} is not https. AWS, Azure and GCP fetch "
                f"discovery over TLS only, so a token naming it would be refused.")
    except IdentityError as exc:
        status["error"] = str(exc)
        return status

    # Files for an audience switched off are removed: a consumer still pointed at one
    # should fail now, not in fifteen minutes when it expires.
    for name in TOKEN_NAMES:
        if name not in wanted:
            try:
                os.unlink(os.path.join(directory, f"{name}.jwt"))
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("dashboard identity: could not remove %s.jwt: %s", name, exc)

    td = None
    for name, audience in wanted.items():
        path = os.path.join(directory, f"{name}.jwt")
        entry = dict(previous.get(name) or {})
        entry["audience"] = audience
        if not _due(path, audience, now):
            entry.pop("error", None)
            status["tokens"][name] = entry
            continue
        try:
            td = td or dashboard_spire.trust_domain()
            token = dashboard_spire.mint_jwt(audience, TOKEN_TTL_S, td)
            _check_minted(token, audience, dashboard_spire.dashboard_id(td),
                          status["issuer"])
            _write_atomic(directory, f"{name}.jwt", token, 0o640)
            entry.update(minted_at=now, expires_at=claims(token).get("exp"))
            entry.pop("error", None)
        except (dashboard_spire.DashboardSpireError, IdentityError, OSError) as exc:
            # The old file, if any, stays: it may still be good for minutes.
            entry["error"] = str(exc)
            logger.warning("dashboard identity: %s token not refreshed: %s", name, exc)
        status["tokens"][name] = entry
    return status


def _read_status(directory: str) -> dict:
    try:
        with open(os.path.join(directory, STATUS_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status() -> dict:
    """What Settings shows: the recorded status, plus what is actually in each file now
    (the file is the truth — the status is only as fresh as the last pass)."""
    from . import cloud_federation
    out = {"enabled": enabled(), "issuer": issuer(), "token_dir": token_dir(),
           # Which rung each cloud's calls use right now — so an operator who clears a
           # stored key sees federation take over rather than inferring it.
           "sources": cloud_federation.sources()}
    if not out["enabled"]:
        return out
    directory = token_dir()
    recorded = _read_status(directory)
    out["checked_at"] = recorded.get("checked_at")
    if recorded.get("error"):
        out["error"] = recorded["error"]
    tokens = {}
    for name, entry in (recorded.get("tokens") or {}).items():
        c = claims(_read(os.path.join(directory, f"{name}.jwt")))
        tokens[name] = {
            "audience": entry.get("audience"),
            "path": os.path.join(directory, f"{name}.jwt"),
            "present": bool(c),
            "subject": c.get("sub"),
            "issuer": c.get("iss"),
            "expires_at": c.get("exp"),
            "minted_at": entry.get("minted_at"),
            "error": entry.get("error"),
        }
    out["tokens"] = tokens
    return out


# ── discovery ─────────────────────────────────────────────────────────────────

def public_jwks(bundle: dict) -> list:
    """The JWT signing keys from a SPIFFE bundle, reduced to public members only.

    A whitelist rather than dropping ``d``: whatever else a future bundle carries, only
    the members a verifier needs leave this process.
    """
    keys = []
    for key in (bundle or {}).get("keys") or []:
        if not isinstance(key, dict) or key.get("use") != "jwt-svid" or not key.get("kid"):
            continue
        jwk = {m: key[m] for m in _PUBLIC_JWK_MEMBERS if m in key}
        if jwk.get("kty") == "EC" and jwk.get("crv") in _ALG_BY_CURVE:
            jwk["alg"] = _ALG_BY_CURVE[jwk["crv"]]
        elif jwk.get("kty") == "RSA":
            jwk["alg"] = "RS256"
        else:
            continue
        jwk["use"] = "sig"
        keys.append(jwk)
    return keys


def _stored_bundle() -> dict:
    from ..database import SessionLocal, SpiffeTrustDomain
    from . import dashboard_spire
    db = SessionLocal()
    try:
        rec = (db.query(SpiffeTrustDomain)
               .filter(SpiffeTrustDomain.created_by == dashboard_spire.OWNER).first())
        return json.loads(rec.bundle_json) if rec and rec.bundle_json else {}
    except ValueError:
        return {}
    finally:
        db.close()


def jwks(now: Optional[float] = None) -> list:
    """Live from the server, cached briefly; the stored daily copy if the server is down.
    An empty list means neither is available, and the route answers 503."""
    from . import dashboard_spire
    now = time.monotonic() if now is None else now
    with _jwks_lock:
        if _jwks_cache["keys"] is not None and now - _jwks_cache["at"] < JWKS_CACHE_S:
            return _jwks_cache["keys"]
    try:
        keys = public_jwks(dashboard_spire.live_bundle())
    except dashboard_spire.DashboardSpireError as exc:
        logger.warning("dashboard identity: live bundle unavailable, serving the stored "
                       "copy: %s", exc)
        return public_jwks(_stored_bundle())
    with _jwks_lock:
        _jwks_cache.update(at=now, keys=keys)
    return keys


def discovery_document() -> dict:
    """The fields AWS, Azure and GCP read, in the shape SPIRE's own OIDC Discovery
    Provider serves."""
    iss = issuer()
    return {
        "issuer": iss,
        "jwks_uri": f"{iss}/keys",
        "authorization_endpoint": "",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256", "ES256", "ES384"],
    }


def clear_cache() -> None:
    with _jwks_lock:
        _jwks_cache.update(at=0.0, keys=None)
