"""Read-only calls to Entitle's public API (``/public/v1/...``), shared by every reader.

``entitle_directory_link`` lists integrations to pin; the virtual-application and
persona-bundle flows list the resources and roles inside one. All of them want the same
four things, and getting any one wrong is silent:

  * **the base URL is the scheme and host of ``entitle_api_url``**. That setting carries
    a ``/v1`` path the public API does not want, and only ``https`` may carry the token.
    Region matters: ``api.us.entitle.io`` and ``api.entitle.io`` are different
    deployments, and the wrong one answers 401 rather than "wrong region";
  * **paging** is ``page`` / ``perPage``, and a short page is the last one;
  * **the list key varies** (``result`` today; ``results`` / ``items`` / ``data`` cost
    nothing to accept);
  * **errors are reported as fixed operator text, never Entitle's own body**, which can
    carry internal detail.

Callers pass their own ``cfg`` and ``transport`` so a module's test seams keep working:
``entitle_directory_link`` hands over its module-level ``_cfg`` / ``transport`` at call
time, which is what its tests monkeypatch.
"""
import logging
import re
from typing import Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PER_PAGE = 100
MAX_PAGES = 5

transport = None    # tests set an httpx.MockTransport


class EntitleApiError(Exception):
    """Entitle could not be read. The message is fixed operator text, never Entitle's."""


def _default_cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "")


def base_url(cfg: Optional[Callable[[str], str]] = None) -> str:
    parts = urlsplit((cfg or _default_cfg)("entitle_api_url").strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme == "https" and parts.netloc else ""


def configured(cfg: Optional[Callable[[str], str]] = None) -> bool:
    cfg = cfg or _default_cfg
    return bool(base_url(cfg) and cfg("entitle_api_token"))


def rows(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("result", "results", "items", "data"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def _client_kwargs(tp) -> dict:
    kwargs = {"timeout": 20}
    tp = tp if tp is not None else transport
    if tp is not None:
        kwargs["transport"] = tp
    return kwargs


def _check(resp, what: str) -> None:
    if resp.status_code in (401, 403):
        raise EntitleApiError("Entitle refused the API token (check it, and that the API "
                              "URL is your tenant's region).")
    if resp.status_code == 404:
        raise EntitleApiError(f"Entitle has no such {what}.")
    if resp.status_code >= 400:
        logger.warning("Entitle %s read: HTTP %s", what, resp.status_code)
        raise EntitleApiError(f"Entitle answered HTTP {resp.status_code}.")


def _creds(cfg) -> tuple:
    cfg = cfg or _default_cfg
    base, token = base_url(cfg), cfg("entitle_api_token")
    if not base or not token:
        raise EntitleApiError("Entitle is not configured — set its API URL and token in "
                              "Settings → Integrations → Entitle.")
    return base, token


async def get_paged(path: str, params: Optional[dict] = None, *, what: str = "list",
                    cfg: Optional[Callable[[str], str]] = None, tp=None,
                    max_pages: int = MAX_PAGES) -> list:
    """Every row of a paged ``GET``, up to ``max_pages`` pages of :data:`PER_PAGE`."""
    import httpx
    base, token = _creds(cfg)
    out: list = []
    async with httpx.AsyncClient(**_client_kwargs(tp)) as client:
        for page in range(1, max_pages + 1):
            try:
                resp = await client.get(
                    f"{base}{path}",
                    params={**(params or {}), "page": page, "perPage": PER_PAGE},
                    headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                logger.warning("Entitle %s read failed: %s", what, type(exc).__name__)
                raise EntitleApiError("Entitle could not be reached.") from exc
            _check(resp, what)
            try:
                batch = rows(resp.json())
            except ValueError as exc:
                raise EntitleApiError("Entitle's answer was not JSON.") from exc
            out.extend(r for r in batch if isinstance(r, dict))
            if len(batch) < PER_PAGE:
                break
    return out


async def get_one(path: str, *, what: str = "item",
                  cfg: Optional[Callable[[str], str]] = None, tp=None) -> dict:
    """One object. Entitle wraps it as ``{"result": {...}}``; a bare object is accepted."""
    import httpx
    base, token = _creds(cfg)
    async with httpx.AsyncClient(**_client_kwargs(tp)) as client:
        try:
            resp = await client.get(f"{base}{path}",
                                    headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            logger.warning("Entitle %s read failed: %s", what, type(exc).__name__)
            raise EntitleApiError("Entitle could not be reached.") from exc
        _check(resp, what)
        try:
            body = resp.json()
        except ValueError as exc:
            raise EntitleApiError("Entitle's answer was not JSON.") from exc
    if isinstance(body, dict) and isinstance(body.get("result"), dict):
        return body["result"]
    return body if isinstance(body, dict) else {}


def _name(obj) -> str:
    return str(obj.get("name") or "") if isinstance(obj, dict) else ""


def role_summary(item: dict) -> dict:
    """``{id, name, external_id, resource_id, resource_name, integration_id,
    integration_name, application, requestable}`` from a role list item or a single role."""
    res = item.get("resource") if isinstance(item.get("resource"), dict) else {}
    integ = res.get("integration") if isinstance(res.get("integration"), dict) else {}
    return {
        "id": str(item.get("id") or ""),
        "name": _name(item),
        "external_id": str(item.get("externalId") or ""),
        "resource_id": str(res.get("id") or item.get("resourceId") or ""),
        "resource_name": _name(res),
        "integration_id": str(integ.get("id") or ""),
        "integration_name": _name(integ),
        "application": _name(integ.get("application")),
        "requestable": item.get("requestable") is not False,
    }


async def list_roles(*, integration_id: str = "", resource_id: str = "", search: str = "",
                     cfg=None, tp=None, max_pages: int = MAX_PAGES) -> list:
    """Roles in one integration (or one resource), as :func:`role_summary` dicts."""
    params = {}
    if integration_id:
        params["integrationId"] = integration_id
    if resource_id:
        params["resourceId"] = resource_id
    if search:
        params["search"] = search
    items = await get_paged("/public/v1/roles", params, what="role", cfg=cfg, tp=tp,
                            max_pages=max_pages)
    return [role_summary(i) for i in items if i.get("id")]


async def get_role(role_id: str, *, cfg=None, tp=None) -> dict:
    """One role with its resource and integration, as a :func:`role_summary` dict."""
    if not re.match(r"^[A-Za-z0-9-]{1,64}$", role_id or ""):
        raise EntitleApiError("Entitle has no such role.")
    return role_summary(await get_one(f"/public/v1/roles/{role_id}", what="role",
                                      cfg=cfg, tp=tp))
