"""auth → workload → response, with logging. Cloud-agnostic.

The entry shims own only the translation (``adapters.from_x`` / ``to_x``); every
decision lives here so the three clouds cannot behave differently.

The workload module is passed IN rather than imported here: in the zip it is a
top-level ``workload`` module, but in-repo it is ``fnworkloads.<name>``, and tests
pass fakes. Threading it through as a parameter keeps this module pure.

Stdlib only.
"""
import os
import time

from fnruntime import auth, logs
from fnruntime.contract import Context, Request, Response


def _debug_enabled() -> bool:
    return (os.environ.get("FN_DEBUG", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _log_body_enabled() -> bool:
    return (os.environ.get("FN_LOG_BODY", "") or "").strip().lower() in ("1", "true", "yes", "on")


def handle_request(req: Request, workload_module, ctx: Context = None) -> Response:
    """Run one request end to end. Never raises — a workload blowing up becomes a
    500 with a request id, not a stack trace on the wire."""
    started = time.time()
    if ctx is None:
        ctx = Context.from_env(req, workload=getattr(workload_module, "NAME", ""))

    denial = auth.verify(req)
    if denial is not None:
        # Log the outcome, never the credential: `presented_secret` is not called
        # here and `headers` goes through redact(), which masks `authorization`.
        logs.emit("warning", "request_denied", request_id=ctx.request_id,
                  workload=ctx.workload, cloud=ctx.cloud, source=req.source,
                  method=req.method, path=req.path, status=denial.status,
                  duration_ms=int((time.time() - started) * 1000))
        return denial

    fields = {
        "request_id": ctx.request_id, "workload": ctx.workload, "cloud": ctx.cloud,
        "region": ctx.region, "function_name": ctx.function_name,
        "source": req.source, "method": req.method, "path": req.path,
    }
    if _log_body_enabled():
        # Redacted, and opt-in: a grant payload carries usernames and can carry a
        # generated password, so it is off by default even though it is redacted.
        fields["body"] = logs.redact(req.json())

    try:
        resp = workload_module.handle(req, ctx)
    except Exception as exc:
        # The caller gets a generic body + the request id to quote; the detail goes
        # to the function's own log stream only. A traceback is genuinely useful
        # when debugging a private-network workload, so it is available — but
        # behind FN_DEBUG, never on by default and never in the response.
        detail = {"error_type": type(exc).__name__}
        if _debug_enabled():
            import traceback
            detail["traceback"] = traceback.format_exc()[-4000:]
        logs.emit("error", "workload_error", status=500,
                  duration_ms=int((time.time() - started) * 1000),
                  **fields, **detail)
        return Response(500, {"error": "internal error", "request_id": ctx.request_id})

    if not isinstance(resp, Response):
        # A workload that returns a bare dict is a common slip; treat it as a 200
        # body rather than serving something the adapters can't render.
        resp = Response(200, resp)

    logs.emit("info", "request", status=resp.status,
              duration_ms=int((time.time() - started) * 1000), **fields)
    return resp
