"""FUXA HMI access rules — pure, stdlib-only, vendored into the adapter's zip.

The ``fuxa_hmi_access`` Cloud Function decides four things, and every one of them is
somewhere a subtle bug grants more than it says:

  * which numeric ``groups`` value a requested role becomes — FUXA's permission model
    is a BITMASK, and an off-by-one bit is administrator;
  * which accounts this adapter is willing to report and delete — an operator's real
    HMI users must never be touched by a grant integration;
  * what a minted account is called, so an unexpected account in FUXA's user list can
    be traced to a person without a lookup;
  * what goes in the ``info`` column, which FUXA parses as JSON *after* answering 200
    and silently drops the user if it cannot.

So they live here, once, tested once, and the module is vendored into the function's
zip verbatim — the same arrangement ``portainer_access_rules`` has with
``portainer_access``. Nothing here does I/O; every function takes already-fetched data.

Everything below was read from the FUXA v1.3.4 source, not from its documentation,
which is wrong in at least three places that matter (see ``fuxa_hmi_access``).

⚠️  VERIFICATION GATE — the BIT SEMANTICS are the one thing not yet confirmed
    against a running FUXA. The bit VALUES and the two-halves layout are read from
    source (``client/src/app/_models/user.ts``: the low byte is the "enabled" set and
    ``<< 8`` is the "show" set), but exactly how the runtime evaluates them for a view
    or a control has not been observed live. That is why :func:`groups_for` is built
    to fail in the SAFE direction: it refuses the administrator bit and the
    all-permissions sentinels outright, so the worst outcome of a wrong reading is a
    grant that does too little, never one that hands out administrator. Confirm the
    semantics before widening ``ROLE_OPTIONS``.
"""
import json
import re
import secrets

# FUXA's group bits (client/src/app/_models/user.ts, UserGroups). The low byte is the
# set whose controls are ENABLED; the same bits shifted left 8 are the set whose views
# are SHOWN. A user's `groups` column is one integer carrying both halves.
GROUP_VIEWER = 1
GROUP_OPERATOR = 2
GROUP_ENGINEER = 4
GROUP_SUPERVISOR = 8
GROUP_MANAGER = 16
GROUP_F = 32
GROUP_G = 64
GROUP_ADMINISTRATOR = 128

SHOW_SHIFT = 8

# ⚠️  NEVER HAND THESE OUT. 128 is the administrator bit; -1 and 255 are what FUXA
#     itself treats as "all groups" (server/api/jwt-helper.js adminGroups = [-1, 255]),
#     and the seeded `admin` account carries -1. A JIT grant that produces any of them
#     is a full takeover of the HMI, and FUXA reports no error for it.
FORBIDDEN_BITS = GROUP_ADMINISTRATOR
ADMIN_SENTINELS = (-1, 255)

# The closed set this adapter will publish and grant. Deliberately two: an HMI grant
# is "look" or "look and act", and every additional code has to be justified by
# something the plant actually distinguishes. `permissions` is descriptive only —
# Entitle shows it to the requester and does not act on it.
ROLE_OPTIONS = (
    {"code": "viewer",
     "display_name": "HMI viewer - read-only screens",
     "groups": GROUP_VIEWER,
     "permissions": ("fuxa:view",)},
    {"code": "operator",
     "display_name": "HMI operator - view and command",
     "groups": GROUP_VIEWER | GROUP_OPERATOR,
     "permissions": ("fuxa:view", "fuxa:command")},
)

DEFAULT_ROLE_CODE = "viewer"

# A minted account is named after the requester so that "who does this belong to" is
# answerable from FUXA's user list alone, and prefixed so that this adapter can tell
# its own accounts from an operator's. Both halves are load-bearing.
EPHEMERAL_PREFIX = "jit-"
_USERNAME_RE = re.compile(r"^jit-[a-z0-9][a-z0-9._-]{2,59}$")

# No quotes, no backslash, no space: the value is carried through JSON into Entitle's
# request detail and read back by a human off a screen. Ambiguous glyphs (O/0, l/1)
# are kept OUT for the same reason — a password nobody can retype is a support call.
_PASSWORD_ALPHABET = ("ABCDEFGHJKLMNPQRSTUVWXYZ"
                      "abcdefghijkmnopqrstuvwxyz"
                      "23456789"
                      "@#%*+=?-_")
