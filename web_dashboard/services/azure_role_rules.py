"""Rules for granting Azure RBAC roles to a machine identity — pure, stdlib-only.

Why this integration exists: AWS machine identity works because Entitle attaches an
IAM policy to the dashboard's IAM **user**. Azure has no equivalent — a machine
identity there is an *application* (a service principal), and an application cannot
be the account privileges are requested for. So Entitle cannot grant it through the
native Azure integration, and a REST adapter is the only route.

The adapter grants a role assignment to an EXISTING service principal. It is
therefore not ephemeral: there is no create_actor/delete_actor, and Entitle owns the
expiry — plain Azure role assignments carry no TTL, so a grant lasts until Entitle
calls revoke_access.

**An adapter that can grant any role at any scope is a privilege-escalation
primitive.** Everything below exists to stop that: both the scopes and the roles it
may touch are allowlisted by the operator, and the three roles that let a grantee
grant themselves more are refused outright regardless of the allowlist.

Pure — no HTTP, no SDK, no credentials — so the guards are testable with nothing
installed. Vendored into the function's zip verbatim.
"""
import re
import uuid

# Well-known Azure built-in role definition GUIDs. Stable across every tenant.
BUILTIN_ROLES = {
    "reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    "contributor": "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "owner": "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
    "user access administrator": "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9",
    "role based access control administrator": "f58310d9-a9f6-439a-9e8d-f62e7b41a168",
}

# Roles that let whoever holds them grant themselves anything else. Refused even if
# an operator allowlists them: a just-in-time grant of Owner is not just-in-time,
# because the grantee can make it permanent before it expires. If someone genuinely
# needs to hand out Owner, that is a decision for a human and a change ticket, not
# an automated integration with a shared secret in front of it.
ESCALATION_ROLES = frozenset({
    BUILTIN_ROLES["owner"],
    BUILTIN_ROLES["user access administrator"],
    BUILTIN_ROLES["role based access control administrator"],
})

# A role assignment name must be a GUID, and Azure treats the PUT as an upsert keyed
# on it. Deriving it deterministically from (scope, principal, role) means a revoke
# can address the assignment directly instead of listing and searching — and a
# repeated grant is idempotent for free.
_ASSIGNMENT_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Subscription, or a resource group / resource under one. Anchored so a tenant-root
# scope ("/" or "/providers/Microsoft.Management/managementGroups/...") can never
# match — granting at management-group scope is far beyond anything this is for.
_SCOPE_RE = re.compile(
    r"^/subscriptions/[0-9a-f-]{36}(/resourceGroups/[A-Za-z0-9._()-]{1,90}"
    r"(/providers/[A-Za-z0-9./_-]+)?)?$", re.IGNORECASE)


class AzureRoleRuleError(Exception):
    """Raised on a scope, role or principal we refuse to act on."""


def _norm(value) -> str:
    return str(value or "").strip()


def parse_list(raw) -> list:
    """A CSV or list config value, as a clean list."""
    if isinstance(raw, (list, tuple)):
        return [_norm(v) for v in raw if _norm(v)]
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def resolve_role(role_code: str) -> str:
    """A role definition GUID from a name or a GUID.

    Accepts the friendly name so an Entitle ``role_code`` can read "Reader" rather
    than a GUID nobody recognises in an approval request.
    """
    value = _norm(role_code).lower()
    if not value:
        raise AzureRoleRuleError("role_code is required")
    if value in BUILTIN_ROLES:
        return BUILTIN_ROLES[value]
    if _GUID_RE.match(value):
        return value
    raise AzureRoleRuleError(
        f"unknown role {role_code!r} — use a role definition GUID or one of: "
        f"{', '.join(sorted(BUILTIN_ROLES))}")


