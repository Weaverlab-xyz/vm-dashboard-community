"""Entitle Remote Adapter: just-in-time accounts on a FUXA web HMI.

The first OT-native target in the Entitle catalogue. A plant gates its HMI — the
console an operator or a visiting vendor uses to see and change the process — and
until now the OT demo handed out a standing admin account. This mints one on
approval and deletes it on expiry.

It runs on the plant's own function runtime (the OT broker's KubeSolo), because the
HMI sits behind the Purdue boundary: the cell admits the PRA Gateway and the broker
and nothing else. So Entitle's agent, a pod on that same cluster, is what calls this
adapter, and Entitle itself never needs a route into the plant.

EPHEMERAL ACCOUNTS mode: ``create_actor`` IS the grant. By the time Entitle validates
the response the account exists, so a response that fails its schema leaves a live HMI
login Entitle does not know it provisioned and will never call ``delete_actor`` for.
Every payload here is therefore built through ``fnruntime.entitle``, which exists
because the published examples for two of these routes are wrong.

Everything about FUXA below was read from the v1.3.4 source. Three things its own
documentation and OpenAPI get wrong, each of which costs a debugging session:

  * the token header is ``x-access-token``. There is no ``Authorization: Bearer``
    parsing anywhere in the server;
  * ``POST /api/users`` takes ``params`` as a SINGLE OBJECT, not the array the
    OpenAPI declares;
  * the mutating user routes answer **200 with an empty body**, not 204.

And two behaviours worth stating to anyone demoing this:

  * with ``secureEnabled: false`` FUXA applies NO authorization to these endpoints —
    an anonymous caller is handed administrator. This adapter works either way, but
    the JIT story only means something with authentication on;
  * deleting a user revokes REST access at once, but an already-connected browser
    session survives until its socket drops: the handshake is verified once and never
    re-checked. The honest claim is "the account is gone and REST access dies
    immediately; an open tab lapses on reconnect", not "instantly".

DRY RUN IS ON BY DEFAULT (``FN_FUXA_DRY_RUN``), like ``db_grant``. The whole Entitle
path — mode, payload shapes, the agent brokering the calls — is then provable before
anything touches a running HMI.

Stdlib only.
"""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

from fnruntime import entitle, logs, secretref
from fnruntime.contract import Context, Request, Response

# Vendored beside this module at package time as `fuxarules`; in-repo that name does
# not exist and it is the service module. The bitmask rules are shared rather than
# retyped precisely because an off-by-one bit here hands out administrator.
try:
    import fuxarules  # noqa: E402
except ImportError:  # pragma: no cover - exercised in-repo, never in the zip
    from web_dashboard.services import fuxa_access_rules as fuxarules  # noqa: E402

NAME = "fuxa_hmi_access"
DESCRIPTION = ("Entitle Remote Adapter: just-in-time accounts on a FUXA web HMI, "
               "run inside the plant.")

# The shared-secret gate is the default and stays the default: on a cluster where
# every pod can reach the OpenFaaS gateway, and where OpenFaaS's own basic auth
# covers /system/* rather than /function/*, this is the only gate in front of an
# endpoint that mints credentials.
AUTH_MODE = "shared_secret"
ENTITLE_ADAPTER = True

REQUIRED_ENV = ("FN_FUXA_URL", "FN_FUXA_ASSET_ID")

_TRUTHY = ("1", "true", "yes", "on")
_TIMEOUT = 20


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or default).strip()


def _dry_run() -> bool:
    """Default ON. An adapter that could mint an HMI login the moment it is deployed
    is one deployment mistake away from doing so; the operator turns this off when
    the rest of the path is proven."""
    raw = os.environ.get("FN_FUXA_DRY_RUN")
    return True if raw is None else raw.strip().lower() in _TRUTHY


