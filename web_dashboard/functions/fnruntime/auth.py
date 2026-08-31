"""Shared-secret verification — the inner half of the layered auth model.

The outer half is the cloud's own front door (Lambda Function URL ``AWS_IAM``, the
Azure function host key, Cloud Run ``roles/run.invoker``). The two are INDEPENDENT:
getting past one does not get you past the other. That matters because the front
door is configured differently on each cloud and is sometimes deliberately open
(a public Function URL for a demo), whereas this check is identical everywhere and
always on.

Three properties are load-bearing and are pinned by ``tests/test_function_auth.py``:

1. **Fails closed.** A missing/blank ``FN_SHARED_SECRET`` returns 500, never 200.
   The two-line version of this mistake turns the endpoint into an open door.
2. **Constant-time compare**, so the secret can't be recovered a byte at a time.
3. **Indistinguishable failures.** "no credential" and "wrong credential" return a
   byte-identical 401, so probing tells an attacker nothing.

Stdlib only.
"""
import base64
import binascii
import hmac
import json
import os

from fnruntime import logs, secretref
from fnruntime.contract import Request, Response

# Which header carries the secret, and the scheme prefix to strip. Overridable so
# an operator whose Entitle tenant (or gateway) insists on a custom header can
# adapt without a code change — the Terraform modules pass these through.
_DEFAULT_HEADER = "authorization"
_DEFAULT_PREFIX = "Bearer "

# The 401 body. A module-level constant so "missing" and "wrong" cannot drift apart.
_DENIED = {"error": "unauthorized"}


def _expected() -> str:
    """The configured secret, resolved the same way every other credential is.

    GCP injects it from Secret Manager and Azure resolves it before the worker
    starts, so on those two it is already in FN_SHARED_SECRET. On AWS the module
    keeps it in Secrets Manager and passes only the id, so it is read here — once
    per cold start, then from secretref's cache — and never appears in
    ``lambda:GetFunctionConfiguration`` output.
    """
    return secretref.resolve("FN_SHARED_SECRET")


def _header_name() -> str:
    return (os.environ.get("FN_AUTH_HEADER", "") or _DEFAULT_HEADER).lower()


def _prefix() -> str:
    # An explicitly empty FN_AUTH_PREFIX means "no scheme prefix", which is why
    # this reads the raw value instead of using `or`.
    val = os.environ.get("FN_AUTH_PREFIX")
    return _DEFAULT_PREFIX if val is None else val


def presented_secret(req: Request) -> str:
    """The credential the caller supplied, or ``""``.

    Normally the configured header. Direct invokes (EventBridge / Cloud Scheduler /
    ``aws lambda invoke``) never traverse the HTTP front door and carry no headers
    at all, so for those — and ONLY those — a ``secret`` field in the synthesized
    body is also accepted. ``secret`` is in the redaction set, so it cannot be
    logged (see fnruntime.logs).
    """
    raw = req.header(_header_name())
    prefix = _prefix()
    if prefix and raw.startswith(prefix):
        raw = raw[len(prefix):]
    if not raw and req.source == "aws_direct":
        raw = str(req.json().get("secret") or "")
    return raw.strip()


def verify(req: Request):
    """``None`` when the caller is authorized, else the ``Response`` to return.

    Never raises. ``dispatch`` calls this outside its own try/except, so an escaping
    exception would skip the log line and leave the platform to render an opaque
    502 — the one failure mode worse than a clear 500.
    """
    try:
        expected = _expected().strip()
    except Exception as exc:
        # The secret could not be resolved: Secrets Manager unreachable, the role's
        # grant removed, a malformed payload. Fail CLOSED, and say why in the
        # function's own log — never in the response, which would tell an
        # unauthenticated caller how the function is configured.
        logs.emit("error", "shared_secret_unresolvable", error_type=type(exc).__name__)
        return Response(500, {"error": "function not configured"})
    if not expected:
        # Fail CLOSED. Deploying without the secret wired up is an operator error,
        # and the one outcome we must never produce is a working open endpoint.
        return Response(500, {"error": "function not configured"})
    presented = presented_secret(req)
    if not presented:
        return Response(401, dict(_DENIED))
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return Response(401, dict(_DENIED))
    return None