def check_role(role_code: str, allowed) -> str:
    """The role GUID, if this adapter is permitted to grant it.

    Two gates, and the escalation one is NOT overridable by configuration.
    """
    role_id = resolve_role(role_code)
    if role_id in ESCALATION_ROLES:
        raise AzureRoleRuleError(
            f"refusing to grant {role_code!r}: it confers the ability to grant "
            "further access, so a time-boxed grant of it is not time-boxed")
    permitted = {resolve_role(entry) for entry in parse_list(allowed)}
    if not permitted:
        raise AzureRoleRuleError(
            "no grantable roles are configured (FN_AZURE_ROLES) — this adapter "
            "will not grant a role nobody allowlisted")
    if role_id not in permitted:
        raise AzureRoleRuleError(f"role {role_code!r} is not in this adapter's allowlist")
    return role_id


def check_scope(scope: str, allowed) -> str:
    """The scope, if this adapter is permitted to grant at it.

    Prefix matching is deliberate and deliberately anchored on a path separator: an
    allowlisted ``/subscriptions/X/resourceGroups/prod`` must not also permit
    ``/subscriptions/X/resourceGroups/prod-secrets``.
    """
    value = _norm(scope)
    if not _SCOPE_RE.match(value):
        raise AzureRoleRuleError(
            f"unsupported scope {scope!r} — expected a subscription, resource group "
            "or resource path (management-group and tenant-root scopes are refused)")
    permitted = [_norm(entry).rstrip("/") for entry in parse_list(allowed)]
    if not permitted:
        raise AzureRoleRuleError(
            "no grantable scopes are configured (FN_AZURE_SCOPES) — this adapter "
            "will not grant at a scope nobody allowlisted")
    for entry in permitted:
        if value.lower() == entry.lower() or value.lower().startswith(entry.lower() + "/"):
            return value
    raise AzureRoleRuleError(f"scope {scope!r} is not in this adapter's allowlist")


def check_principal(principal_id: str) -> str:
    """The service principal's OBJECT id.

    Must be a GUID: Azure accepts an application id here and silently creates an
    assignment that grants nothing, because the assignment binds to the object id.
    That failure is invisible until someone wonders why their access does not work.
    """
    value = _norm(principal_id).lower()
    if not _GUID_RE.match(value):
        raise AzureRoleRuleError(
            f"principal {principal_id!r} is not a GUID — this must be the service "
            "principal's OBJECT id, not its application (client) id")
    return value


def assignment_name(scope: str, principal_id: str, role_id: str) -> str:
    """The deterministic GUID naming this assignment.

    Same inputs always give the same name, so a grant is an idempotent upsert and a
    revoke can DELETE by name without listing anything.
    """
    key = f"{_norm(scope).lower()}|{_norm(principal_id).lower()}|{_norm(role_id).lower()}"
    return str(uuid.uuid5(_ASSIGNMENT_NAMESPACE, key))


def role_definition_id(subscription_id: str, role_id: str) -> str:
    return (f"/subscriptions/{_norm(subscription_id)}/providers/"
            f"Microsoft.Authorization/roleDefinitions/{_norm(role_id)}")


def assignment_path(scope: str, name: str) -> str:
    return (f"{_norm(scope).rstrip('/')}/providers/Microsoft.Authorization/"
            f"roleAssignments/{_norm(name)}")


def asset_for(scope: str, roles) -> dict:
    """A scope, as an Entitle asset whose roles are the ones allowlisted on it."""
    grantable = []
    for entry in parse_list(roles):
        try:
            role_id = resolve_role(entry)
        except AzureRoleRuleError:
            continue
        if role_id in ESCALATION_ROLES:
            continue
        grantable.append({
            "code": _norm(entry),
            "display_name": _norm(entry).title(),
            "available": True,
            "permissions": [role_id],
        })
    return {
        "identifier": f"azure:scope:{_norm(scope)}",
        "name": _norm(scope).rsplit("/", 1)[-1] or _norm(scope),
        "type": "azure_scope",
        "role_options": grantable,
    }


def scope_from_asset(payload: dict) -> str:
    identifier = _norm((payload.get("asset") or {}).get("identifier"))
    prefix = "azure:scope:"
    return identifier[len(prefix):] if identifier.startswith(prefix) else ""