PASSWORD_LENGTH = 24


class FuxaRuleError(Exception):
    """Raised on a value we refuse to send to FUXA, or to accept from Entitle."""


def _norm(value) -> str:
    return str(value or "").strip()


def _lower(value) -> str:
    return _norm(value).lower()


# ── Roles ────────────────────────────────────────────────────────────────────

def role_codes() -> tuple:
    return tuple(option["code"] for option in ROLE_OPTIONS)


def role_option(code: str) -> dict:
    """The published option ``code`` names.

    An unknown code RAISES rather than falling back to a default. A grant for a role
    this adapter does not publish is a configuration mismatch between Entitle's
    catalogue and this function, and quietly substituting one would either grant the
    wrong thing or make the mismatch permanently invisible.
    """
    wanted = _lower(code) or DEFAULT_ROLE_CODE
    for option in ROLE_OPTIONS:
        if option["code"] == wanted:
            return dict(option)
    raise FuxaRuleError(
        f"unknown role_code {code!r} - this adapter publishes "
        f"{', '.join(role_codes())}. Entitle's catalogue and get_assets have drifted; "
        f"re-sync the integration's resources.")


def is_admin_groups(value) -> bool:
    """Whether ``value`` would give administrator. The guard, not a formatter."""
    try:
        groups = int(value)
    except (TypeError, ValueError):
        # Unparseable is treated as dangerous: it is a value we did not compute, and
        # "I cannot tell" must not read as "safe".
        return True
    if groups in ADMIN_SENTINELS or groups < 0:
        return True
    enabled = groups & 0xFF
    shown = (groups >> SHOW_SHIFT) & 0xFF
    return bool((enabled | shown) & FORBIDDEN_BITS)


def groups_for(code: str) -> int:
    """The ``groups`` integer for a published role code.

    Both halves are set from the same bits: a role that may act on a screen has to be
    able to see it, and splitting them would let a grant enable a control on a view
    the account cannot open — which presents as a broken HMI rather than as a
    permission problem.
    """
    option = role_option(code)
    bits = int(option["groups"]) & 0xFF
    groups = bits | (bits << SHOW_SHIFT)
    if is_admin_groups(groups):
        # Unreachable from ROLE_OPTIONS as written, and that is the point: this is the
        # assertion that keeps it unreachable when somebody edits the table.
        raise FuxaRuleError(
            f"refusing to mint groups={groups} for role {code!r}: it carries the "
            f"administrator bit or an all-permissions sentinel")
    return groups


def role_code_for_groups(value) -> str:
    """The published code whose value is exactly ``value``, or ``""``.

    Used to report what an account HOLDS. Returns "" rather than guessing at a near
    match: an account an operator made by hand, or one from an older version of this
    table, holds something this adapter cannot name, and saying so is the honest
    answer for a reconciliation Entitle acts on.
    """
    try:
        groups = int(value)
    except (TypeError, ValueError):
        return ""
    for option in ROLE_OPTIONS:
        if groups_for(option["code"]) == groups:
            return option["code"]
    return ""


def asset_role_options(catalogue=None) -> list:
    """``role_options`` for ``get_assets``.

    ``catalogue`` is FUXA's own ``GET /api/roles`` output, passed when the instance
    runs in named-role mode. It is used only to mark an option AVAILABLE or not — the
    codes stay ours, because Entitle stores a role_code on the grant and a catalogue
    that is edited between grant and revoke would otherwise orphan it.

    ``available: False`` rather than omitting the option: Entitle greys out an
    unavailable role, and drops an asset whose options all vanished — so a
    half-configured HMI should show a disabled role, not disappear mid-POV.
    """
    names = {_lower(entry.get("name")) for entry in (catalogue or [])
             if isinstance(entry, dict)}
    out = []
    for option in ROLE_OPTIONS:
        out.append({
            "code": option["code"],
            "display_name": option["display_name"],
            # With no catalogue we are in bitmask mode, where every published code is
            # always grantable. With one, a code the plant has no role for is shown
            # and disabled rather than silently granted as a bitmask.
            "available": True if not names else option["code"] in names,
            "permissions": list(option["permissions"]),
        })
    return out


# ── Accounts ─────────────────────────────────────────────────────────────────

