"""Per-cloud event translation — the only code that knows what cloud it is on.

``from_*`` normalizes an inbound event into a :class:`Request`; ``to_*`` renders a
:class:`Response` into what the platform expects back.

Cloud SDKs are imported lazily inside the functions that need them (the repo's
convention, and here it also means this module imports cleanly on all three
runtimes and is unit-testable against plain fixture objects with nothing
installed). ``from_gcp`` never imports Flask at all — it duck-types the request.

Stdlib only.
"""
import base64
import json

from fnruntime.contract import Request, Response


def _lower_headers(headers) -> dict:
    """Lower-case every header key. AWS's v2 shape already does this; API Gateway
    v1, ALB and Azure do not, and a workload must not have to care."""
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {}
    return {str(k).lower(): "" if v is None else str(v) for k, v in items}


def _decode_body(raw, is_base64: bool) -> bytes:
    """Body as bytes, base64-decoded when the platform flagged it."""
    if raw is None:
        return b""
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        data = str(raw).encode("utf-8")
    if is_base64:
        try:
            return base64.b64decode(data)
        except Exception:
            # A body flagged base64 that isn't decodable is a platform bug; pass
            # the raw bytes through rather than dropping the request.
            return data
    return data


def _strip_stage(path: str, stage: str) -> str:
    """Drop an API Gateway stage prefix so paths match across clouds.

    ``$default`` is not a real prefix — it is the literal name of the implicit
    stage, and the path never contains it.
    """
    if not path:
        return "/"
    if stage and stage != "$default":
        prefix = "/" + stage
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
    return path


# ── AWS ───────────────────────────────────────────────────────────────────────
#
# Four shapes reach a Lambda, and getting any of them wrong is a crash rather than
# a degradation. The direct-invoke branch is what makes SCHEDULED workloads
# possible (EventBridge / `aws lambda invoke` send a bare JSON document with no
# requestContext at all) — without it every scheduled invocation dies on a KeyError.

def from_aws(event, lambda_context=None) -> Request:
    """Normalize any of the four AWS event shapes."""
    if not isinstance(event, dict):
        event = {}
    request_context = event.get("requestContext")
    if event.get("version") == "2.0" and isinstance(request_context, dict):
        return _from_aws_v2(event, request_context)
    if "httpMethod" in event:
        return _from_aws_v1(event)
    return _from_aws_direct(event)


def _from_aws_v2(event: dict, request_context: dict) -> Request:
    """Lambda Function URL and API Gateway HTTP API v2 — one shape, one branch.

    They differ only in the domain (and that a real API GW stage can prefix the
    path), so the source is distinguished by the domain for logging/auth purposes.
    """
    http = request_context.get("http") or {}
    domain = str(request_context.get("domainName", ""))
    source = "aws_function_url" if ".lambda-url." in domain else "aws_apigw_v2"
    path = _strip_stage(event.get("rawPath") or "/",
                        str(request_context.get("stage", "")))
    query = event.get("queryStringParameters") or {}
    return Request(
        method=str(http.get("method", "GET")).upper(),
        path=path,
        headers=_lower_headers(event.get("headers")),
        query={str(k): str(v) for k, v in query.items()},
        body=_decode_body(event.get("body"), bool(event.get("isBase64Encoded"))),
        source=source,
    )


def _from_aws_v1(event: dict) -> Request:
    """API Gateway REST (v1) and ALB. Headers are mixed-case here, and
    ``queryStringParameters`` is ``None`` (not ``{}``) when there are none."""
    query = event.get("queryStringParameters") or {}
    return Request(
        method=str(event.get("httpMethod", "GET")).upper(),
        path=_strip_stage(event.get("path") or "/",
                          str((event.get("requestContext") or {}).get("stage", ""))),
        headers=_lower_headers(event.get("headers")),
        query={str(k): str(v) for k, v in query.items()},
        body=_decode_body(event.get("body"), bool(event.get("isBase64Encoded"))),
        source="aws_apigw_v1",
    )


def _from_aws_direct(event: dict) -> Request:
    """A non-HTTP invoke (EventBridge schedule, `aws lambda invoke`, another
    Lambda). The whole event becomes the body so a workload reads it exactly as it
    would an HTTP POST — one code path instead of two."""
    try:
        body = json.dumps(event).encode("utf-8")
    except (TypeError, ValueError):
        body = b"{}"
    return Request(method="POST", path="/", headers={}, query={},
                   body=body, source="aws_direct")


def to_aws(resp: Response) -> dict:
    """The explicit proxy envelope — ALWAYS, for every shape.

    Returning a bare dict from a Function URL makes Lambda treat the whole dict as
    a 200 body, which would silently swallow a 401 and turn an auth failure into a
    successful-looking response.
    """
    text, headers, is_base64 = resp.rendered()
    return {
        "statusCode": int(resp.status),
        "headers": headers,
        "body": text,
        "isBase64Encoded": is_base64,
    }


# ── Azure ─────────────────────────────────────────────────────────────────────