# ── GCP OIDC: the inner gate for a caller the dashboard did not issue a secret to ──
#
# One workload has a caller it cannot hand a shared secret to. The Password Safe GCP
# Cloud SQL plugin's "cloud-run" channel authenticates with a Google-issued OIDC ID
# token in the SAME Authorization header this module reads, so verify() above would
# compare an ID token against the dashboard's secret and 401 every request.
#
# The answer is a different inner gate, not the absence of one. Cloud Run's front door
# has ALREADY validated the token's signature, expiry and audience and confirmed the
# caller holds roles/run.invoker before the request reached this container — that is
# exactly what auth_mode = "run_invoker" buys, and re-verifying the signature would
# mean fetching and caching Google's JWKS from a module whose whole contract is
# "stdlib only". So the claims are parsed for AUTHORIZATION (which principal, for which
# audience) on top of an AUTHENTICATION decision the platform already made.
#
# That reasoning holds only while the platform really is gating ingress. Deploying such
# a workload with an open front door is refused at the click
# (cloud_function_service._check_front_door) AND refused here, because the two
# checks live in different trust domains and the second one is the one an attacker
# would have to get past.
_OIDC_AUDIENCE_ENV = "FN_DBOPS_AUDIENCE"
_OIDC_ALLOWED_ENV = "FN_DBOPS_ALLOWED_INVOKERS"


def _b64url_json(segment: str) -> dict:
    """One JWT segment as a dict. Raises on anything that is not one."""
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def claims(req: Request) -> dict:
    """The bearer token's claims, or ``{}``.

    Unverified BY DESIGN — see the module note above. Never use this to decide
    whether a caller is authenticated; only to decide what an already-authenticated
    caller may do, and for logging.
    """
    raw = req.header("authorization")
    if raw.lower().startswith("bearer "):
        raw = raw[7:]
    parts = raw.strip().split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = _b64url_json(parts[1])
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def verify_gcp_oidc(req: Request):
    """``None`` when the caller is authorized, else the ``Response`` to return.

    Same three properties verify() is pinned on: fails closed when it is not
    configured, and returns a byte-identical 401 whether the token is absent,
    unparseable, for another audience or from an unlisted principal — probing an
    endpoint that changes credentials must reveal nothing about how it is configured.
    """
    # FAIL CLOSED unless the front door is one that verifies tokens. Without the
    # platform's own check there is nothing standing behind this one, and parsing an
    # unverified token would then be the ONLY gate — an attacker writes their own.
    #
    # An ALLOWLIST, not a denylist of "none": the Terraform module always sets this,
    # so a missing value means a hand-rolled or pre-upgrade deploy whose front door we
    # cannot vouch for, and the safe reading of "I don't know" is "no".
    if (os.environ.get("FN_AUTH_MODE_FRONT_DOOR", "") or "").strip().lower() not in (
            "run_invoker",):
        logs.emit("error", "oidc_front_door_unverified")
        return Response(500, {"error": "function not configured"})

    audience = (os.environ.get(_OIDC_AUDIENCE_ENV, "") or "").strip()
    if not audience:
        # Same reasoning as a blank FN_SHARED_SECRET: an unconfigured audience would
        # accept a token minted for any other service the caller can reach.
        logs.emit("error", "oidc_audience_unset")
        return Response(500, {"error": "function not configured"})

    payload = claims(req)
    if not payload:
        return Response(401, dict(_DENIED))

    presented_aud = payload.get("aud")
    # aud is a string in every Google-issued ID token, but the JWT spec allows a list
    # and accepting only the string form would be a silent outage if that ever changes.
    audiences = presented_aud if isinstance(presented_aud, list) else [presented_aud]
    if not any(hmac.compare_digest(str(a or "").strip(), audience) for a in audiences):
        logs.emit("warning", "oidc_audience_mismatch", expected=audience)
        return Response(401, dict(_DENIED))

    allowed = [entry.strip().lower()
               for entry in (os.environ.get(_OIDC_ALLOWED_ENV, "") or "").split(",")
               if entry.strip()]
    if allowed:
        email = str(payload.get("email") or "").strip().lower()
        if email not in allowed:
            # The principal IS logged: it authenticated successfully at the platform,
            # so it is not a secret, and "which service account was refused" is the
            # first question of every broker-onboarding failure.
            logs.emit("warning", "oidc_principal_not_allowed", principal=email or "-")
            return Response(401, dict(_DENIED))
    return None


# Inner gates by name. A workload names one with a module-level AUTH_MODE; anything
# unrecognised is a 500, never a pass — a typo in that constant must not be the thing
# that opens a credential-changing endpoint.
_MODES = {
    "": verify,
    "shared_secret": verify,
    "gcp_oidc": verify_gcp_oidc,
}


def verify_for(mode: str, req: Request):
    """Dispatch to the inner gate ``mode`` names. ``dispatch.handle_request`` only."""
    handler = _MODES.get((mode or "").strip().lower())
    if handler is None:
        logs.emit("error", "unknown_auth_mode", auth_mode=str(mode))
        return Response(500, {"error": "function not configured"})
    return handler(req)
