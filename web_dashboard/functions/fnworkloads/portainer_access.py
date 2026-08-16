"""Entitle Remote Adapter for just-in-time Portainer access.

Implements the Remote Adapter contract
(docs.beyondtrust.com/entitle/docs/open-api-definition) against a Portainer CE
instance, so Entitle can mint a short-lived Portainer account, put it in a team
that already has access to an environment, and take both away again.

Portainer has no Entitle connector at all, so this is the only route to JIT access
for it.

    GET  /get_assets        the teams access can be granted through
    GET  /get_actors        the ephemeral accounts THIS adapter created
    GET  /get_all_permissions
    POST /create_actor      mint the Portainer user (in no team — no access yet)
    POST /give_access       add it to the team named by role_code
    POST /revoke_access     remove it from that team
    POST /delete_actor      delete the user
    POST /check_config

**Access is granted through team membership, not per-user.** In Portainer an access
policy attaches to an environment or environment group and names teams; an operator
sets that up once, and a grant is then a single membership row. That keeps the
reversible per-request operation separate from the standing configuration, so a
revoke can never leave a half-dismantled access policy behind.

Two safety rules that the matching rules module enforces and this file relies on:

  * ``create_actor`` makes a STANDARD user, never an administrator, and puts it in
    no team — so an actor whose ``give_access`` never arrives can reach nothing.
  * ``get_actors`` and ``delete_actor`` only ever see accounts this adapter minted
    (``jit-`` prefix). An operator's real Portainer users must never be listed to
    Entitle, and must never be deletable through a grant integration.

HTTP is ``urllib`` from the standard library, not httpx: the fnruntime contract is
stdlib-only, and Portainer's API is plain JSON over HTTPS. The decision logic —
which user a name refers to, whether a membership exists, whether a role id is one
we will hand out — is NOT reimplemented here; it comes from
``web_dashboard/services/portainer_access_rules.py``, vendored into the zip so the
dashboard client and this adapter cannot disagree.
"""
import json
import os
import secrets
import ssl
import string
import urllib.error
import urllib.request

from fnruntime import logs
from fnruntime.contract import Context, Request, Response

NAME = "portainer_access"
DESCRIPTION = "Entitle Remote Adapter: just-in-time Portainer access via team membership."

# Vendored beside this module at package time; in-repo it is the service module.
try:
    import portainerrules as _rules  # noqa: E402
except ImportError:  # pragma: no cover - exercised in-repo, never in the zip
    from web_dashboard.services import portainer_access_rules as _rules  # noqa: E402

_TRUTHY = ("1", "true", "yes", "on")
_TIMEOUT = 20


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or default).strip()


def _dry_run() -> bool:
    raw = os.environ.get("FN_PORTAINER_DRY_RUN")
    return True if raw is None else raw.strip().lower() in _TRUTHY


def _config() -> dict:
    base = _env("FN_PORTAINER_URL").rstrip("/")
    if not base:
        raise RuntimeError("FN_PORTAINER_URL is not set")
    api_key = _env("FN_PORTAINER_API_KEY")
    if not api_key and not _dry_run():
        raise RuntimeError(
            "FN_PORTAINER_API_KEY is not set (GCP secret env var / Azure Key Vault "
            "reference / AWS Secrets Manager via FN_PORTAINER_KEY_SECRET_ID)")
    return {
        "base": base,
        "api_key": api_key,
        "verify_ssl": _env("FN_PORTAINER_VERIFY_SSL", "1").lower() in _TRUTHY,
    }


def _api(config: dict, method: str, path: str, body: dict = None):
    """One Portainer API call. stdlib only."""
    url = config["base"] + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-API-Key", config["api_key"])
    if data is not None:
        request.add_header("Content-Type", "application/json")
    context = None
    if not config["verify_ssl"]:
        # Opt-in only. A self-signed Portainer is common in a lab; this is never
        # the default, and the operator has to say so.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT, context=context) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "DELETE":
            # Already gone is success — a revoke that errors on an
            # already-removed grant leaves the caller retrying forever.
            return None
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"portainer {method} {path} failed: HTTP {exc.code} {detail}") from None
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        return None


def _generate_password(length: int = 24) -> str:
    """Portainer enforces a 12-character minimum; 24 with mixed classes clears it
    with room to spare."""
    alphabet = string.ascii_letters + string.digits + "#-_"
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "#-_"]
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice(alphabet) for _ in range(max(length, 12) - len(pools))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _teams(config: dict) -> list:
    return _api(config, "GET", "/api/teams") or []


def _users(config: dict) -> list:
    return _api(config, "GET", "/api/users") or []