def _config() -> dict:
    base = _env("FN_FUXA_URL").rstrip("/")
    if not base:
        raise RuntimeError("FN_FUXA_URL is not set")
    asset_id = _env("FN_FUXA_ASSET_ID")
    if not asset_id:
        raise RuntimeError("FN_FUXA_ASSET_ID is not set")
    # By reference on every platform: a file on the plant runtime (OpenFaaS mounts a
    # secret at /var/openfaas/secrets/<name>), the platform's own env var on GCP and
    # Azure, Secrets Manager on AWS. secretref resolves whichever arrived.
    api_key = secretref.resolve("FN_FUXA_API_KEY")
    password = secretref.resolve("FN_FUXA_PASSWORD")
    username = _env("FN_FUXA_USER", "admin")
    # NO credential is NOT fatal, and that is deliberate rather than lax.
    #
    # A FUXA with `secureEnabled: false` applies no authorization to these endpoints
    # at all — an anonymous caller is handed administrator — and that is the state the
    # OT cell's HMI ships in. Refusing here would make the adapter unable to run
    # against the very target it exists for, and would do it by raising a
    # configuration error that says nothing about the HMI's actual posture.
    #
    # So the condition is REPORTED instead: `check_config` names it in as many words,
    # and the wiring play probes that route on every deploy. If authentication IS on,
    # FUXA answers 401 and its own message is a better diagnosis than a guess made
    # here would be.
    return {
        "base": base,
        "asset_id": asset_id,
        "asset_name": _env("FN_FUXA_ASSET_NAME") or f"FUXA HMI - {asset_id}",
        "cell": _env("FN_FUXA_CELL"),
        "hmi_url": _env("FN_FUXA_HMI_URL") or base,
        # The minted account is USELESS without this: the cell admits the PRA Gateway
        # and the broker and nothing else, so a requester's browser cannot route to
        # the HMI at all. Naming the jump item is what makes the credential usable.
        "jump_item": _env("FN_FUXA_JUMP_ITEM"),
        "username": username,
        "password": password,
        "api_key": api_key,
        # Named-role mode. FUXA's default is the bitmask, so that is this adapter's
        # default too; with roles configured, get_assets marks an option unavailable
        # when the plant has no role of that name.
        "role_mode": (_env("FN_FUXA_ROLE_MODE", "bitmask")).lower(),
        "verify_ssl": _env("FN_FUXA_VERIFY_SSL", "1").lower() in _TRUTHY,
        "dry_run": _dry_run(),
    }


def _ssl_context(config: dict):
    if config["verify_ssl"]:
        return None
    # Opt-in only. A plant HMI on a self-signed cert is ordinary; this is never the
    # default and the operator has to say so.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _api(config: dict, method: str, path: str, body=None, token: str = ""):
    """One FUXA API call. stdlib only.

    ``token`` goes in ``x-access-token`` — NOT ``Authorization``, which the server
    does not read. An API key, when configured, goes in ``x-api-key`` and needs no
    sign-in at all.
    """
    url = config["base"] + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if config["api_key"]:
        request.add_header("x-api-key", config["api_key"])
    if token:
        request.add_header("x-access-token", token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT,
                                    context=_ssl_context(config)) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "DELETE":
            # Already gone is success. A revoke that errors on an account somebody
            # removed by hand leaves Entitle retrying a grant it can never close.
            return None
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            f"fuxa {method} {path} failed: HTTP {exc.code} {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"fuxa {method} {path} could not be reached: {exc.reason}. From the "
            f"plant runtime this is the DMZ-to-plant firewall rule, and it has to be "
            f"asked from a POD - the node's own address is a different answer."
        ) from None
    # The mutating user routes answer 200 with an EMPTY body, not 204. Treating an
    # empty body as a failure would make every successful grant look broken.
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        return None


def _signin(config: dict) -> str:
    """A session token, or "" when none is needed.

    An API key needs no sign-in. Neither does a FUXA with ``secureEnabled: false``,
    which applies no authorization at all — so a failed sign-in is NOT fatal here:
    the call that follows either works without a token or fails with its own, better
    message naming the route that was refused.
    """
    if config["api_key"] or not config["password"]:
        return ""
    body = {"username": config["username"], "password": config["password"]}
    try:
        payload = _api(config, "POST", "/api/signin", body)
    except RuntimeError as exc:
        logs.emit("warning", "fuxa_signin_failed", error_detail=str(exc)[:200])
        return ""
    token = ""
    if isinstance(payload, dict):
        token = str((payload.get("data") or {}).get("token") or "")
    if not token:
        logs.emit("warning", "fuxa_signin_no_token")
    return token


