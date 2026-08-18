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
import hmac
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
