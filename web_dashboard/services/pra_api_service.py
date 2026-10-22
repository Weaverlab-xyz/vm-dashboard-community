"""
BeyondTrust PRA (SRA) Configuration-API client — minimal REST for the few
things the terraform provider can't do interactively, e.g. enumerating Vault
account groups to fill the database provision form's dropdown.

Uses the same OAuth client-credentials pair as terraform_pra_service:

  bt_api_host      - PRA appliance hostname, e.g. tenant.beyondtrustcloud.com
  bt_client_id     - OAuth2 client credentials
  bt_client_secret

``pra_auth_mode = workload`` swaps the token call for the shared BeyondTrust
platform identity (services/bt_platform_identity.py): no ``/oauth2/token``, the
container's OIDC token + ``X-BT-Service-Name`` instead, sent to
``pra_workload_api_url``. REST only — the sra Terraform provider still takes the
client credentials above.

Endpoints (the same surface the sra terraform provider calls):
  POST   /oauth2/token                                 (client credentials, Basic auth)
  GET    /api/config/v1/vault/account-group
  GET    /api/config/v1/jump-group                     (jump-group picker)
  GET    /api/config/v1/jumpoint                       (jumpoint picker)
  POST   /api/config/v1/jump-item/protocol-tunnel-jump (create a k8s tunnel jump)
  DELETE /api/config/v1/jump-item/protocol-tunnel-jump/{id}

The protocol-tunnel-jump create is done over REST (not Terraform) because the
beyondtrust/sra provider's ``tunnel_type`` schema validator omits ``"k8s"`` (it
only allows ``tcp``/``mssql`` through v1.3.0), so ``terraform plan`` rejects a k8s
tunnel client-side even though the PRA backend accepts it. See
docs/notes/sra-provider-k8s-tunnel-bug.md.
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


def _uses_workload() -> bool:
    from . import bt_platform_identity
    return bt_platform_identity.uses_workload("pra")


def configured() -> bool:
    if _uses_workload():
        from . import bt_platform_identity
        return not bt_platform_identity.missing("pra")
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


async def _auth(client: httpx.AsyncClient) -> tuple[str, dict]:
    """``(base, auth headers)`` for this install's PRA, in either auth mode."""
    if _uses_workload():
        from . import bt_platform_identity
        try:
            return (bt_platform_identity.api_url("pra"),
                    bt_platform_identity.headers("pra"))
        except bt_platform_identity.IdentityError as exc:
            raise PRAApiError(str(exc)) from exc
    host = _host()
    token = await _token(client, host)
    return host, {"Authorization": f"Bearer {token}"}


async def _list_config(path: str, label: str) -> list[dict]:
    """GET a PRA config-API collection and normalize to ``[{id, name}]``.
    Raises PRAApiError on any failure — callers treat listing as best-effort."""
    async with httpx.AsyncClient(timeout=20.0, headers={"Accept": "application/json"}) as client:
        host, auth = await _auth(client)
        resp = await client.get(f"{host}{path}", headers=auth)
        if resp.status_code != 200:
            raise PRAApiError(f"GET {path} failed ({resp.status_code}): {resp.text[:400]}")
        items = resp.json()
        if not isinstance(items, list):
            raise PRAApiError(f"unexpected {path} response: {str(items)[:400]}")
        return [
            {"id": it.get("id"), "name": str(it.get("name") or f"{label} {it.get('id')}")}
            for it in items if it.get("id") is not None
        ]


_TUNNEL_PATH = "/api/config/v1/jump-item/protocol-tunnel-jump"


async def _resolve_id(items: list[dict], name: str, label: str) -> int:
    match = next((it for it in items if str(it.get("name")) == name), None)
    if match is None or match.get("id") is None:
        raise PRAApiError(f"{label} named {name!r} not found in PRA")
    return int(match["id"])


