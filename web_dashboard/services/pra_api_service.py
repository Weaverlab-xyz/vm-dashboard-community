"""
BeyondTrust PRA (SRA) Configuration-API client — minimal REST for the few
things the terraform provider can't do interactively, e.g. enumerating Vault
account groups to fill the database provision form's dropdown.

Uses the same OAuth client-credentials pair as terraform_pra_service:

  bt_api_host      - PRA appliance hostname, e.g. tenant.beyondtrustcloud.com
  bt_client_id     - OAuth2 client credentials
  bt_client_secret

Endpoints (the same surface the sra terraform provider calls):
  POST /oauth2/token                       (client credentials, Basic auth)
  GET  /api/config/v1/vault/account-group
"""
import logging

import httpx

logger = logging.getLogger(__name__)


class PRAApiError(Exception):
    """Raised when a PRA config-API call fails."""


def _cfg(key: str) -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, "") or ""


def configured() -> bool:
    return all(_cfg(k) for k in ("bt_api_host", "bt_client_id", "bt_client_secret"))


def _host() -> str:
    host = _cfg("bt_api_host").rstrip("/")
    if not host:
        raise PRAApiError("bt_api_host is not configured")
    if not host.lower().startswith("http"):
        host = f"https://{host}"
    return host


async def _token(client: httpx.AsyncClient, host: str) -> str:
    resp = await client.post(
        f"{host}/oauth2/token",
        auth=(_cfg("bt_client_id"), _cfg("bt_client_secret")),
        data={"grant_type": "client_credentials"},
    )
    if resp.status_code != 200:
        raise PRAApiError(f"PRA OAuth token request failed ({resp.status_code}): {resp.text[:400]}")
    token = resp.json().get("access_token", "")
    if not token:
        raise PRAApiError("PRA OAuth token response contained no access_token")
    return token


async def list_vault_account_groups() -> list[dict]:
    """Return Vault account groups as ``[{id, name}]`` (for the provision
    form's account-group picker). Raises PRAApiError on any failure — callers
    treat this as best-effort."""
    host = _host()
    async with httpx.AsyncClient(timeout=20.0, headers={"Accept": "application/json"}) as client:
        token = await _token(client, host)
        resp = await client.get(
            f"{host}/api/config/v1/vault/account-group",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            raise PRAApiError(
                f"GET vault/account-group failed ({resp.status_code}): {resp.text[:400]}")
        groups = resp.json()
        if not isinstance(groups, list):
            raise PRAApiError(f"unexpected vault/account-group response: {str(groups)[:400]}")
        return [
            {"id": g.get("id"), "name": str(g.get("name") or f"group {g.get('id')}")}
            for g in groups if g.get("id") is not None
        ]
