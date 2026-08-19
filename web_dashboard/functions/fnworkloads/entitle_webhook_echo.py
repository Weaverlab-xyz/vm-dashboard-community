"""A no-op Entitle Remote Adapter, for proving the path before wiring a real target.

Implements every route of the Remote Adapter contract
(docs.beyondtrust.com/entitle/docs/open-api-definition) with valid, empty-but-
well-formed responses, and reports what it received. Point a real Entitle
integration at this and you can confirm — before any target system is involved —
that:

  * the network path and front door work
  * the shared secret is configured correctly on both sides
  * each ``*_path`` in the integration config maps to a route that exists
  * the payloads arrive in the shape the contract says

Every real adapter (``db_grant``, and the Portainer and machine-identity ones to
come) implements this same set of routes, so this is the reference and the
smoke test for all of them.

Failure injection, for testing Entitle's retry and alerting behaviour:

    ?fail=401 | 403 | 404 | 500 | slow | timeout

Stdlib only.
"""
import time

from fnruntime.contract import Context, Request, Response

NAME = "entitle_webhook_echo"
DESCRIPTION = "No-op Entitle Remote Adapter — proves the path without granting anything."

# Serves the Entitle Remote Adapter contract (the eight routes in _ROUTES), so the
# dashboard may register it as a REST integration. Declared on the module rather than
# listed in the page's JavaScript, which is where this used to live and could drift.
ENTITLE_ADAPTER = True

# The documented contract. Kept as data so `handle` can both route on it and report
# it in a 404, which is what turns a path-config mistake into a self-explaining
# error rather than a silent one.
_ROUTES = (
    ("GET", "/get_assets"),
    ("GET", "/get_actors"),
    ("GET", "/get_all_permissions"),
    ("GET", "/get_asset_permissions/{asset_identifier}"),
    ("POST", "/create_actor"),
    ("POST", "/delete_actor"),
    ("POST", "/give_access"),
    ("POST", "/revoke_access"),
    ("POST", "/check_config"),
)

# Fields the contract defines per write route. Reported as present/absent rather
# than enforced — the point is to show an operator what actually arrived.
_EXPECTED = {
    "/give_access": ("asset", "actor_identifier", "role_code"),
    "/revoke_access": ("asset", "actor_identifier", "role_code"),
    "/create_actor": ("actor",),
    "/delete_actor": ("actor_identifier",),
    "/check_config": ("config",),
}

_SAMPLE_ASSET = {
    "identifier": "demo:asset:1",
    "name": "Demo asset",
    "type": "demo",
    "role_options": [
        {"code": "read", "display_name": "Read only", "available": True,
         "permissions": ["read"]},
    ],
}


def _injected_failure(req: Request):
    mode = str(req.query.get("fail") or "").strip().lower()
    if not mode:
        return None
    if mode == "slow":
        time.sleep(5)
        return None
    if mode == "timeout":
        time.sleep(25)
        return None
    codes = {"401": 401, "unauthorized": 401, "403": 403, "forbidden": 403,
             "404": 404, "notfound": 404, "500": 500, "error": 500}
    if mode in codes:
        return Response(codes[mode], {"error": mode, "injected": True})
    return Response(400, {"error": f"unknown fail mode: {mode}", "injected": True})


def _observed(req: Request, path: str) -> dict:
    """What arrived, and whether it matches the contract for this route."""
    payload = req.json()
    expected = _EXPECTED.get(path, ())
    return {
        "method": req.method,
        "path": path,
        "source": req.source,
        "received_keys": sorted(payload.keys()),
        "expected_keys": list(expected),
        "missing_keys": [key for key in expected if key not in payload],
        "unexpected_keys": [key for key in sorted(payload) if expected and key not in expected],
    }


def handle(req: Request, ctx: Context) -> Response:
    injected = _injected_failure(req)
    if injected is not None:
        return injected

    path = (req.path or "/").rstrip("/") or "/"
    known = {route for _method, route in _ROUTES}
    is_asset_permissions = path.startswith("/get_asset_permissions/")

    if path not in known and not is_asset_permissions:
        # Entitle's *_path fields are configurable per operation, so this is the
        # single most likely setup mistake — say exactly what is served.
        return Response(404, {
            "error": f"no route for {req.method} {path}",
            "routes": [f"{method} {route}" for method, route in _ROUTES],
            "request_id": ctx.request_id,
        })

    observed = _observed(req, path)
    observed["request_id"] = ctx.request_id

    # Valid, empty-but-well-formed responses in the contract's own envelopes, so
    # Entitle accepts them and the integration can be saved and exercised.
    if path == "/get_assets":
        return Response(200, {"next": "", "data": {"assets": [_SAMPLE_ASSET]},
                              "observed": observed})
    if path == "/get_actors":
        return Response(200, {"next": "", "data": {"actors": []}, "observed": observed})
    if path == "/get_all_permissions" or is_asset_permissions:
        return Response(200, {"next": "", "data": {"actors_permissions": [],
                                                   "assets_permissions": []},
                              "observed": observed})
    if path == "/check_config":
        return Response(200, {"data": {"valid": True}, "observed": observed})

    # create_actor / delete_actor / give_access / revoke_access — acknowledged,
    # nothing granted.
    return Response(200, {"data": {"ok": True, "granted": False},
                          "observed": observed})