def _users(config: dict, token: str) -> list:
    payload = _api(config, "GET", "/api/users", token=token)
    return payload if isinstance(payload, list) else []


def _roles(config: dict, token: str) -> list:
    """FUXA's own role catalogue, or ``[]``.

    Only consulted in named-role mode, and never fatal: an instance running the
    default bitmask model has no catalogue to offer and that is not an error.
    """
    if config["role_mode"] != "catalogue":
        return []
    try:
        payload = _api(config, "GET", "/api/roles", token=token)
    except RuntimeError as exc:
        logs.emit("warning", "fuxa_roles_unreadable", error_detail=str(exc)[:200])
        return []
    return payload if isinstance(payload, list) else []


def _identity(payload: dict) -> str:
    """The requester, from wherever Entitle put them.

    Checked in order because the field differs by route and by tenant version, and
    the value is what ``actor_data`` records as the account's owner — the only link
    from an account back to the person who asked for it.
    """
    provisioning = payload.get("provisioning_data") or {}
    for source in (provisioning, payload.get("actor") or {}, payload):
        if not isinstance(source, dict):
            continue
        for field in ("email", "user_email", "actor_email", "identifier", "user"):
            value = str(source.get(field) or "").strip()
            if value:
                return value
    return ""


def _role_code(payload: dict) -> str:
    for field in ("role_code", "role", "permission"):
        value = str(payload.get(field) or "").strip()
        if value:
            return value
    return fuxarules.DEFAULT_ROLE_CODE


def _asset(config: dict, catalogue=None) -> dict:
    """One asset per function, because one function fronts one cell's HMI and the
    admin credential it holds is instance-wide.

    ``multirole`` is False: a FUXA account carries one ``groups`` value, so
    advertising a combination the target cannot express would produce a grant that
    silently does something other than what was requested.
    """
    return {
        "identifier": config["asset_id"],
        "name": config["asset_name"],
        "type": "fuxa_hmi",
        "multirole": False,
        "role_options": fuxarules.asset_role_options(catalogue),
    }


# ── Routes ───────────────────────────────────────────────────────────────────

def _get_assets(req: Request, ctx: Context, config: dict) -> Response:
    token = _signin(config)
    return Response(200, {"data": {"assets": [_asset(config, _roles(config, token))]}})


def _get_actors(req: Request, ctx: Context, config: dict) -> Response:
    """Only the accounts this adapter minted.

    An operator's real HMI users must never appear here: Entitle reconciles against
    this list, and an account it believes it provisioned is one it will eventually
    delete.
    """
    token = _signin(config)
    actors = []
    for user in fuxarules.ephemeral_users(_users(config, token)):
        name = str(user.get("username") or "")
        actors.append({"identifier": name,
                       "name": str(user.get("fullname") or name),
                       "type": "fuxa_user",
                       "email": ""})
    return Response(200, {"data": {"actors": actors}})


def _get_all_permissions(req: Request, ctx: Context, config: dict) -> Response:
    """Who holds what, as a MAP keyed by asset id.

    ``fnruntime.entitle.permissions_data`` builds it because the published example
    renders both fields as arrays and the validator does not: an adapter that answers
    with a list fails only this one route, the integration still syncs green, and the
    single symptom is "Failed to fetch the permissions" in Entitle's audit log.

    The asset key is present even with no holders — an asset with an empty list says
    "nobody has this", one the map omits says nothing at all, and only the first is
    something Entitle can reconcile against.
    """
    token = _signin(config)
    rows = []
    for user in fuxarules.ephemeral_users(_users(config, token)):
        rows.append({
            "actor_id": str(user.get("username") or ""),
            "role_code": fuxarules.role_code_for_groups(user.get("groups")),
            "direct_member": True,
        })
    # assets_permissions is the asset-to-asset half; an HMI account nests inside
    # nothing, so {} is the honest answer rather than a placeholder.
    return Response(200, {"data": entitle.permissions_data({config["asset_id"]: rows})})


