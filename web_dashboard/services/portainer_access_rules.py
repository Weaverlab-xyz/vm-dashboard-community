"""Portainer access-management rules — pure, stdlib-only, shared by two callers.

``portainer_service`` (the dashboard's httpx client) and the ``portainer_access``
Cloud Function both need to decide the same things: which user a name refers to,
whether a membership already exists, and whether a role id is one we are willing to
hand out. The HTTP transports differ — httpx in the dashboard, ``urllib`` in the
function, which has no third-party packages — but the DECISIONS must not, because
this is exactly where the subtle, dangerous bugs live:

  * a case-sensitive user lookup creates a second "Alice" beside "alice" and grants
    access to the wrong one
  * a membership matched on the user alone revokes every team they belong to
  * ids that arrive as strings make every revoke a silent no-op that reports success
  * a role id off by one hands out administrator

So the rules live here, once, tested once, and the module is vendored into the
function's zip verbatim — the same arrangement ``cloud_db_sql_service`` has with
``db_grant``. Nothing here does I/O; every function takes the already-fetched list.
"""
import re

# ⚠️  ROLE IDS ARE NUMERIC AND EASY TO INVERT. Portainer uses 1 = administrator and
#     2 = standard for USERS, and 1 = team LEADER, 2 = member for MEMBERSHIPS. Both
#     are "1 is the powerful one", and both are silently accepted if swapped — a JIT
#     grant that hands out administrator is not an error the API reports.
USER_ROLE_ADMIN = 1
USER_ROLE_STANDARD = 2
TEAM_ROLE_LEADER = 1
TEAM_ROLE_MEMBER = 2

VALID_USER_ROLES = (USER_ROLE_ADMIN, USER_ROLE_STANDARD)
VALID_TEAM_ROLES = (TEAM_ROLE_LEADER, TEAM_ROLE_MEMBER)


class PortainerRuleError(Exception):
    """Raised on a value we refuse to send to Portainer."""


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _int(value) -> int:
    """Coerce an id. Portainer's JSON has returned these as strings in some
    versions, and an int/str mismatch makes every lookup miss — which surfaces as a
    revoke that reports success while the access remains."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return -1


def validate_user_role(role: int) -> int:
    """A user role we are willing to create. Portainer accepts any integer."""
    if _int(role) not in VALID_USER_ROLES:
        raise PortainerRuleError(
            f"invalid Portainer user role {role!r} (expected {USER_ROLE_STANDARD} "
            f"standard or {USER_ROLE_ADMIN} administrator)")
    return _int(role)


def validate_team_role(role: int) -> int:
    if _int(role) not in VALID_TEAM_ROLES:
        raise PortainerRuleError(
            f"invalid Portainer team role {role!r} (expected {TEAM_ROLE_MEMBER} "
            f"member or {TEAM_ROLE_LEADER} leader)")
    return _int(role)


def match_user(users, username: str) -> dict:
    """The user with this username, or ``{}``.

    Case-insensitive: Portainer stores the name as given but treats logins
    case-insensitively.
    """
    target = _norm(username)
    if not target:
        return {}
    for user in users or []:
        if _norm(user.get("Username")) == target:
            return user
    return {}


def match_team(teams, name: str) -> dict:
    """The team with this name, or ``{}`` (case-insensitive, as for users)."""
    target = _norm(name)
    if not target:
        return {}
    for team in teams or []:
        if _norm(team.get("Name")) == target:
            return team
    return {}


def match_membership(memberships, user_id, team_id) -> dict:
    """The membership joining this user to this team, or ``{}``.

    Matches on BOTH ids — the user alone would match every team they belong to —
    and coerces them, so a string id from Portainer still finds the row.
    """
    want_user, want_team = _int(user_id), _int(team_id)
    if want_user < 0 or want_team < 0:
        return {}
    for row in memberships or []:
        if _int(row.get("UserID")) == want_user and _int(row.get("TeamID")) == want_team:
            return row
    return {}


def memberships_for_user(memberships, user_id) -> list:
    """Every membership a user holds — what a delete_actor has to clean up."""
    want = _int(user_id)
    return [row for row in memberships or [] if _int(row.get("UserID")) == want]


# Portainer usernames: no whitespace, and Portainer itself rejects several
# punctuation characters. Keep to a conservative allowlist so a generated name is
# always creatable and always safe to interpolate into a URL path.
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")


def ephemeral_username(identity: str, token: str) -> str:
    """A traceable, collision-resistant account name for one grant.

    Derived from the requester rather than random so an operator looking at
    Portainer's user list can tell WHO an account belongs to without a lookup — the
    first question asked of any unexpected account.
    """
    slug = re.sub(r"[^a-z0-9]", "-", _norm(identity)).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:32] or "jit"
    suffix = re.sub(r"[^a-z0-9]", "", _norm(token))[:12] or "x"
    name = f"jit-{slug}-{suffix}"
    if not name[0].isalpha():
        name = "j" + name
    return name[:64]


def is_ephemeral_username(username: str) -> bool:
    """Whether this account was minted by us.

    The adapter reports and deletes only its OWN accounts: an operator's real
    Portainer users must never appear in get_actors, and must never be deletable
    through a grant integration.
    """
    return _norm(username).startswith("jit-")


def validate_username(username: str) -> str:
    if not _USERNAME_RE.match(_norm(username)):
        raise PortainerRuleError(
            f"unsafe Portainer username {username!r} — must match {_USERNAME_RE.pattern}")
    return _norm(username)
