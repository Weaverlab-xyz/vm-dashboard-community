"""Entitle Remote Adapter for Azure machine-identity access.

AWS machine identity works because Entitle attaches an IAM policy to the dashboard's
IAM **user**. Azure has no equivalent: a machine identity there is an *application*
(a service principal), and an application cannot be the account privileges are
requested for — so Entitle's native Azure integration cannot grant to one, and this
adapter is the only route.

    GET  /get_assets              the scopes this adapter may grant at
    GET  /get_actors              the service principals it is configured for
    GET  /get_all_permissions     the assignments it currently holds
    POST /give_access             {asset, actor_identifier, role_code}
    POST /revoke_access           same
    POST /check_config

Not ephemeral: service principals already exist, so there is no
create_actor/delete_actor. Entitle owns the expiry — plain Azure role assignments
carry no TTL, so a grant lasts until Entitle calls revoke_access.

**This adapter is a privilege-escalation primitive if it is unbounded**, so it is
bounded in three ways, all in ``azure_role_rules``: the scopes it may grant at are
allowlisted, the roles are allowlisted, and Owner / User Access Administrator /
RBAC Administrator are refused outright regardless of the allowlist — a time-boxed
grant of those is not time-boxed, because the grantee can make it permanent before
it expires.

ARM is plain REST, so this uses ``urllib`` and needs no Azure SDK — the fnruntime
contract stays stdlib-only and nothing but the rules module is vendored.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from fnruntime import logs, secretref
from fnruntime.contract import Context, Request, Response

NAME = "azure_role_grant"
DESCRIPTION = "Entitle Remote Adapter: time-boxed Azure RBAC for machine identities."

# Settings this workload cannot run without, in the form the dashboard's deploy
# validation reads: ``"A|B"`` means either satisfies the requirement. Declared here,
# beside the code that reads them, so the picker and the validator cannot drift from
# what azure_role_grant actually needs.
#
# Without this a hand-deploy from the Cloud Functions page produced a function that
# 500-ed on every request for the life of the install — the automated pairing path
# always passes them, so nothing else caught it.
REQUIRED_ENV = ("FN_AZURE_SUBSCRIPTION_ID",)

try:
    import azureroles as _rules  # noqa: E402
except ImportError:  # pragma: no cover - exercised in-repo, never in the zip
    from web_dashboard.services import azure_role_rules as _rules  # noqa: E402

_TRUTHY = ("1", "true", "yes", "on")
_TIMEOUT = 30
_ARM = "https://management.azure.com"
_API_VERSION = "2022-04-01"

# Cached across warm invocations. An AAD client-credentials token lasts ~1h; minting
# one per request would triple the latency of every grant for no benefit.
_TOKEN = {"value": "", "expires_at": 0.0}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or default).strip()


def _dry_run() -> bool:
    raw = os.environ.get("FN_AZURE_DRY_RUN")
    return True if raw is None else raw.strip().lower() in _TRUTHY


def _config() -> dict:
    subscription = _env("FN_AZURE_SUBSCRIPTION_ID")
    if not subscription:
        raise RuntimeError("FN_AZURE_SUBSCRIPTION_ID is not set")
    return {
        "tenant_id": _env("FN_AZURE_TENANT_ID"),
        "client_id": _env("FN_AZURE_CLIENT_ID"),
        # By reference on every cloud (fnruntime.secretref). This one holds
        # User Access Administrator, so it is the last credential that should
        # ever sit in a plaintext setting.
        "client_secret": secretref.resolve(
            "FN_AZURE_CLIENT_SECRET", "FN_AZURE_CLIENT_SECRET_ID"),
        "subscription_id": subscription,
        "scopes": _env("FN_AZURE_SCOPES"),
        "roles": _env("FN_AZURE_ROLES"),
        "principals": _env("FN_AZURE_PRINCIPALS"),
    }


def _token(config: dict) -> str:
    """An ARM access token via client credentials. Plain HTTPS, no SDK."""
    now = time.time()
    if _TOKEN["value"] and _TOKEN["expires_at"] > now + 60:
        return _TOKEN["value"]
    for key in ("tenant_id", "client_id", "client_secret"):
        if not config.get(key):
            if key == "client_secret":
                raise RuntimeError(
                    "no Azure client secret available: set FN_AZURE_CLIENT_SECRET "
                    "(GCP secret env var / Azure Key Vault reference) or "
                    "FN_AZURE_CLIENT_SECRET_ID (AWS Secrets Manager)")
            raise RuntimeError(f"FN_AZURE_{key.upper()} is not set")
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "scope": f"{_ARM}/.default",
    }).encode()
    url = f"https://login.microsoftonline.com/{config['tenant_id']}/oauth2/v2.0/token"
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Deliberately does NOT echo the response body: a failed token request can
        # include the client id and correlation detail, and this runs on the grant
        # path where the log is the one place a secret must never surface.
        raise RuntimeError(f"azure token request failed: HTTP {exc.code}") from None
    _TOKEN["value"] = str(payload.get("access_token") or "")
    _TOKEN["expires_at"] = now + float(payload.get("expires_in") or 3600)
    if not _TOKEN["value"]:
        raise RuntimeError("azure token response carried no access_token")
    return _TOKEN["value"]


def _arm(config: dict, method: str, path: str, body: dict = None,
         ok_missing: bool = False):
    url = f"{_ARM}{path}?api-version={_API_VERSION}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {_token(config)}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and ok_missing:
            # Already gone is success — a revoke that errors on an absent
            # assignment leaves the caller retrying forever.
            return None
        if exc.code == 409 and method == "PUT":
            # The assignment already exists. Because the name is derived
            # deterministically, that means OUR assignment, so the grant is
            # already in the state the caller asked for.
            return {"conflict": True}
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"arm {method} failed: HTTP {exc.code} {detail}") from None
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        return None


def _principals(config: dict) -> list:
    """The service principals this adapter is configured to grant to.

    An allowlist, like the scopes and roles: the set of identities a grant
    integration may touch should be a deliberate decision, not "whatever object id
    the caller sends".
    """
    entries = []
    for raw in _rules.parse_list(config["principals"]):
        # "objectid" or "objectid=Display Name"
        object_id, _, label = raw.partition("=")
        try:
            checked = _rules.check_principal(object_id)
        except _rules.AzureRoleRuleError:
            continue
        entries.append({"identifier": checked,
                        "name": label.strip() or checked,
                        "type": "azure_service_principal", "email": ""})
    return entries


def _resolve_principal(config: dict, identifier: str) -> str:
    """The object id for an actor, if it is one we are configured for."""
    wanted = str(identifier or "").strip().lower()
    for entry in _principals(config):
        if wanted in (entry["identifier"], entry["name"].strip().lower()):
            return entry["identifier"]
    raise _rules.AzureRoleRuleError(
        f"service principal {identifier!r} is not in this adapter's allowlist")


# ── Routes ───────────────────────────────────────────────────────────────────

def _get_assets(req, ctx, config):
    assets = [_rules.asset_for(scope, config["roles"])
              for scope in _rules.parse_list(config["scopes"])]
    return Response(200, {"next": "", "data": {"assets": assets}})


def _get_actors(req, ctx, config):
    return Response(200, {"next": "", "data": {"actors": _principals(config)}})


def _get_all_permissions(req, ctx, config):
    """The assignments this adapter holds.

    Only its OWN: every assignment it creates is named by a deterministic GUID, so
    membership is recomputed rather than read back. Listing every assignment at the
    scope would report ones a human made and invite Entitle to reconcile them away.
    """
    if _dry_run():
        return Response(200, {"next": "", "data": {"actors_permissions": [],
                                                   "assets_permissions": []}})
    actors_permissions = []
    roles = _rules.parse_list(config["roles"])
    for scope in _rules.parse_list(config["scopes"]):
        for principal in _principals(config):
            for role_code in roles:
                try:
                    role_id = _rules.resolve_role(role_code)
                except _rules.AzureRoleRuleError:
                    continue
                name = _rules.assignment_name(scope, principal["identifier"], role_id)
                found = _arm(config, "GET", _rules.assignment_path(scope, name),
                             ok_missing=True)
                if found:
                    actors_permissions.append({
                        "actor_id": principal["identifier"],
                        "role_code": role_code, "direct_member": True})
    assets_permissions = [
        {"asset_id": f"azure:scope:{scope}", "role_code": role_code}
        for scope in _rules.parse_list(config["scopes"]) for role_code in roles
    ]
    return Response(200, {"next": "", "data": {
        "actors_permissions": actors_permissions,
        "assets_permissions": assets_permissions}})


def _resolve_request(config: dict, payload: dict) -> tuple:
    scope = _rules.check_scope(_rules.scope_from_asset(payload), config["scopes"])
    role_id = _rules.check_role(payload.get("role_code"), config["roles"])
    principal = _resolve_principal(config, payload.get("actor_identifier"))
    name = _rules.assignment_name(scope, principal, role_id)
    return scope, role_id, principal, name


def _give_access(req, ctx, config):
    payload = req.json()
    try:
        scope, role_id, principal, name = _resolve_request(config, payload)
    except _rules.AzureRoleRuleError as exc:
        return Response(403, {"error": str(exc)})

    logs.emit("info", "give_access", request_id=ctx.request_id, scope=scope,
              principal=principal, role=role_id, dry_run=_dry_run())
    if _dry_run():
        return Response(200, {"data": {
            "dry_run": True, "actor_identifier": principal,
            "role_code": payload.get("role_code"), "scope": scope,
            "would": f"PUT {_rules.assignment_path(scope, name)}"}})

    result = _arm(config, "PUT", _rules.assignment_path(scope, name), {
        "properties": {
            "roleDefinitionId": _rules.role_definition_id(config["subscription_id"], role_id),
            "principalId": principal,
            # Stated explicitly: without it ARM looks the principal up in Graph, and
            # a freshly created service principal can 400 with "principal not found"
            # purely from replication lag.
            "principalType": "ServicePrincipal",
        }})
    return Response(200, {"data": {
        "actor_identifier": principal, "role_code": payload.get("role_code"),
        "scope": scope, "assignment": name,
        "already_present": bool((result or {}).get("conflict"))}})


def _revoke_access(req, ctx, config):
    payload = req.json()
    try:
        scope, role_id, principal, name = _resolve_request(config, payload)
    except _rules.AzureRoleRuleError as exc:
        return Response(403, {"error": str(exc)})

    logs.emit("info", "revoke_access", request_id=ctx.request_id, scope=scope,
              principal=principal, role=role_id, dry_run=_dry_run())
    if _dry_run():
        return Response(200, {"data": {
            "dry_run": True, "actor_identifier": principal,
            "role_code": payload.get("role_code"), "scope": scope,
            "would": f"DELETE {_rules.assignment_path(scope, name)}"}})

    _arm(config, "DELETE", _rules.assignment_path(scope, name), ok_missing=True)
    return Response(200, {"data": {
        "actor_identifier": principal, "role_code": payload.get("role_code"),
        "scope": scope, "assignment": name, "removed": True}})


def _check_config(req, ctx, config):
    problems = []
    scopes, roles = [], []
    for scope in _rules.parse_list(config["scopes"]):
        try:
            _rules.check_scope(scope, config["scopes"])
            scopes.append(scope)
        except _rules.AzureRoleRuleError as exc:
            problems.append(str(exc))
    for role in _rules.parse_list(config["roles"]):
        try:
            _rules.check_role(role, config["roles"])
            roles.append(role)
        except _rules.AzureRoleRuleError as exc:
            problems.append(str(exc))
    principals = _principals(config)
    if not scopes:
        problems.append("no grantable scopes configured (FN_AZURE_SCOPES)")
    if not roles:
        problems.append("no grantable roles configured (FN_AZURE_ROLES)")
    if not principals:
        problems.append("no service principals configured (FN_AZURE_PRINCIPALS)")
    return Response(200, {"data": {
        "valid": not problems, "scopes": scopes, "roles": roles,
        "principals": [p["identifier"] for p in principals],
        "dry_run": _dry_run(), "problems": problems}})


_ROUTES = {
    ("GET", "/get_assets"): _get_assets,
    ("GET", "/get_actors"): _get_actors,
    ("GET", "/get_all_permissions"): _get_all_permissions,
    ("POST", "/give_access"): _give_access,
    ("POST", "/revoke_access"): _revoke_access,
    ("POST", "/check_config"): _check_config,
}


def _unconfigured(exc: Exception, ctx: Context, *, reporting: bool) -> Response:
    """What to answer when ``FN_AZURE_*`` cannot be resolved.

    :func:`_config` used to run BEFORE routing, so its raise became dispatch's
    opaque ``500 {"error": "internal error"}`` on every route — ``/check_config``
    included, which is the one route that exists to name what is missing.

    Safe to detail: dispatch verifies the shared secret before calling ``handle``.
    """
    problem = str(exc) or type(exc).__name__
    if reporting:
        # Same body shape as the success path, so a consumer needs no special case.
        return Response(200, {"data": {
            "valid": False, "scopes": [], "roles": [], "principals": [],
            "dry_run": _dry_run(), "problems": [problem],
        }})
    return Response(500, {"error": "function not configured",
                          "problem": problem, "request_id": ctx.request_id})


def handle(req: Request, ctx: Context) -> Response:
    path = (req.path or "/").rstrip("/") or "/"
    handler = _ROUTES.get((req.method, path))
    if handler is None and path.startswith("/get_asset_permissions/") and req.method == "GET":
        handler = _get_all_permissions
    if handler is None:
        return Response(404, {
            "error": f"no route for {req.method} {path}",
            "routes": sorted(f"{method} {route}" for method, route in _ROUTES),
        })

    # AFTER routing — see _unconfigured.
    try:
        config = _config()
    except Exception as exc:
        return _unconfigured(exc, ctx, reporting=handler is _check_config)
    return handler(req, ctx, config)