def _memberships(config: dict) -> list:
    return _api(config, "GET", "/api/team_memberships") or []


def _asset(team: dict) -> dict:
    """A team, as an Entitle asset. role_code is the team name, so an operator
    reading an Entitle request sees the team they configured access on."""
    name = str(team.get("Name") or "")
    return {
        "identifier": f"portainer:team:{team.get('Id')}",
        "name": name,
        "type": "portainer_team",
        "role_options": [{
            "code": name,
            "display_name": f"Member of {name}",
            "available": True,
            "permissions": ["team_member"],
        }],
    }


def _resolve_team(config: dict, payload: dict) -> dict:
    """The team a request refers to, by role_code or by asset identifier."""
    teams = _teams(config)
    role_code = str(payload.get("role_code") or "").strip()
    if role_code:
        team = _rules.match_team(teams, role_code)
        if team:
            return team
    identifier = str((payload.get("asset") or {}).get("identifier") or "")
    if identifier.startswith("portainer:team:"):
        wanted = identifier.rsplit(":", 1)[-1]
        for team in teams:
            if str(team.get("Id")) == wanted:
                return team
    return {}


# ── Routes ───────────────────────────────────────────────────────────────────

def _get_assets(req, ctx, config):
    if _dry_run() and not config["api_key"]:
        return Response(200, {"next": "", "data": {"assets": []}})
    return Response(200, {"next": "", "data": {
        "assets": [_asset(team) for team in _teams(config)]}})


def _get_actors(req, ctx, config):
    """Only the accounts this adapter minted. Listing an operator's real users to
    Entitle would offer them up for deletion through a grant integration."""
    if _dry_run() and not config["api_key"]:
        return Response(200, {"next": "", "data": {"actors": []}})
    actors = [
        {"identifier": str(user.get("Username")), "name": str(user.get("Username")),
         "type": "portainer_user", "email": ""}
        for user in _users(config)
        if _rules.is_ephemeral_username(user.get("Username"))
    ]
    return Response(200, {"next": "", "data": {"actors": actors}})


def _get_all_permissions(req, ctx, config):
    if _dry_run() and not config["api_key"]:
        return Response(200, {"next": "", "data": {"actors_permissions": [],
                                                   "assets_permissions": []}})
    users = {int(u.get("Id", -1)): str(u.get("Username") or "")
             for u in _users(config)
             if _rules.is_ephemeral_username(u.get("Username"))}
    teams = {int(t.get("Id", -1)): str(t.get("Name") or "") for t in _teams(config)}
    actors_permissions = []
    for row in _memberships(config):
        username = users.get(int(row.get("UserID", -1) or -1))
        if not username:
            continue
        actors_permissions.append({
            "actor_id": username,
            "role_code": teams.get(int(row.get("TeamID", -1) or -1), ""),
            "direct_member": True,
        })
    return Response(200, {"next": "", "data": {
        "actors_permissions": actors_permissions,
        "assets_permissions": [{"asset_id": f"portainer:team:{tid}", "role_code": name}
                               for tid, name in teams.items()],
    }})


def _create_actor(req, ctx, config):
    payload = req.json()
    actor = payload.get("actor") or payload
    identity = str(actor.get("email") or actor.get("identifier")
                   or actor.get("name") or "").strip()
    if not identity:
        return Response(400, {"error": "create_actor needs an actor with an email or identifier"})
    username = _rules.ephemeral_username(identity, ctx.request_id)
    password = _generate_password()
    logs.emit("info", "create_actor", request_id=ctx.request_id,
              identity=identity, username=username, dry_run=_dry_run())
    if _dry_run():
        return Response(200, {"data": {
            "dry_run": True, "identifier": username, "name": username,
            "type": "portainer_user",
            "would": f"POST /api/users {username} role=standard, no team"}})
    # STANDARD, and in no team: an actor whose give_access never arrives can reach
    # nothing.
    created = _api(config, "POST", "/api/users", {
        "Username": _rules.validate_username(username),
        "Password": password,
        "Role": _rules.validate_user_role(_rules.USER_ROLE_STANDARD),
    }) or {}
    return Response(200, {"data": {
        "identifier": username, "name": username, "type": "portainer_user",
        "portainer_user_id": created.get("Id"),
        "username": username, "password": password,
        "url": config["base"],
    }})


