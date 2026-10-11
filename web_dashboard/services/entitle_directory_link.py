"""Which Entitle integration governs a managed directory, chosen by an operator.

Read-only against Entitle: this lists the tenant's integrations (``GET
/public/v1/integrations``) so the Directories page can offer the ones whose application
looks like the directory's kind, and the operator PINS one. Nothing is created, changed
or matched automatically — Entitle accepts almost anything at registration and surfaces
mistakes much later, so a guessed link would read as a fact it is not.

The HTTP side (base URL, paging, fixed error text) is ``entitle_public_api``'s; this
module passes it its own ``_cfg`` and ``transport`` so the seams its tests use still hold.
"""
import logging

from . import entitle_public_api as api

logger = logging.getLogger(__name__)

_PATH = "/public/v1/integrations"
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
    return api.configured(_cfg)


def _app_name(item: dict) -> str:
    app = item.get("application")
    if isinstance(app, dict):
        return str(app.get("name") or "")
    return str(app or item.get("applicationName") or "")


def matches(provider: str, application: str) -> bool:
    app = (application or "").lower()
    return any(h in app for h in APPLICATION_HINTS.get(provider, ()))


async def list_integrations(provider: str = "") -> list:
    """``[{id, name, application, suggested}]``, suggested ones first."""
    try:
        rows = await api.get_paged(_PATH, what="integrations", cfg=_cfg, tp=transport,
                                   max_pages=_MAX_PAGES)
    except api.EntitleApiError as exc:
        raise EntitleLinkError(str(exc)) from exc
    out, seen = [], set()
    for item in rows:
        if not item.get("id"):
            continue
        iid = str(item["id"])
        if iid in seen:
            continue
        seen.add(iid)
        app = _app_name(item)
        out.append({"id": iid, "name": str(item.get("name") or ""),
                    "application": app, "suggested": matches(provider, app)})
    out.sort(key=lambda r: (not r["suggested"], r["name"].lower()))
    return out
