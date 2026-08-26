"""HTTP client for the Skytap REST API (v2).

Deliberately **not** a Terraform provider. The `skytap/skytap` provider's last release was
v0.15.1 (Nov 2022) and its own documentation says it "doesn't enumerate the resources
contained within that template, including VMs and networks" — which is the one thing a POV
environment needs, since a POV *is* a template instantiated whole. So this talks to the
REST API directly, the way the Portainer and Rancher integrations do.

Everything goes through one :meth:`SkytapClient.request`, because three of Skytap's
behaviours are easy to get wrong once and then get wrong everywhere:

**423 Locked is normal.** It is not an error condition — it is how Skytap says "this
resource is busy, or the account is being rate-limited", and it carries a ``Retry-After``.
Environments also expose a ``rate_limited`` boolean of their own. A client that treats 423
as a failure reports a broken integration during ordinary concurrent use.

**Every GET must carry ``keep_idle=true``.** Without it, *reading* an environment resets
its idle timer. A dashboard that polls environments would therefore hold every one of them
awake forever and quietly defeat ``suspend_on_idle`` — the single biggest lever on Skytap
spend. The failure has no symptom except the bill, which is exactly why it belongs in the
one function nobody can forget to call.

**Pagination is count/offset, not a cursor.** Collections cap out server-side, so a naive
GET silently returns a first page and callers treat it as the whole world.

Pure transport: no database, no config_service, no dashboard imports. Credentials arrive as
a :class:`SkytapCreds`. That keeps it unit-testable against `httpx.MockTransport` with no
app, which is how the retry and keep_idle behaviours are actually pinned.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cloud.skytap.com"

# Skytap answers "busy or rate-limited" with 423 and a Retry-After. 429 is the plain
# rate-limit case. Both are transient and both are retried.
_RETRY_STATUSES = (423, 429)

# Bounds on that retry. A POV provision is a long job, but a *single* call that has been
# refused six times is telling us something a seventh will not fix, and a job that hangs
# for an hour inside one HTTP call is worse than one that fails with a clear message.
_MAX_RETRIES = 6
_MAX_RETRY_WAIT_S = 30.0     # cap on any single Retry-After we will honour
_DEFAULT_RETRY_WAIT_S = 5.0  # when Retry-After is absent or unparseable

# Server-side page size for collection reads.
_PAGE_SIZE = 100
# Refuse to loop forever if the server keeps returning full pages.
_MAX_PAGES = 100


class SkytapError(Exception):
    """A Skytap API call failed."""


class SkytapAuthError(SkytapError):
    """Credentials were rejected (401/403). Worth its own type: this is the one failure an
    operator fixes in Settings rather than by retrying."""


@dataclass(frozen=True)
class SkytapCreds:
    """Skytap authenticates with HTTP Basic — username plus an API token, not a password.

    Frozen, with a redacting ``__repr__``, following
    ``hypervisor_connection_service.Connection``: these objects get passed into log-heavy
    call paths and a stray f-string should not be able to leak the token.
    """
    username: str
    api_token: str
    base_url: str = DEFAULT_BASE_URL

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"SkytapCreds(username={self.username!r}, "
                f"api_token=<redacted>, base_url={self.base_url!r})")

    @property
    def auth_header(self) -> str:
        raw = f"{self.username}:{self.api_token}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def valid(self) -> bool:
        return bool(self.username and self.api_token and self.base_url)


def _retry_after_seconds(response: httpx.Response) -> float:
    """Seconds to wait, from the Retry-After header, clamped.

    Skytap sends an integer count of seconds. The spec also permits an HTTP-date, which we
    do not attempt to parse: falling back to a fixed delay is better than a wrong one, and
    the retry is bounded either way.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        wait = _DEFAULT_RETRY_WAIT_S
    if wait <= 0:
        wait = _DEFAULT_RETRY_WAIT_S
    return min(wait, _MAX_RETRY_WAIT_S)