async def create_k8s_tunnel_jump(*, name: str, hostname: str, url: str,
                                 ca_certificates: str, jump_group_name: str,
                                 jumpoint_name: str, tag: str = "Kubernetes") -> tuple[int, int]:
    """Create a ``tunnel_type=k8s`` protocol-tunnel jump via the PRA Config API and
    return ``(jump_id, jump_group_id)`` — the jump-group id is reused to scope the
    Vault token account to the group (a per-jump-item association is rejected for
    ``tunnel_type=k8s``). Resolves the Asset Group + Jumpoint names to ids first
    (both must already exist). This is the REST replacement for the Terraform path,
    which the sra provider blocks for k8s (see module docstring)."""
    async with httpx.AsyncClient(timeout=30.0, headers={"Accept": "application/json"}) as client:
        host, auth = await _auth(client)

        async def _get_list(path: str, label: str) -> list[dict]:
            r = await client.get(f"{host}{path}", headers=auth)
            if r.status_code != 200:
                raise PRAApiError(f"GET {path} failed ({r.status_code}): {r.text[:300]}")
            return r.json() if isinstance(r.json(), list) else []

        jg_id = await _resolve_id(await _get_list("/api/config/v1/jump-group", "asset group"),
                                  jump_group_name, "asset group")
        jp_id = await _resolve_id(await _get_list("/api/config/v1/jumpoint", "jumpoint"),
                                  jumpoint_name, "jumpoint")
        body = {
            "name": name[:128],
            "hostname": hostname,
            "jump_group_id": jg_id,
            "jump_group_type": "shared",
            "jumpoint_id": jp_id,
            "tunnel_type": "k8s",
            "url": url,
            "ca_certificates": ca_certificates,
            "tag": tag,
            "comments": "Auto-provisioned by Infrastructure Management Dashboard (k8s tunnel)",
        }
        resp = await client.post(f"{host}{_TUNNEL_PATH}", headers=auth, json=body)
        if resp.status_code not in (200, 201):
            raise PRAApiError(f"create k8s tunnel jump failed ({resp.status_code}): {resp.text[:400]}")
        jump_id = (resp.json() or {}).get("id")
        if jump_id is None:
            raise PRAApiError(f"k8s tunnel create returned no id: {resp.text[:300]}")
        return int(jump_id), int(jg_id)


async def delete_protocol_tunnel_jump(jump_id) -> None:
    """Delete a protocol-tunnel jump by id (best-effort teardown). 404 is treated
    as already-gone."""
    async with httpx.AsyncClient(timeout=30.0, headers={"Accept": "application/json"}) as client:
        host, auth = await _auth(client)
        resp = await client.delete(f"{host}{_TUNNEL_PATH}/{jump_id}", headers=auth)
        if resp.status_code not in (200, 204, 404):
            raise PRAApiError(f"delete tunnel jump {jump_id} failed ({resp.status_code}): {resp.text[:300]}")


async def list_vault_account_groups() -> list[dict]:
    """Vault account groups for the credential-injection picker — ``[{id, name}]``."""
    return await _list_config("/api/config/v1/vault/account-group", "group")


async def list_jump_groups() -> list[dict]:
    """PRA Asset Groups for the provision form's jump-group picker — ``[{id, name}]``.
    The tunnel HCL filters by name (sra_jump_group_list), so callers submit the name."""
    return await _list_config("/api/config/v1/jump-group", "asset group")


async def list_jumpoints() -> list[dict]:
    """PRA Jumpoints for the provision form's jumpoint picker — ``[{id, name}]``.
    The tunnel HCL filters by name (sra_jumpoint_list), so callers submit the name."""
    return await _list_config("/api/config/v1/jumpoint", "jumpoint")


async def list_pickers() -> dict:
    """All PRA-sourced form pickers — Vault account groups, Asset Groups and Jumpoints —
    fetched concurrently. Cloud-agnostic (PRA objects aren't region/cloud-scoped).
    Best-effort: any individual failure yields an empty list for that picker (callers
    fall back to the configured default at broker time). Returns
    ``{vault_account_groups, jump_groups, jumpoints}`` — all empty when PRA is
    unconfigured. Shared by the cloud-DB provision form + the k8s PRA-tunnel modal."""
    import asyncio
    empty = {"vault_account_groups": [], "jump_groups": [], "jumpoints": []}
    if not configured():
        return empty
    vg, jg, jp = await asyncio.gather(
        list_vault_account_groups(), list_jump_groups(), list_jumpoints(),
        return_exceptions=True,
    )

    def _ok(x, what):
        if isinstance(x, Exception):
            logger.warning("PRA %s listing failed (non-fatal): %s", what, x)
            return []
        return x

    return {
        "vault_account_groups": _ok(vg, "vault account-group"),
        "jump_groups": _ok(jg, "jump-group"),
        "jumpoints": _ok(jp, "jumpoint"),
    }
