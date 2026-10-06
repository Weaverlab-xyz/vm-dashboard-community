"""Which Entitle integration governs a managed directory, chosen by an operator.

Read-only against Entitle: this lists the tenant's integrations (``GET
/public/v1/integrations``) so the Directories page can offer the ones whose application
looks like the directory's kind, and the operator PINS one. Nothing is created, changed
or matched automatically — Entitle accepts almost anything at registration and surfaces
mistakes much later, so a guessed link would read as a fact it is not.

The base URL is the scheme and host of ``entitle_api_url``, the same normalisation
``entitle_registration_service._provider_endpoint`` uses, because that setting carries
a ``/v1`` path the public API does not want. Region matters: ``api.us.entitle.io`` and
``api.entitle.io`` are different deployments.
"""
import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_PATH = "/public/v1/integrations"
_PER_PAGE = 100
_MAX_PAGES = 5

# Entitle application names (the catalog display name, lowercased) that plausibly
# govern each directory provider. A hint for ordering the list, never a filter: an
# operator can pin any integration.
APPLICATION_HINTS = {
    "entra_id": ("azure active directory", "azure ad", "entra"),
    "okta": ("okta",),
    "pingone": ("pingone", "ping identity", "ping one"),
    "onprem_ad": ("active directory",),
    "aws_managed_ad": ("active directory",),
    "aws_ad_connector": ("active directory",),
    "aws_simple_ad": ("active directory",),
    "gcp_managed_ad": ("active directory",),
    "ldap": ("ldap",),
}

transport = None    # tests set an httpx.MockTransport


class EntitleLinkError(Exception):
    """Entitle could not be read. The message is fixed operator text, never Entitle's."""


def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "")


def configured() -> bool:
    return bool(_base_url() and _cfg("entitle_api_token"))


def _base_url() -> str:
    parts = urlsplit(_cfg("entitle_api_url").strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme == "https" and parts.netloc else ""


def _app_name(item: dict) -> str:
    app = item.get("application")
    if isinstance(app, dict):
        return str(app.get("name") or "")
    return str(app or item.get("applicationName") or "")


def _rows(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("result", "results", "items", "data"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def matches(provider: str, application: str) -> bool:
    app = (application or "").lower()
    return any(h in app for h in APPLICATION_HINTS.get(provider, ()))


async def list_integrations(provider: str = "") -> list:
    """``[{id, name, application, suggested}]``, suggested ones first."""
    import httpx
    base, token = _base_url(), _cfg("entitle_api_token")
    if not base or not token:
        raise EntitleLinkError("Entitle is not configured — set its API URL and token in "
                               "Settings → Integrations → Entitle.")
    out, seen = [], set()
    kwargs = {"timeout": 20}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        for page in range(1, _MAX_PAGES + 1):
            try:
                resp = await client.get(f"{base}{_PATH}",
                                        params={"page": page, "perPage": _PER_PAGE},
                                        headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                logger.warning("Entitle integrations read failed: %s", type(exc).__name__)
                raise EntitleLinkError("Entitle could not be reached.") from exc
            if resp.status_code in (401, 403):
                raise EntitleLinkError("Entitle refused the API token (check it, and that "
                                       "the API URL is your tenant's region).")
            if resp.status_code >= 400:
                logger.warning("Entitle integrations read: HTTP %s", resp.status_code)
                raise EntitleLinkError(f"Entitle answered HTTP {resp.status_code}.")
            try:
                rows = _rows(resp.json())
            except ValueError as exc:
                raise EntitleLinkError("Entitle's answer was not JSON.") from exc
            for item in rows:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                iid = str(item["id"])
                if iid in seen:
                    continue
                seen.add(iid)
                app = _app_name(item)
                out.append({"id": iid, "name": str(item.get("name") or ""),
                            "application": app, "suggested": matches(provider, app)})
            if len(rows) < _PER_PAGE:
                break
    out.sort(key=lambda r: (not r["suggested"], r["name"].lower()))
    return out

