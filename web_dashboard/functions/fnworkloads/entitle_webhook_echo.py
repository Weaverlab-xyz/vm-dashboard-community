"""Entitle REST-integration contract simulator.

Entitle's REST integration is **outbound from Entitle to the target system**: it
POSTs Give Access / Revoke Access and expects a synchronous response. This workload
implements that contract and does nothing else — so the whole path (network, front
door, shared secret, timeouts, retries, error handling) can be wired up and proven
BEFORE any real grant logic exists, and re-run afterwards as a regression check.

Every real Phase 2 workload implements this same shape; this is the reference.

⚠️  SCHEMA NOT YET CONFIRMED against a live tenant. Entitle's exact field names for
    the Give/Revoke payload are an open item (see docs/design/cloud-functions.md
    §7), so ``_normalize`` accepts every plausible spelling and reports which key it
    actually matched under ``matched_keys``. Point a real integration at this
    workload, read ``received`` out of the response, and THEN pin the schema. This
    mirrors the ⚠️ discipline in entitle_registration_service for unconfirmed
    vendor schema.

Failure injection, for testing Entitle's retry/alerting behaviour:

    ?fail=401     unauthorized
    ?fail=403     forbidden
    ?fail=404     resource not found
    ?fail=500     server error
    ?fail=slow    respond after ~5s (inside most timeouts)
    ?fail=timeout sleep past any sane timeout (~25s)

Stdlib only.
"""
import time

from fnruntime.contract import Context, Request, Response

NAME = "entitle_webhook_echo"
DESCRIPTION = "Validates the Entitle Give/Revoke REST contract without granting anything."

# Candidate spellings, most-likely first. Whichever is present wins.
_ACTION_KEYS = ("action", "operation", "type", "eventType", "event_type", "method")
_USER_KEYS = ("userEmail", "user_email", "email", "user", "principal",
              "targetUser", "target_user", "actor")
_RESOURCE_KEYS = ("resource", "resourceName", "resource_name", "target",
                  "integration", "resourceId", "resource_id")
_ROLE_KEYS = ("role", "roleName", "role_name", "permission", "entitlement", "bundle")
_DURATION_KEYS = ("duration", "durationSeconds", "duration_seconds", "ttl",
                  "ttlSeconds", "ttl_seconds", "expiresIn", "expires_in")
_REQUEST_KEYS = ("requestId", "request_id", "accessRequestId", "id")

# Values that mean grant vs revoke, lower-cased.
_GRANT_WORDS = ("give", "grant", "giveaccess", "give_access", "add", "create", "provision")
_REVOKE_WORDS = ("revoke", "remove", "revokeaccess", "revoke_access", "delete", "deprovision")


def _first(payload: dict, keys) -> tuple:
    """``(value, matched_key)`` for the first present, non-empty key."""
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key], key
    return None, ""


def _classify(raw_action) -> str:
    text = str(raw_action or "").strip().lower().replace("-", "").replace(" ", "")
    if any(word in text for word in _REVOKE_WORDS):
        return "revoke"
    if any(word in text for word in _GRANT_WORDS):
        return "grant"
    return "unknown"


def _normalize(payload: dict, path: str) -> dict:
    """Best-effort mapping onto the shape every real workload consumes."""
    raw_action, action_key = _first(payload, _ACTION_KEYS)
    action = _classify(raw_action)
    # Entitle may express the verb in the PATH rather than the body
    # (…/give-access vs …/revoke-access), so fall back to that.
    if action == "unknown":
        action = _classify(path)
        if action != "unknown":
            action_key = "<path>"

    user, user_key = _first(payload, _USER_KEYS)
    resource, resource_key = _first(payload, _RESOURCE_KEYS)
    role, role_key = _first(payload, _ROLE_KEYS)
    duration, duration_key = _first(payload, _DURATION_KEYS)
    request_id, request_id_key = _first(payload, _REQUEST_KEYS)

    try:
        duration_seconds = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None

    # A nested user object ({"user": {"email": ...}}) is common; flatten it.
    if isinstance(user, dict):
        user = user.get("email") or user.get("id") or user.get("name") or ""

    return {
        "action": action,
        "user_email": str(user) if user is not None else "",
        "resource": str(resource) if resource is not None else "",
        "role": str(role) if role is not None else "",
        "duration_seconds": duration_seconds,
        "entitle_request_id": str(request_id) if request_id is not None else "",
        "matched_keys": {k: v for k, v in {
            "action": action_key, "user_email": user_key, "resource": resource_key,
            "role": role_key, "duration_seconds": duration_key,
            "entitle_request_id": request_id_key,
        }.items() if v},
    }


def _injected_failure(req: Request):
    """The ``?fail=`` response, or None."""
    mode = str(req.query.get("fail") or "").strip().lower()
    if not mode:
        return None
    if mode == "slow":
        time.sleep(5)
        return None
    if mode == "timeout":
        time.sleep(25)
        return None
    if mode in ("401", "unauthorized"):
        return Response(401, {"error": "unauthorized", "injected": True})
    if mode in ("403", "forbidden"):
        return Response(403, {"error": "forbidden", "injected": True})
    if mode in ("404", "notfound", "not_found"):
        return Response(404, {"error": "resource not found", "injected": True})
    if mode in ("500", "error"):
        return Response(500, {"error": "internal error", "injected": True})
    return Response(400, {"error": f"unknown fail mode: {mode}", "injected": True})


def handle(req: Request, ctx: Context) -> Response:
    injected = _injected_failure(req)
    if injected is not None:
        return injected

    payload = req.json()
    normalized = _normalize(payload, req.path)

    problems = []
    if normalized["action"] == "unknown":
        problems.append(
            "could not determine grant vs revoke — no recognised action key "
            f"(looked for: {', '.join(_ACTION_KEYS)}) and the path did not say")
    if not normalized["user_email"]:
        problems.append(
            f"no user identified (looked for: {', '.join(_USER_KEYS)})")
    if not normalized["resource"]:
        problems.append(
            f"no resource identified (looked for: {', '.join(_RESOURCE_KEYS)})")

    # 200 even when fields are missing: this workload's job is to REPORT what the
    # payload looked like, and a 4xx would make Entitle retry and hide the answer.
    return Response(200, {
        "ok": not problems,
        "workload": NAME,
        "request_id": ctx.request_id,
        "received": normalized,
        "problems": problems,
        "raw_keys": sorted(payload.keys()),
        "duration_ms": ctx.elapsed_ms(),
    })
