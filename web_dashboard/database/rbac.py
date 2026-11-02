"""The permission-map rules the User model and the login path share.

Three pieces, kept together because they have to agree about one thing: an EMPTY map is
UNRESTRICTED (``api/auth.has_permission``), so every "nothing" answer here is the
non-empty ``_ROLE_UNRESOLVED`` sentinel, never ``{}``. Pure functions on dicts: no engine,
no models. ``web_dashboard.database`` re-exports every name here.
"""

# What `effective_permissions_dict` returns for a principal that HOLDS a role but whose
# materialised copy of that role's map is missing (`role_id` set, `role_permissions` NULL).
#
# It has to be non-empty, and that is the whole point. `api/auth.has_permission` treats an
# empty map as UNRESTRICTED -- so returning `{}` for an unresolvable role would promote that
# user to every permission in the dashboard rather than refusing them. A map with one key
# nobody can enforce takes the strict-allowlist branch instead and grants exactly nothing.
#
# The key is deliberately not a real scope: `tests/test_rbac_roles.py` asserts it is absent
# from `PERMISSION_SCOPE_LEVELS`, so it can never grant even if some route were gated on it.
# `api/auth.me` strips it from its response -- cosmetic, but an unexplained key in a JSON
# body is a support call.
#
# Read `role_id` and never a relationship when deciding this: `api/mcp_server._validate_pat`
# closes its session and keeps the User in a ContextVar for the whole MCP session, so that
# instance is DETACHED. A loaded column still reads there; an unloaded relationship raises,
# and the obvious `except: return {}` around it is the fail-open this constant exists to
# make unwritable.
_ROLE_UNRESOLVED = {"__role_unresolved__": []}


def merge_permission_maps(*maps) -> dict:
    """Union permission maps left to right. THE definition of how sources combine.

    Three call sites had their own copy of this before a role made it four: this property
    below, and twice inside `api/auth._complete_oauth_login` (a matched group's
    `default_permissions`, and now the role that group confers). They agreed, which is the
    only reason nobody noticed -- and a permission-merge rule that exists four times is one
    that will eventually be four different rules, silently, in whichever direction the copy
    someone edited happens to point.

    Three kinds of value, and the asymmetry is deliberate:

      ``is_admin``   OR'd. Any source granting admin grants admin.
      a list        set-union, then sorted -- so the result is order-independent and
                    comparable, which is what lets `reconcile` detect drift by equality.
      anything else last write wins. Not a real case today; kept so an unexpected value
                    survives a merge instead of vanishing, because a key that disappears
                    here is a grant nobody can see to remove.

    Earlier arguments are the lower-precedence ones for that last case. Callers pass the
    role first, then the admin baseline, so a hand edit outranks a stored copy.
    """
    out: dict = {}
    for src in maps:
        for key, val in (src or {}).items():
            if key == "is_admin":
                out[key] = out.get(key, False) or bool(val)
            elif isinstance(val, list):
                existing = out.get(key)
                if isinstance(existing, list):
                    out[key] = sorted(set(existing) | set(val))
                else:
                    out[key] = sorted(set(val))
            else:
                out[key] = val
    return out


def narrow_to_token_scope(perms: dict, token_scope) -> dict:
    """Intersect a permission map with the ``scope`` an OAuth access token was issued for.

    ``token_scope`` is None for every credential that carries no scope -- a human's login
    JWT, a PAT, a workload token requested without one -- and then this is the identity.
    Otherwise it is ``{scope: [levels]}`` from ``services/service_accounts.parse_scope``
    and the result is the per-scope INTERSECTION: a token can only ever narrow what its
    principal holds, never widen it.

    ``is_admin`` never survives, and an intersection that leaves nothing at all answers
    ``_ROLE_UNRESOLVED`` rather than ``{}`` -- the empty map is UNRESTRICTED, and a token
    that asked for less must not be read as one that asked for everything.
    """
    if token_scope is None:
        return perms
    out = {}
    for key, val in (perms or {}).items():
        if key == "is_admin" or not isinstance(val, list):
            continue
        allowed = token_scope.get(key, [])
        out[key] = [lvl for lvl in sorted(val) if lvl in allowed]
    return out or dict(_ROLE_UNRESOLVED)
