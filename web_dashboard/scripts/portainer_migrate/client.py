"""Minimal HTTP client for one Portainer CE instance.

Stdlib-only (``urllib.request``), for the same reason
``config_migrate.client`` is: this runs from an operator's shell against a
workstation Portainer, and "pip install these first" is the friction that makes a
migration tool go unused.

Two auth modes, matching what Portainer offers:

    X-API-Key: <pat>                  an access token from My account → Access tokens
    Authorization: Bearer <jwt>       from POST /api/auth (username + password)

A throwaway Portainer restored from a backup has whatever admin the backup
carried, so the username/password mode is usually the one that works there — a
fresh PAT can only be minted after logging in anyway.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 30


class PortainerCliError(RuntimeError):
    """An HTTP call failed. Carries Portainer's own message where there is one."""

    def __init__(self, method: str, url: str, status: int | None, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"{method} {url} -> {status or 'no response'}: {detail}")


class Client:
    """One Portainer instance, addressed by base URL."""

    def __init__(self, base_url: str, *, pat: str = "", ca_bundle: str = "",
                 insecure: bool = False, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.pat = pat
        self.jwt = ""
        self.timeout = timeout
        self._ctx = self._build_ssl_context(ca_bundle, insecure)

    # ── transport ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_ssl_context(ca_bundle: str, insecure: bool) -> ssl.SSLContext:
        """TLS context with the escape hatches this actually needs.

        Portainer's own default cert on :9443 is self-signed, so ``--insecure`` is
        the NORMAL case here rather than a last resort — unlike the dashboard
        client, where it means someone is debugging at 5pm.
        """
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif ca_bundle:
            ctx.load_verify_locations(cafile=ca_bundle)
        return ctx

    def _headers(self, auth: bool) -> dict:
        if not auth:
            return {}
        if self.jwt:
            return {"Authorization": f"Bearer {self.jwt}"}
        if self.pat:
            return {"X-API-Key": self.pat}
        return {}

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str = "", auth: bool = True):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=body, method=method)
        for key, val in self._headers(auth).items():
            req.add_header(key, val)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
                detail = payload.get("message") or payload.get("details") or ""
            except (ValueError, OSError):
                pass
            raise PortainerCliError(method, url, exc.code, detail or exc.reason or "") from exc
        except urllib.error.URLError as exc:
            raise PortainerCliError(method, url, None, str(exc.reason)) from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return raw.decode("utf-8", "replace")

    # ── auth ─────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """``GET /api/system/status`` — also the liveness probe.

        A Portainer whose admin-init window closed with no admin answers this with
        an "administrator initialization timeout" message instead of a status body,
        so the caller can tell "not up yet" from "fenced off for good".
        """
        return self._request("GET", "/api/system/status", auth=False) or {}

    def login(self, username: str, password: str) -> str:
        """``POST /api/auth`` → JWT, which then takes precedence over any PAT."""
        body = json.dumps({"Username": username, "Password": password}).encode()
        data = self._request("POST", "/api/auth", body=body,
                             content_type="application/json", auth=False) or {}
        self.jwt = data.get("jwt") or ""
        if not self.jwt:
            raise PortainerCliError("POST", f"{self.base_url}/api/auth", None,
                                    "no jwt in the response")
        return self.jwt

    # ── reads ────────────────────────────────────────────────────────────────

    def get_list(self, path: str) -> list:
        """A collection endpoint, normalized to a list.

        Portainer answers some collections with ``null`` rather than ``[]`` when
        empty, which would otherwise propagate into the bundle as a null and blow
        up the importer's iteration.
        """
        data = self._request("GET", path)
        return data if isinstance(data, list) else []

    def get_obj(self, path: str) -> dict:
        data = self._request("GET", path)
        return data if isinstance(data, dict) else {}

    def stack_file(self, stack_id: int) -> str:
        """``GET /api/stacks/{id}/file`` → the compose text.

        Separate from the stack listing because Portainer does not inline it; a
        stack without this is just a name and cannot be recreated anywhere.
        """
        data = self.get_obj(f"/api/stacks/{int(stack_id)}/file")
        return data.get("StackFileContent") or ""