class SkytapClient:
    """One client per set of credentials. Safe to construct per call — it opens and closes
    its own ``httpx.AsyncClient`` unless one is injected.

    ``transport`` exists for tests: pass an ``httpx.MockTransport`` and every behaviour
    below is exercisable with no network and no app.
    """

    def __init__(self, creds: SkytapCreds, *, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None,
                 sleep=asyncio.sleep):
        if not creds.valid():
            raise SkytapError(
                "Skytap is not configured — set the API URL, username and API token in "
                "Settings → Integrations → Skytap.")
        self._creds = creds
        self._timeout = timeout
        self._transport = transport
        # Injectable so tests do not spend real seconds proving the retry waits.
        self._sleep = sleep

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._creds.base_url.rstrip("/"),
            timeout=self._timeout,
            transport=self._transport,
            headers={
                "Authorization": self._creds.auth_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def request(self, method: str, path: str, *,
                      params: dict | None = None,
                      json: dict | list | None = None,
                      client: httpx.AsyncClient | None = None):
        """One request, with auth, the keep_idle guarantee and the 423/429 retry.

        Returns the decoded JSON body, or ``None`` for an empty one (DELETE answers 204).
        """
        params = dict(params or {})
        if method.upper() == "GET":
            # The whole reason this lives here. See the module docstring: a GET without it
            # resets the environment's idle timer, so polling silently defeats
            # suspend_on_idle and the only symptom is the invoice.
            params.setdefault("keep_idle", "true")

        own_client = client is None
        cli = client or self._client()
        try:
            last: httpx.Response | None = None
            for attempt in range(_MAX_RETRIES + 1):
                resp = await cli.request(method, path, params=params, json=json)
                last = resp
                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    wait = _retry_after_seconds(resp)
                    logger.info(
                        "Skytap %s %s -> %s (busy/rate-limited); retrying in %.1fs "
                        "(attempt %d/%d)",
                        method, path, resp.status_code, wait, attempt + 1, _MAX_RETRIES)
                    await self._sleep(wait)
                    continue
                return self._decode(resp, method, path)
            # Only reachable if the loop exhausted its retries.
            return self._decode(last, method, path)  # type: ignore[arg-type]
        finally:
            if own_client:
                await cli.aclose()

    @staticmethod
    def _decode(resp: httpx.Response, method: str, path: str):
        if resp.status_code in (401, 403):
            raise SkytapAuthError(
                f"Skytap rejected the credentials for {method} {path} "
                f"({resp.status_code}). Check the username and API token in Settings — "
                f"Skytap uses an API token, not your account password.")
        if resp.status_code in _RETRY_STATUSES:
            raise SkytapError(
                f"Skytap is still busy after {_MAX_RETRIES} retries on {method} {path} "
                f"({resp.status_code}). The environment or the account is rate-limited; "
                f"try again shortly.")
        if resp.status_code >= 400:
            raise SkytapError(
                f"Skytap {method} {path} failed ({resp.status_code}): {resp.text[:400]}")
        if resp.status_code == 204 or not (resp.content or b"").strip():
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise SkytapError(
                f"Skytap {method} {path} returned a non-JSON body: "
                f"{resp.text[:200]}") from exc

    async def get(self, path: str, **kw):
        return await self.request("GET", path, **kw)

    async def list(self, path: str, *, params: dict | None = None) -> list:
        """GET a collection, following count/offset pagination to the end.

        Skytap caps collection responses server-side, so a single GET returns a first page
        that looks exactly like a complete answer. Stopping short would mean a POV silently
        not listing the template someone needs.
        """
        out: list = []
        base = dict(params or {})
        async with self._client() as cli:
            for page in range(_MAX_PAGES):
                page_params = dict(base)
                page_params["count"] = _PAGE_SIZE
                page_params["offset"] = page * _PAGE_SIZE
                body = await self.request("GET", path, params=page_params, client=cli)
                if body is None:
                    break
                if not isinstance(body, list):
                    # A single object where a collection was expected: hand it back rather
                    # than silently returning nothing.
                    return [body]
                out.extend(body)
                if len(body) < _PAGE_SIZE:
                    break
            else:
                logger.warning(
                    "Skytap %s: stopped after %d pages (%d items); the collection may be "
                    "truncated", path, _MAX_PAGES, len(out))
        return out
