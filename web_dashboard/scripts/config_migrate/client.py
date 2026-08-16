"""Minimal HTTP client for the two dashboard instances.

Deliberately stdlib-only (``urllib.request``). This runs from an operator's
shell, not from inside the app image — a fresh WSL box or a Windows host has no
virtualenv and no ``requests``, and "install these packages first" is exactly
the friction that makes a migration tool go unused. ``CONTRIBUTING.md`` also
asks that dependencies not be added without a clear reason, and there isn't one
for four requests and a form post.

The auth dance is copied from ``scripts/sandbox/Linux/onboard-sandbox.sh`` so
both tools fail the same way against the same server:

    GET  /api/setup/status                  → is setup complete?
    POST /api/auth/login   (form-urlencoded) → access_token
    POST /api/setup/import (Bearer)          → merge config

One thing that trips people up: a ``vmcli_`` personal access token does **not**
work here. ``api/setup._require_admin`` decodes the JWT itself rather than going
through the ``get_current_user`` dependency that understands PATs, so the
migration needs a real login.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 30


class ApiError(RuntimeError):
    """An HTTP call failed. Carries the server's own message where there is one."""

    def __init__(self, method: str, url: str, status: int | None, detail: str):
        self.status = status
        self.detail = detail
        where = f"{method} {url}"
        super().__init__(f"{where} → {status or 'no response'}: {detail}")


class Client:
    """One dashboard instance, addressed by base URL."""

    def __init__(self, base_url: str, *, token: str = "", ca_bundle: str = "",
                 insecure: bool = False, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ctx = self._build_ssl_context(ca_bundle, insecure)

    # ── transport ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_ssl_context(ca_bundle: str, insecure: bool) -> ssl.SSLContext:
        """TLS context, with the two escape hatches a corporate network needs.

        A TLS-inspecting proxy re-signs everything with its own root, so the
        default trust store rejects a perfectly healthy dashboard. ``--ca-bundle``
        is the right fix; ``--insecure`` exists because sometimes you are
        debugging at 5pm, and it prints a warning every time it is used.
        """
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif ca_bundle:
            ctx.load_verify_locations(cafile=ca_bundle)
        return ctx

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str = "", auth: bool = True) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        if auth and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                raw = resp.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            raise ApiError(method, url, exc.code, _detail_of(exc)) from exc
        except urllib.error.URLError as exc:
            raise ApiError(method, url, None, str(exc.reason)) from exc
        except OSError as exc:  # socket timeouts, DNS, TLS handshake
            raise ApiError(method, url, None, str(exc)) from exc
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(method, url, 200, f"expected JSON, got {raw[:120]!r}") from None

    def _get(self, path: str, *, auth: bool = True) -> dict:
        return self._request("GET", path, auth=auth)

    def _post_json(self, path: str, payload: dict, *, auth: bool = True) -> dict:
        return self._request("POST", path, body=json.dumps(payload).encode("utf-8"),
                             content_type="application/json", auth=auth)

    # ── auth ─────────────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> str:
        """Exchange admin credentials for a JWT and remember it on this client."""
        form = urllib.parse.urlencode({"username": username, "password": password})
        data = self._request("POST", "/api/auth/login", body=form.encode("utf-8"),
                             content_type="application/x-www-form-urlencoded",
                             auth=False)
        token = data.get("access_token") or ""
        if not token:
            raise ApiError("POST", f"{self.base_url}/api/auth/login", 200,
                           "login succeeded but returned no access_token")
        self.token = token
        return token

    # ── setup / config ───────────────────────────────────────────────────────

    def setup_status(self) -> dict:
        """``{"complete": bool, "configured_keys": [...]}``. Needs no auth."""
        return self._get("/api/setup/status", auth=False)

    def get_config(self) -> dict:
        """The instance's global config, decrypted, vault references unresolved.

        ``get_all_public`` reads ``config_service``'s in-memory cache directly,
        and that cache holds ``_decrypt(row.value)`` without ever calling
        ``_resolve_external`` — so an ``azure_kv://…`` value comes back as the
        reference string rather than the secret behind it. That is the desirable
        behaviour for a migration: the pointer moves, the secret stays put.

        The four keys in ``config_service._SECRET_KEYS`` come back as bullets.
        """
        return self._get("/api/setup/config")

    def import_config(self, config: dict, *,
                      admin_username: str = "", admin_password: str = "") -> dict:
        """``POST /api/setup/import`` — verbatim merge into ``app_config``.

        On a virgin target the admin credentials create the account and mark
        setup complete; on a configured one they are ignored and the bearer
        token is what authorises the merge.
        """
        payload: dict = {"config": config}
        if admin_username:
            payload["admin_username"] = admin_username
            payload["admin_password"] = admin_password
        return self._post_json("/api/setup/import", payload,
                               auth=bool(self.token))

    # ── notification endpoints ───────────────────────────────────────────────

    def list_endpoints(self) -> list[dict]:
        """Existing endpoints. URLs and secrets are *not* included by design —
        the response carries a host hint and ``has_secret`` only, so this is
        useful for matching by name, not for reading values back."""
        return (self._get("/api/notifications/endpoints") or {}).get("endpoints", [])

    def create_endpoint(self, endpoint: dict) -> dict:
        return self._post_json("/api/notifications/endpoints", endpoint)

    def patch_endpoint(self, endpoint_id: str, fields: dict) -> dict:
        return self._request("PATCH", f"/api/notifications/endpoints/{endpoint_id}",
                             body=json.dumps(fields).encode("utf-8"),
                             content_type="application/json")


def _detail_of(exc: urllib.error.HTTPError) -> str:
    """FastAPI's ``{"detail": ...}`` when present, else the raw body.

    Worth the effort: the useful errors here are all server-authored — "Setup is
    already complete", "config must be a non-empty object", "Not authenticated".
    """
    try:
        body = exc.read().decode("utf-8")
    except Exception:  # noqa: BLE001
        return exc.reason or ""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:300] or (exc.reason or "")
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if isinstance(detail, list):  # pydantic validation errors
        return "; ".join(str(d.get("msg", d)) for d in detail)
    return str(detail or body[:300])
