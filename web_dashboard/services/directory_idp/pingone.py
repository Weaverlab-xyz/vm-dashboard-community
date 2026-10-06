"""PingOne through the Platform API.

A worker application in the environment authenticates with client credentials
(``client_secret_basic`` by default, ``options.token_endpoint_auth = "post"`` for an app
set to client_secret_post). Give it the *Identity Data Read Only* role to browse, or
*Identity Data Admin* for membership writes.

The row's ``endpoint`` is the region's top-level domain (``com``, ``eu``, ``ca``,
``asia``, ``com.au``, ``sg``) rather than a URL, so the hosts are always PingOne's own:
``auth.pingone.<tld>`` for tokens and ``api.pingone.<tld>`` for the API.
"""
import re
import time

from .base import (IdPConnection, IdPError, checked_next, group_item, page_size, request,
                   user_item)

LABEL = "PingOne"
TLDS = ("com", "eu", "ca", "asia", "com.au", "sg")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def valid_environment(env_id: str) -> bool:
    return bool(_UUID_RE.match(env_id or ""))


def _tld(conn: IdPConnection) -> str:
    tld = (conn.endpoint or "").strip().lower().lstrip(".")
    if tld not in TLDS:
        raise IdPError(f"{LABEL}: region {conn.endpoint!r} is not one of {', '.join(TLDS)}")
    return tld


def _api(conn: IdPConnection) -> str:
    if not valid_environment(conn.tenant_id):
        raise IdPError(f"{LABEL}: the environment id must be a UUID")
    return f"https://api.pingone.{_tld(conn)}/v1/environments/{conn.tenant_id}"


def _id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _UUID_RE.match(value):
        raise IdPError(f"{LABEL}: {what} id must be a UUID")
    return value


async def auth_header(conn: IdPConnection) -> tuple:
    if conn.auth_mode != "client_secret":
        raise IdPError(f"{LABEL}: unsupported auth mode {conn.auth_mode!r}")
    url = f"https://auth.pingone.{_tld(conn)}/{_id(conn.tenant_id, 'environment')}/as/token"
    data = {"grant_type": "client_credentials"}
    auth = None
    if (conn.options.get("token_endpoint_auth") or "basic") == "post":
        data.update(client_id=conn.client_id, client_secret=conn.secret)
    else:
        auth = (conn.client_id, conn.secret)
    resp = await request(f"{LABEL} sign-in", "POST", url, data=data, auth=auth)
    body = resp.json()
    ttl = float(body.get("expires_in") or 3600)
    return f"Bearer {body['access_token']}", time.monotonic() + ttl - 60


def _h(header: str) -> dict:
    return {"Authorization": header}


def _term(q: str) -> str:
    return re.sub(r'["\\]', "", (q or "").strip())[:100]


def _user(u: dict) -> dict:
    name = u.get("name") or {}
    display = name.get("formatted") or " ".join(
        x for x in (name.get("given"), name.get("family")) if x)
    return user_item(id=u.get("id"), display_name=display, login=u.get("username"),
                     email=u.get("email"), enabled=u.get("enabled"))


def group_editable(g: dict) -> str:
    if g.get("userFilter"):
        return "a dynamic group: its user filter decides the members"
    return ""


def _group(g: dict) -> dict:
    return group_item(id=g.get("id"), name=g.get("name"),
                      kind="dynamic" if g.get("userFilter") else "static",
                      description=g.get("description"), editable_reason=group_editable(g))


def _embedded(body: dict, key: str) -> list:
    return ((body or {}).get("_embedded") or {}).get(key) or []


async def _page(conn, header, path, params, cursor, key, label):
    api = _api(conn)
    if cursor:
        resp = await request(f"{LABEL} {label}", "GET", checked_next(cursor, api),
                             headers=_h(header))
    else:
        resp = await request(f"{LABEL} {label}", "GET", f"{api}{path}",
                             headers=_h(header), params=params)
    body = resp.json()
    nxt = (((body.get("_links") or {}).get("next")) or {}).get("href") or ""
    return _embedded(body, key), nxt


async def test(conn: IdPConnection, header: str) -> dict:
    api = _api(conn)
    await request(f"{LABEL} test", "GET", f"{api}/users", headers=_h(header),
                  params={"limit": "1"})
    await request(f"{LABEL} test", "GET", f"{api}/groups", headers=_h(header),
                  params={"limit": "1"})
    return {"ok": True, "missing": [], "writes_missing": [],
            "detail": "PingOne answered. Writes need the Identity Data Admin role."}


async def list_users(conn, header, q="", cursor=""):
    params = {"limit": str(page_size(conn))}
    term = _term(q)
    if term:
        params["filter"] = " or ".join(f'{f} sw "{term}"'
                                       for f in ("username", "email", "name.given",
                                                 "name.family"))
    rows, nxt = await _page(conn, header, "/users", params, cursor, "users", "users")
    return {"items": [_user(u) for u in rows], "next": nxt}


async def list_groups(conn, header, q="", cursor=""):
    params = {"limit": str(page_size(conn))}
    term = _term(q)
    if term:
        params["filter"] = f'name sw "{term}"'
    rows, nxt = await _page(conn, header, "/groups", params, cursor, "groups", "groups")
    return {"items": [_group(g) for g in rows], "next": nxt}


async def get_group(conn, header, gid):
    gid = _id(gid, "group")
    resp = await request(f"{LABEL} group", "GET", f"{_api(conn)}/groups/{gid}",
                         headers=_h(header))
    return _group(resp.json())


async def group_members(conn, header, gid, cursor=""):
    gid = _id(gid, "group")
    rows, nxt = await _page(conn, header, "/users",
                            {"limit": str(page_size(conn)),
                             "filter": f'memberOfGroups[id eq "{gid}"]'},
                            cursor, "users", "members")
    return {"items": [_user(u) for u in rows], "next": nxt}


async def user_groups(conn, header, uid):
    uid = _id(uid, "user")
    rows, _ = await _page(conn, header, f"/users/{uid}/memberOfGroups",
                          {"limit": "100", "expand": "group"}, "", "groupMemberships",
                          "user groups")
    items = []
    for m in rows:
        g = ((m.get("_embedded") or {}).get("group")) or m
        items.append(_group(g))
    return {"items": items, "next": ""}


async def add_member(conn, header, gid, uid) -> dict:
    gid, uid = _id(gid, "group"), _id(uid, "user")
    try:
        await request(f"{LABEL} add member", "POST",
                      f"{_api(conn)}/users/{uid}/memberOfGroups", headers=_h(header),
                      json={"id": gid})
    except IdPError as exc:
        if "already" in str(exc).lower():
            return {"changed": False}
        raise
    return {"changed": True}


async def remove_member(conn, header, gid, uid) -> dict:
    gid, uid = _id(gid, "group"), _id(uid, "user")
    try:
        await request(f"{LABEL} remove member", "DELETE",
                      f"{_api(conn)}/users/{uid}/memberOfGroups/{gid}", headers=_h(header))
    except IdPError as exc:
        if "(HTTP 404)" in str(exc):
            return {"changed": False}
        raise
    return {"changed": True}
