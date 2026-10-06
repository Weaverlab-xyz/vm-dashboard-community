"""Microsoft Entra ID through Microsoft Graph v1.0, over raw httpx like the rest of the
app's Graph calls (``azure_service.service_principal_object_id``,
``scripts/bootstrap_entitle_groups.py``). No msgraph SDK.

Two ways to authenticate:

* ``client_secret`` — an app registration in the directory's own tenant, its secret
  behind the row's ``credentials_ref``.
* ``dashboard_azure`` — the dashboard's own Azure identity (``azure_service._ensure_creds``:
  Workload Credentials, config, env or Password Safe, and SPIFFE federation), which then
  needs Graph application permissions as well as its ARM roles.

Application permissions this needs (admin-consented):
``User.Read.All`` and ``Group.Read.All`` to browse, plus ``GroupMember.ReadWrite.All``
for membership writes. ``Directory.Read.All`` / ``*.ReadWrite.All`` cover the same.
"""
import re
import time
from urllib.parse import quote

from .base import (IdPConnection, IdPError, checked_next, group_item, page_size, request,
                   user_item)

LABEL = "Entra ID"
GRAPH = "https://graph.microsoft.com"
_BASE = f"{GRAPH}/v1.0"
_LOGIN = "https://login.microsoftonline.com"
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

_USER_SELECT = "id,displayName,userPrincipalName,mail,accountEnabled"
_GROUP_SELECT = ("id,displayName,description,groupTypes,securityEnabled,mailEnabled,"
                 "onPremisesSyncEnabled,isAssignableToRole")

# Any one of each set satisfies the need.
_READ_USERS = {"User.Read.All", "User.ReadWrite.All", "Directory.Read.All",
               "Directory.ReadWrite.All"}
_READ_GROUPS = {"Group.Read.All", "Group.ReadWrite.All", "GroupMember.Read.All",
                "GroupMember.ReadWrite.All", "Directory.Read.All", "Directory.ReadWrite.All"}
_WRITE_MEMBERS = {"GroupMember.ReadWrite.All", "Group.ReadWrite.All",
                  "Directory.ReadWrite.All"}


def valid_tenant(tenant_id: str) -> bool:
    return bool(_GUID_RE.match(tenant_id or ""))


def _id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _GUID_RE.match(value):
        raise IdPError(f"{LABEL}: {what} id must be an object id (a GUID)")
    return value


async def auth_header(conn: IdPConnection) -> tuple:
    if conn.auth_mode == "dashboard_azure":
        from .. import azure_service
        try:
            credential, _sub = await azure_service._ensure_creds()
            token = await azure_service._to_thread(credential.get_token, f"{GRAPH}/.default")
        except Exception as exc:  # noqa: BLE001
            raise IdPError(f"{LABEL}: the dashboard's Azure identity could not get a Graph "
                           f"token ({type(exc).__name__}) — check Settings → Azure") from exc
        ttl = max(60.0, float(token.expires_on) - time.time())
        return f"Bearer {token.token}", time.monotonic() + ttl - 60
    if conn.auth_mode != "client_secret":
        raise IdPError(f"{LABEL}: unsupported auth mode {conn.auth_mode!r}")
    resp = await request(
        f"{LABEL} sign-in", "POST", f"{_LOGIN}/{conn.tenant_id}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": conn.client_id,
              "client_secret": conn.secret, "scope": f"{GRAPH}/.default"})
    body = resp.json()
    ttl = float(body.get("expires_in") or 3600)
    return f"Bearer {body['access_token']}", time.monotonic() + ttl - 60


def _roles(header: str) -> set:
    from ..azure_service import _jwt_claims
    claims = _jwt_claims(header.split(" ", 1)[-1])
    return set(claims.get("roles") or [])


def _h(header: str, search: bool = False) -> dict:
    h = {"Authorization": header}
    if search:
        h["ConsistencyLevel"] = "eventual"   # $search on directory objects requires it
    return h


def _term(q: str) -> str:
    # $search takes "property:term" in double quotes; a quote or backslash in the term
    # would end the clause, so they go rather than being escaped.
    return re.sub(r'["\\]', "", (q or "").strip())[:100]


def _user(u: dict) -> dict:
    return user_item(id=u.get("id"), display_name=u.get("displayName"),
                     login=u.get("userPrincipalName"), email=u.get("mail"),
                     enabled=u.get("accountEnabled"))


def group_editable(g: dict) -> str:
    """Why the dashboard will not change this group's members, or "" if it may."""
    types = g.get("groupTypes") or []
    if "DynamicMembership" in types:
        return "a dynamic group: its rule decides the members"
    if g.get("onPremisesSyncEnabled"):
        return "synced from on-premises AD: change it there"
    if g.get("isAssignableToRole"):
        return "role-assignable: it grants Entra roles, change it in the Entra admin center"
    if g.get("mailEnabled") and "Unified" not in types:
        return "a distribution list or mail-enabled security group: Exchange owns its members"
    return ""