def ephemeral_username(identity: str, token: str) -> str:
    """A traceable, collision-resistant account name for one grant."""
    slug = re.sub(r"[^a-z0-9]", "-", _lower(identity)).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:32] or "req"
    suffix = re.sub(r"[^a-z0-9]", "", _lower(token))[:12] or "x"
    return f"{EPHEMERAL_PREFIX}{slug}-{suffix}"[:64]


def is_ephemeral_username(username: str) -> bool:
    """Whether this account was minted by us.

    The ONLY thing standing between ``delete_actor`` and the operator's own ``admin``
    account, so it is checked on the name that arrives in the REQUEST, before any
    lookup. It is also what keeps real HMI users out of ``get_actors``.
    """
    return _lower(username).startswith(EPHEMERAL_PREFIX)


def validate_username(username: str) -> str:
    name = _lower(username)
    if not _USERNAME_RE.match(name):
        raise FuxaRuleError(
            f"unsafe FUXA username {username!r} - must match {_USERNAME_RE.pattern}")
    return name


def ephemeral_users(users) -> list:
    """Only the accounts this adapter minted, from a ``GET /api/users`` response."""
    return [user for user in (users or [])
            if isinstance(user, dict) and is_ephemeral_username(user.get("username"))]


def match_user(users, username: str) -> dict:
    """The user ``username`` refers to, case-insensitively, or ``{}``.

    FUXA's username is the table's PRIMARY KEY and its own UI does not fold case, so
    a case-sensitive match here would let "Alice" and "alice" both exist — and a
    delete would then remove neither.
    """
    wanted = _lower(username)
    for user in (users or []):
        if isinstance(user, dict) and _lower(user.get("username")) == wanted:
            return dict(user)
    return {}


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A password FUXA will accept and a human can retype off a screen."""
    size = max(12, int(length))
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(size))


# ── The `info` column ────────────────────────────────────────────────────────

def user_info(roles=None, existing: str = "") -> str:
    """The ``info`` column's JSON string.

    ⚠️  NEVER EMPTY, AND ALWAYS VALID JSON. ``setUsers`` resolves its promise and
    *then* does ``JSON.parse(query.info)``; when that throws, the rejection lands on
    an already-resolved promise and is a no-op — so **the API answers 200 while the
    user is silently absent from FUXA's in-memory map**. That account can sign in and
    then gets 401 on every admin endpoint until a restart repopulates the map, which
    reads as a permissions bug in the adapter. FUXA's own onboarding wizard sends
    ``'{}'``; so does this.

    ``existing`` preserves an account's other keys (``start``, ``languageId``) on a
    rewrite rather than flattening them.
    """
    payload = {}
    if existing:
        try:
            parsed = json.loads(existing)
            if isinstance(parsed, dict):
                payload = parsed
        except (TypeError, ValueError):
            # An unparseable existing value is exactly the state described above;
            # replacing it with a valid one is a repair, not a loss.
            payload = {}
    payload.setdefault("start", "")
    payload.setdefault("languageId", "")
    payload["roles"] = [str(role) for role in (roles or [])]
    # Separators without spaces: this is a database column, not a document.
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def user_payload(*, username: str, password: str, role_code: str,
                 fullname: str = "", roles=None, existing_info: str = "") -> dict:
    """The body of ``POST /api/users``, ready to be wrapped in ``params``.

    ⚠️  A SINGLE OBJECT, never a list — FUXA's own OpenAPI declares ``params`` as an
    array and is wrong: ``setUsers`` reads ``query.username`` off the value directly,
    so a list is rejected with a bare 400.

    ``password`` is sent in PLAINTEXT, which is correct: the server bcrypts it
    (``usrstorage.setUser``), and nothing in FUXA's client hashes it first. Omitting
    it on a NEW account inserts a NULL password that then fails sign-in, so it is
    required here rather than optional.
    """
    name = validate_username(username)
    if not _norm(password):
        raise FuxaRuleError(
            "refusing to create a FUXA account with no password: the row would be "
            "inserted with a NULL password and every sign-in would fail")
    return {
        "username": name,
        "fullname": _norm(fullname) or f"Entitle JIT ({name})",
        "password": _norm(password),
        "groups": groups_for(role_code),
        "info": user_info(roles=roles, existing=existing_info),
    }