def from_azure(req) -> Request:
    """Normalize an ``azure.functions.HttpRequest``.

    Azure routes functions under ``/api/<function-name>``; that prefix is stripped
    so a workload sees the same path it would on the other two clouds.
    """
    try:
        raw_body = req.get_body() or b""
    except Exception:
        raw_body = b""
    params = {}
    try:
        params = {str(k): str(v) for k, v in dict(req.params or {}).items()}
    except Exception:
        params = {}
    path = "/"
    try:
        from urllib.parse import urlparse
        path = urlparse(str(getattr(req, "url", "") or "")).path or "/"
    except Exception:
        path = "/"
    if path.startswith("/api/"):
        path = path[4:] or "/"
    elif path == "/api":
        path = "/"
    return Request(
        method=str(getattr(req, "method", "GET") or "GET").upper(),
        path=path,
        headers=_lower_headers(getattr(req, "headers", None)),
        query=params,
        body=bytes(raw_body),
        source="azure",
    )


def to_azure(resp: Response):
    """Render to an ``azure.functions.HttpResponse`` (imported lazily)."""
    import azure.functions as func  # noqa: PLC0415 — see module docstring

    text, headers, _is_base64 = resp.rendered()
    return func.HttpResponse(
        body=text,
        status_code=int(resp.status),
        headers=headers,
        mimetype=headers.get("content-type", "application/json"),
    )


# ── GCP ───────────────────────────────────────────────────────────────────────

def from_gcp(req) -> Request:
    """Normalize a Flask request — **duck-typed, never imported**.

    Cloud Run functions hand the handler a ``flask.Request``, but only five
    attributes are needed. Not importing Flask keeps this module loadable (and
    testable) on AWS, on Azure, and on a laptop with nothing installed.
    """
    try:
        raw_body = req.get_data() if hasattr(req, "get_data") else b""
    except Exception:
        raw_body = b""
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    try:
        query = {str(k): str(v) for k, v in dict(getattr(req, "args", {}) or {}).items()}
    except Exception:
        query = {}
    return Request(
        method=str(getattr(req, "method", "GET") or "GET").upper(),
        path=str(getattr(req, "path", "/") or "/"),
        headers=_lower_headers(getattr(req, "headers", None)),
        query=query,
        body=bytes(raw_body or b""),
        source="gcp",
    )


def to_gcp(resp: Response) -> tuple:
    """The ``(body, status, headers)`` triple Flask accepts."""
    text, headers, _is_base64 = resp.rendered()
    return text, int(resp.status), headers


# ── Self-hosted (OpenFaaS / Nuclio / a plain Deployment) ──────────────────────
#
# The fourth target is not a cloud: it is a container the dashboard put on a
# cluster it does not own, reached over ordinary HTTP. So unlike the three above
# there is no vendor event shape to normalize — the entry shim owns a real HTTP
# server and hands the pieces straight in. That is why these two take primitives
# rather than a request object: they are the one pair that can be driven from a
# test, or from a laptop, with nothing installed and no fixture to imitate.
#
# ``source`` is ``openfaas`` for every self-hosted runtime, deliberately. It
# names the CONTRACT (an HTTP request that arrived through a gateway we do not
# authenticate at), not the product, so swapping OpenFaaS for Nuclio or a bare
# Deployment does not change a workload's behaviour or an auth decision. Note
# what it does NOT unlock: ``auth.presented_secret`` accepts a secret in the body
# for ``aws_direct`` only, because that shape has no headers at all. This one
# always has headers, so the credential always belongs in one.

def from_http(method, path, headers=None, body=None, query=None) -> Request:
    """Normalize a plain HTTP request.

    ``path`` may still carry its query string — an of-watchdog upstream receives
    the raw request line — so it is split here rather than in the shim. A caller
    that has already parsed the query passes ``query`` and keeps the path clean;
    both are accepted because the two runtimes differ on which they hand over.
    """
    raw_path = str(path or "/")
    parsed = dict(query or {})
    if "?" in raw_path:
        raw_path, _, qs = raw_path.partition("?")
        if not parsed and qs:
            from urllib.parse import parse_qsl
            parsed = {str(k): str(v) for k, v in parse_qsl(qs, keep_blank_values=True)}
    if isinstance(body, str):
        body = body.encode("utf-8")
    return Request(
        method=str(method or "GET").upper(),
        path=raw_path or "/",
        headers=_lower_headers(headers),
        query={str(k): str(v) for k, v in parsed.items()},
        body=bytes(body or b""),
        source="openfaas",
    )


def to_http(resp: Response) -> tuple:
    """The ``(status, headers, body_bytes)`` triple an HTTP server writes.

    Bytes, not text, because the shim writes to a socket and must set
    ``content-length`` from the encoded length — deriving it from a str is how a
    non-ASCII error message becomes a truncated body the caller reads as a
    connection reset. ``rendered()`` already base64-encodes a bytes payload, so
    what comes back here is always text safe to encode as UTF-8.
    """
    text, headers, _is_base64 = resp.rendered()
    payload = (text or "").encode("utf-8")
    headers["content-length"] = str(len(payload))
    return int(resp.status), headers, payload