def _delete_actor(req, ctx, config):
    payload = req.json()
    username = str(payload.get("actor_identifier") or payload.get("identifier") or "").strip()
    if not username:
        return Response(400, {"error": "delete_actor needs actor_identifier"})
    if not _rules.is_ephemeral_username(username):
        # Refuse rather than 400-and-continue: this is the guard that stops a grant
        # integration deleting an operator's real Portainer account.
        return Response(403, {"error": f"{username!r} was not created by this adapter"})
    logs.emit("info", "delete_actor", request_id=ctx.request_id,
              username=username, dry_run=_dry_run())
    if _dry_run():
        return Response(200, {"data": {"dry_run": True, "identifier": username,
                                       "would": f"DELETE /api/users/{username}"}})
    user = _rules.match_user(_users(config), username)
    if not user:
        return Response(200, {"data": {"identifier": username, "already_absent": True}})
    _api(config, "DELETE", f"/api/users/{int(user.get('Id'))}")
    return Response(200, {"data": {"identifier": username, "deleted": True}})


def _membership_change(req, ctx, config, *, add: bool):
    payload = req.json()
    username = str(payload.get("actor_identifier") or "").strip()
    if not username:
        return Response(400, {"error": "actor_identifier is required"})
    if not _rules.is_ephemeral_username(username):
        return Response(403, {"error": f"{username!r} was not created by this adapter"})

    action = "give_access" if add else "revoke_access"
    logs.emit("info", action, request_id=ctx.request_id, username=username,
              role_code=payload.get("role_code"), dry_run=_dry_run())
    if _dry_run():
        return Response(200, {"data": {
            "dry_run": True, "actor_identifier": username,
            "role_code": payload.get("role_code"),
            "would": ("POST /api/team_memberships" if add
                      else "DELETE /api/team_memberships/<id>")}})

    team = _resolve_team(config, payload)
    if not team:
        return Response(404, {"error": "no Portainer team matches this request's "
                                       "role_code or asset identifier"})
    user = _rules.match_user(_users(config), username)
    if not user:
        return Response(404, {"error": f"unknown actor {username!r}"})

    existing = _rules.match_membership(_memberships(config), user.get("Id"), team.get("Id"))
    if add:
        if existing:
            # Idempotent: a duplicate row would make the FIRST revoke look
            # successful while access remained.
            return Response(200, {"data": {"actor_identifier": username,
                                           "role_code": team.get("Name"),
                                           "already_member": True}})
        _api(config, "POST", "/api/team_memberships", {
            "UserID": int(user.get("Id")), "TeamID": int(team.get("Id")),
            "Role": _rules.validate_team_role(_rules.TEAM_ROLE_MEMBER),
        })
        return Response(200, {"data": {"actor_identifier": username,
                                       "role_code": team.get("Name")}})
    if not existing:
        return Response(200, {"data": {"actor_identifier": username,
                                       "role_code": team.get("Name"),
                                       "already_absent": True}})
    _api(config, "DELETE", f"/api/team_memberships/{int(existing.get('Id'))}")
    return Response(200, {"data": {"actor_identifier": username,
                                   "role_code": team.get("Name"), "removed": True}})


def _give_access(req, ctx, config):
    return _membership_change(req, ctx, config, add=True)


def _revoke_access(req, ctx, config):
    return _membership_change(req, ctx, config, add=False)


def _check_config(req, ctx, config):
    problems = []
    teams = []
    if not config["api_key"]:
        problems.append("FN_PORTAINER_API_KEY is not set")
    else:
        try:
            teams = [str(t.get("Name")) for t in _teams(config)]
        except Exception as exc:
            problems.append(str(exc))
    if not problems and not teams:
        problems.append("Portainer has no teams — access is granted through team "
                        "membership, so there is nothing to grant")
    return Response(200, {"data": {
        "valid": not problems, "url": config["base"], "teams": teams,
        "dry_run": _dry_run(), "problems": problems,
    }})


_ROUTES = {
    ("GET", "/get_assets"): _get_assets,
    ("GET", "/get_actors"): _get_actors,
    ("GET", "/get_all_permissions"): _get_all_permissions,
    ("POST", "/create_actor"): _create_actor,
    ("POST", "/delete_actor"): _delete_actor,
    ("POST", "/give_access"): _give_access,
    ("POST", "/revoke_access"): _revoke_access,
    ("POST", "/check_config"): _check_config,
}


def handle(req: Request, ctx: Context) -> Response:
    config = _config()
    path = (req.path or "/").rstrip("/") or "/"
    handler = _ROUTES.get((req.method, path))
    if handler is None and path.startswith("/get_asset_permissions/") and req.method == "GET":
        handler = _get_all_permissions
    if handler is None:
        return Response(404, {
            "error": f"no route for {req.method} {path}",
            "routes": sorted(f"{method} {route}" for method, route in _ROUTES),
        })
    return handler(req, ctx, config)
