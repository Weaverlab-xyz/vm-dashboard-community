"""Azure Functions entry point (Python v2 programming model).

Packaged at the zip root as **function_app.py** — the v2 model requires that exact
filename at the root, and there is no Terraform field to point elsewhere.

Routes (Azure prefixes them all with ``/api``):

  POST|GET /api/invoke   the workload. FUNCTION auth level, so the host key is the
                         cloud-native front door; the shared-secret bearer is
                         checked independently inside dispatch.
  GET      /api/health   ANONYMOUS, and deliberately says nothing but "indexing
                         worked". The classic v2 failure is an app that boots,
                         serves the default landing page and registers ZERO
                         functions — this route turns diagnosing that into one
                         curl that needs no key.
"""
import azure.functions as func

import workload
from fnruntime import adapters, dispatch

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# A wildcard, not a fixed "invoke": a workload may serve several routes, and for an
# Entitle Remote Adapter THE VERB IS THE PATH (/give_access vs /revoke_access), so a
# single fixed route would make every operation indistinguishable. The path reaches
# the workload as Request.path with the /api prefix stripped, so it reads the same
# on all three clouds.
@app.route(route="{*path}", methods=["GET", "POST"])
def invoke(req: func.HttpRequest) -> func.HttpResponse:
    normalized = adapters.from_azure(req)
    response = dispatch.handle_request(normalized, workload)
    return adapters.to_azure(response)


# Declared AFTER the wildcard but matched BEFORE it: a literal route outranks a
# catch-all in the ASP.NET-style routing Functions uses. That precedence is
# load-bearing — if the wildcard ever won, /api/health would inherit FUNCTION auth
# and the one diagnostic that needs no key would start returning 401. A 401 here
# rather than a 200 is the signal that it did.
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(body='{"ok":true}', status_code=200,
                             mimetype="application/json")