def _create_actor(req: Request, ctx: Context, config: dict) -> Response:
    """Mint an HMI account. THIS IS THE GRANT."""
    payload = req.json()
    identity = _identity(payload)
    role_code = _role_code(payload)
    try:
        option = fuxarules.role_option(role_code)
    except fuxarules.FuxaRuleError as exc:
        return Response(400, {"error": str(exc)})

    username = fuxarules.ephemeral_username(identity or "requester", ctx.request_id)
    password = fuxarules.generate_password()
    token = _signin(config)

    roles = []
    if config["role_mode"] == "catalogue":
        wanted = option["code"]
        roles = [str(entry.get("id")) for entry in _roles(config, token)
                 if isinstance(entry, dict)
                 and str(entry.get("name") or "").strip().lower() == wanted
                 and entry.get("id")]

    try:
        body = fuxarules.user_payload(username=username, password=password,
                                      role_code=option["code"], roles=roles)
    except fuxarules.FuxaRuleError as exc:
        return Response(400, {"error": str(exc)})

    if not config["dry_run"]:
        # A SINGLE OBJECT under `params`, not a list. And `info` is always valid JSON
        # (see fuxarules.user_info) or FUXA answers 200 while dropping the user.
        _api(config, "POST", "/api/users", {"params": body}, token=token)

    login_info = {
        "username": username,
        "password": password,
        "role_code": option["code"],
        "role": option["display_name"],
        "hmi_url": config["hmi_url"],
    }
    if config["jump_item"]:
        login_info["pra_jump_item"] = config["jump_item"]
        login_info["how"] = (
            f"Open the BeyondTrust Web Jump named {config['jump_item']!r} and sign in "
            f"with these. The HMI is not reachable any other way - the plant admits "
            f"the Gateway and nothing else.")
    else:
        login_info["how"] = (
            "Sign in at the HMI URL above. If it does not load, you are outside the "
            "plant boundary and need the BeyondTrust Web Jump for this cell.")
    login_info["revocation"] = (
        "The account is deleted when this request expires. REST access ends "
        "immediately; a browser tab left open lapses when its session reconnects.")
    if config["dry_run"]:
        login_info["dry_run"] = ("FN_FUXA_DRY_RUN is on - no account was created on "
                                 "the HMI and this credential does not work")

    # actor_data, never a flat dict: `data` is validated against a closed schema with
    # exactly two properties, and the flat shape every adapter first wrote is rejected
    # wholesale - with the account already created.
    return Response(200, {"data": entitle.actor_data(
        username, "fuxa_user", email=identity, name=body["fullname"],
        login_info=login_info)})


def _delete_actor(req: Request, ctx: Context, config: dict) -> Response:
    payload = req.json()
    identifier = str(payload.get("actor_identifier") or "").strip()
    if not identifier:
        return Response(400, {"error": "actor_identifier is required"})
    # Checked on the name from the REQUEST, before any lookup: this is the only thing
    # between a grant integration and the operator's own `admin` account. 403 rather
    # than 400 - it is a refusal, not a malformed call.
    if not fuxarules.is_ephemeral_username(identifier):
        return Response(403, {
            "error": f"refusing to delete {identifier!r}: this adapter manages only "
                     f"the accounts it minted (those prefixed "
                     f"{fuxarules.EPHEMERAL_PREFIX!r})"})

    token = _signin(config)
    existing = fuxarules.match_user(_users(config, token), identifier)
    if not existing:
        # Already gone is 200. Entitle retries a failed delete, and an error here
        # would leave it retrying forever over an account that is not there.
        return Response(200, {"data": {"identifier": identifier,
                                       "already_absent": True}})
    if not config["dry_run"]:
        # The query key is `param`, singular, and it keys on the USERNAME - there is
        # no internal id.
        _api(config, "DELETE",
             "/api/users?" + urllib.parse.urlencode({"param": identifier}),
             token=token)
    return Response(200, {"data": {"identifier": identifier, "deleted": True,
                                   "dry_run": config["dry_run"]}})


