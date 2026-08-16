"""Template for a new Cloud Functions workload.

To use it: copy this file into ``web_dashboard/functions/fnworkloads/`` and rename it
to your workload name (the filename IS the workload name — the filesystem is the
catalog, so nothing else needs editing). It will appear in the deploy form on the
next page load.

Two rules, both load-bearing:

1. **Standard library only.** No pip dependencies. On AWS you may additionally use
   ``boto3``, which the Lambda runtime ships — but import it INSIDE ``handle`` so the
   module still imports on the other clouds and in the dashboard's catalog scan. If a
   workload genuinely can't run everywhere, add it to
   ``cloud_function_service._CLOUD_RESTRICTED``.

2. **Never log a secret.** Use ``fnruntime.logs.emit``, which redacts automatically —
   anything whose key contains ``password``/``secret``/``token``/``credential``, and
   ``authorization`` unconditionally.

You do NOT write any cloud-specific code: no ``event``/``context``, no
``azure.functions``, no Flask, and no authentication check. The runtime has already
verified the shared secret by the time ``handle`` is called, and a raised exception
becomes a clean 500 with a request id (never a stack trace on the wire).

Test it locally with no cloud account:

    python -c "
    import sys; sys.path.insert(0, '.')
    from web_dashboard import functions
    from fnruntime.contract import Request, Context
    from fnworkloads import my_workload
    print(my_workload.handle(Request(body=b'{}'), Context.from_env()).body)"
"""
from fnruntime import logs
from fnruntime.contract import Context, Request, Response

# Shown in the deploy form.
NAME = "custom_handler"
DESCRIPTION = "Describe what this workload does, in one line."


def handle(req: Request, ctx: Context) -> Response:
    """Handle one request.

    ``req.headers`` keys are always lower-cased, ``req.body`` is already
    base64-decoded, and ``req.json()`` returns ``{}`` rather than raising on a
    malformed body. ``req.source`` tells you how you were invoked
    (``aws_function_url``, ``aws_direct`` for a scheduled run, ``azure``, ``gcp``…),
    which is how a workload supports both HTTP and schedule triggers.
    """
    payload = req.json()

    # Example: an Entitle-style grant/revoke, which is the Phase 2 shape.
    action = str(payload.get("action") or "").lower()
    if action not in ("grant", "revoke"):
        return Response(400, {"error": "action must be 'grant' or 'revoke'"})

    logs.emit("info", "handling", request_id=ctx.request_id, action=action,
              # Safe: redact() masks this automatically if it is sensitive.
              user=payload.get("user_email"))

    # ... do the work here ...

    return Response(200, {
        "ok": True,
        "action": action,
        "request_id": ctx.request_id,
        "duration_ms": ctx.elapsed_ms(),
    })