def _group(g: dict) -> dict:
    types = g.get("groupTypes") or []
    kind = ("microsoft365" if "Unified" in types
            else "security" if g.get("securityEnabled") else "distribution")
    return group_item(id=g.get("id"), name=g.get("displayName"), kind=kind,
                      description=g.get("description"), editable_reason=group_editable(g))


async def _page(header, url, params, cursor, *, search=False, label="list"):
    if cursor:
        resp = await request(f"{LABEL} {label}", "GET", checked_next(cursor, _BASE),
                             headers=_h(header, search))
    else:
        resp = await request(f"{LABEL} {label}", "GET", url, headers=_h(header, search),
                             params=params)
    body = resp.json()
    return body.get("value") or [], body.get("@odata.nextLink") or ""


async def test(conn: IdPConnection, header: str) -> dict:
    roles = _roles(header)
    missing = []
    if not roles & _READ_USERS:
        missing.append("User.Read.All")
    if not roles & _READ_GROUPS:
        missing.append("Group.Read.All")
    writes_missing = [] if roles & _WRITE_MEMBERS else ["GroupMember.ReadWrite.All"]
    if not missing:
        await request(f"{LABEL} test", "GET", f"{_BASE}/users",
                      headers=_h(header), params={"$top": "1", "$select": "id"})
    return {"ok": not missing, "missing": missing, "writes_missing": writes_missing,
            "detail": ("Graph answered." if not missing else
                       "The app is missing Graph application permissions: "
                       + ", ".join(missing) + " (grant them and admin-consent).")}


async def list_users(conn, header, q="", cursor=""):
    params = {"$top": str(page_size(conn)), "$select": _USER_SELECT}
    term = _term(q)
    if term:
        params["$search"] = f'"displayName:{term}" OR "userPrincipalName:{term}"'
    rows, nxt = await _page(header, f"{_BASE}/users", params, cursor, search=bool(term),
                            label="users")
    return {"items": [_user(u) for u in rows], "next": nxt}


async def list_groups(conn, header, q="", cursor=""):
    params = {"$top": str(page_size(conn)), "$select": _GROUP_SELECT}
    term = _term(q)
    if term:
        params["$search"] = f'"displayName:{term}"'
    rows, nxt = await _page(header, f"{_BASE}/groups", params, cursor, search=bool(term),
                            label="groups")
    return {"items": [_group(g) for g in rows], "next": nxt}


async def get_group(conn, header, gid):
    gid = _id(gid, "group")
    resp = await request(f"{LABEL} group", "GET", f"{_BASE}/groups/{gid}",
                         headers=_h(header), params={"$select": _GROUP_SELECT})
    return _group(resp.json())


async def group_members(conn, header, gid, cursor=""):
    gid = _id(gid, "group")
    rows, nxt = await _page(header, f"{_BASE}/groups/{gid}/members",
                            {"$top": str(page_size(conn)), "$select": _USER_SELECT},
                            cursor, label="members")
    items = []
    for m in rows:
        item = _user(m)
        # Groups nest: a member may be a group, device or service principal.
        item["kind"] = (m.get("@odata.type") or "").rsplit(".", 1)[-1] or "user"
        items.append(item)
    return {"items": items, "next": nxt}


async def user_groups(conn, header, uid):
    uid = _id(uid, "user")
    rows, _nxt = await _page(header, f"{_BASE}/users/{quote(uid)}/memberOf",
                             {"$top": "200", "$select": _GROUP_SELECT}, "", label="memberOf")
    return {"items": [_group(g) for g in rows
                      if (g.get("@odata.type") or "").endswith(".group")], "next": ""}


async def add_member(conn, header, gid, uid) -> dict:
    gid, uid = _id(gid, "group"), _id(uid, "user")
    try:
        await request(f"{LABEL} add member", "POST", f"{_BASE}/groups/{gid}/members/$ref",
                      headers=_h(header),
                      json={"@odata.id": f"{_BASE}/directoryObjects/{uid}"})
    except IdPError as exc:
        if "already exist" in str(exc):
            return {"changed": False}
        raise
    return {"changed": True}


async def remove_member(conn, header, gid, uid) -> dict:
    gid, uid = _id(gid, "group"), _id(uid, "user")
    try:
        await request(f"{LABEL} remove member", "DELETE",
                      f"{_BASE}/groups/{gid}/members/{uid}/$ref", headers=_h(header))
    except IdPError as exc:
        if "(HTTP 404)" in str(exc):
            return {"changed": False}
        raise
    return {"changed": True}