def _mode_mismatch(req: Request, ctx: Context, config: dict) -> Response:
    """``give_access`` / ``revoke_access`` — Standing Accounts routes.

    Six lines that turn an undiagnosable 404 into a self-explaining error. Entitle
    infers the mode from the integration's configuration, and if it ever picks
    Standing for this adapter every operation lands on a route that does not exist.
    """
    return Response(400, {
        "error": "this adapter serves Ephemeral Accounts only - it mints an HMI "
                 "account per grant and deletes it on expiry. The integration looks "
                 "configured for Standing Accounts; check its Connection setting.",
        "supported": ["get_assets", "get_actors", "get_all_permissions",
                      "create_actor", "delete_actor", "check_config"]})


def _check_config(req: Request, ctx: Context, config: dict) -> Response:
    """Prove the adapter can do its job, without doing it.

    Called by the wiring play's probe and by an operator when Entitle reports the
    integration unhealthy. It reads, never writes, and reports every problem it finds
    rather than the first — the failures here are usually more than one.
    """
    problems = []
    token = ""
    users = []
    try:
        token = _signin(config)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"sign-in failed: {exc}")
    try:
        users = _users(config, token)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"GET /api/users failed: {exc}")
    if not (config["api_key"] or token):
        problems.append(
            "no credential is in use: either FUXA has secureEnabled off (in which "
            "case it applies NO authorization to these endpoints at all) or the "
            "configured credential did not sign in")
    if not config["jump_item"]:
        problems.append(
            "FN_FUXA_JUMP_ITEM is unset, so a requester is handed a credential with "
            "no route to the HMI - the plant admits the Gateway and nothing else")
    return Response(200, {"data": {
        "valid": not problems,
        "reason": "; ".join(problems),
        "url": config["base"],
        "asset": config["asset_id"],
        "role_mode": config["role_mode"],
        "roles": [option["code"] for option in fuxarules.asset_role_options(
            _roles(config, token))],
        "managed_accounts": len(fuxarules.ephemeral_users(users)),
        "authenticated": bool(config["api_key"] or token),
        "dry_run": config["dry_run"],
        "problems": problems,
    }})


_ROUTES = {
    ("GET", "/get_assets"): _get_assets,
    ("GET", "/get_actors"): _get_actors,
    ("GET", "/get_all_permissions"): _get_all_permissions,
    ("POST", "/create_actor"): _create_actor,
    ("POST", "/delete_actor"): _delete_actor,
    ("POST", "/give_access"): _mode_mismatch,
    ("POST", "/revoke_access"): _mode_mismatch,
    ("POST", "/check_config"): _check_config,
}


def _unconfigured(exc: Exception, ctx: Context, *, reporting: bool) -> Response:
    """What to answer when ``FN_FUXA_*`` cannot be resolved.

    ``_config`` runs AFTER routing so that ``/check_config`` — the one route whose
    whole job is naming what is missing — can still answer when it is what is broken.
    Detail is safe here: dispatch verified the shared secret before calling ``handle``.
    """
    problem = str(exc) or type(exc).__name__
    if reporting:
        return Response(200, {"data": {
            "valid": False, "reason": problem, "url": "", "asset": "",
            "roles": [], "dry_run": _dry_run(), "problems": [problem]}})
    return Response(500, {"error": "function not configured",
                          "problem": problem, "request_id": ctx.request_id})


def handle(req: Request, ctx: Context) -> Response:
    path = (req.path or "/").rstrip("/") or "/"
    handler = _ROUTES.get((req.method, path))
    # Entitle's per-asset variant appends the asset id to the path. One asset here, so
    # it answers the same rows as the unscoped route.
    if handler is None and req.method == "GET" and path.startswith("/get_asset_permissions/"):
        handler = _get_all_permissions
    if handler is None:
        return Response(404, {
            "error": f"no route for {req.method} {path}",
            "routes": sorted(f"{method} {route}" for method, route in _ROUTES)})

    try:
        config = _config()
    except Exception as exc:  # noqa: BLE001
        return _unconfigured(exc, ctx, reporting=handler is _check_config)
    return handler(req, ctx, config)
