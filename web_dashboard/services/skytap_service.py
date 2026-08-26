"""The Skytap lab-platform adapter.

Implements ``lab_platforms.READ_CONTRACT`` over ``skytap_client``. Everything here is
mapping: Skytap's JSON in, the dashboard's shape out. The transport concerns — Basic auth,
the 423/Retry-After retry, the ``keep_idle=true`` guarantee, count/offset pagination — all
live in the client and are deliberately not repeated.

Skytap's vocabulary maps onto the feature's like this:

    template       -> the thing a POV is created FROM
    configuration  -> a running environment. The API calls it a "configuration"; the UI and
                      the docs call it an environment. This module speaks "environment"
                      outward and only uses "configuration" in the URL paths.
    vm             -> a VM inside one

One naming rule worth stating because breaking it is silent: every read is keyed on the
environment's **id**, never its name. An SE may rename an environment in Skytap to whatever
their approvals allow, and nothing here should notice.
"""
from __future__ import annotations

import logging

from .skytap_client import DEFAULT_BASE_URL, SkytapClient, SkytapCreds, SkytapError

logger = logging.getLogger(__name__)

# Re-exported so callers can catch one error type without importing the client too.
__all__ = ["SkytapError", "configured", "credentials", "list_templates",
           "list_environments", "get_environment"]


def _cfg(key: str) -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:  # noqa: BLE001
        pass
    from ..config import settings
    return getattr(settings, key, "") or ""


def credentials() -> SkytapCreds:
    return SkytapCreds(
        username=_cfg("skytap_username"),
        api_token=_cfg("skytap_api_token"),
        base_url=_cfg("skytap_base_url") or DEFAULT_BASE_URL,
    )


def configured() -> bool:
    return credentials().valid()


def _client() -> SkytapClient:
    return SkytapClient(credentials())


# ── mapping ──────────────────────────────────────────────────────────────────

def _vm_count(raw: dict) -> int | None:
    """VM count, or None when the payload did not carry one.

    None, never 0: a collection listing may omit the nested `vms` array entirely, and
    rendering "0 VMs" for a template that has eight is worse than rendering nothing. Same
    rule the hypervisor projections follow for live-only fields.
    """
    vms = raw.get("vms")
    if isinstance(vms, list):
        return len(vms)
    for key in ("vm_count", "svms"):
        if isinstance(raw.get(key), int):
            return raw[key]
    return None


def _os_family(raw: dict) -> str:
    """Best-effort guess, used to pick the wire-up path (SSH vs RDP) later.

    Returns "" rather than guessing wrong: an empty value shows as unknown in the UI and
    makes the later wire-up ask, whereas a confident wrong answer would send a Windows VM
    down the Password-Safe-over-SSH path.
    """
    blob = " ".join(str(raw.get(k) or "") for k in ("guestos", "os", "name", "hardware"))
    low = blob.lower()
    if "windows" in low or "win" in low.split():
        return "windows"
    for token in ("linux", "ubuntu", "debian", "centos", "rhel", "red hat", "suse",
                  "rocky", "alma", "fedora"):
        if token in low:
            return "linux"
    return ""


def _interfaces(raw_vm: dict) -> tuple[str, list[dict]]:
    """(private_ip, published_services) for a VM.

    A published service is Skytap NAT-ing a guest port to a public ip:port. We surface them
    because they are useful to see, NOT because the wire-up uses them — POV wiring reaches
    VMs on their private IPs through an in-environment Gateway. See docs/pov-instance.md.
    """
    private_ip = ""
    published: list[dict] = []
    for iface in (raw_vm.get("interfaces") or []):
        if not private_ip and iface.get("ip"):
            private_ip = str(iface["ip"])
        for svc in (iface.get("services") or []):
            published.append({
                "id": str(svc.get("id") or ""),
                "internal_port": svc.get("internal_port"),
                "external_ip": svc.get("external_ip") or "",
                "external_port": svc.get("external_port"),
            })
    return private_ip, published


def _template(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "description": raw.get("description") or "",
        "vm_count": _vm_count(raw),
        "region": raw.get("region") or "",
        "url": raw.get("url") or "",
    }


def _environment(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "runstate": raw.get("runstate") or "",
        "vm_count": _vm_count(raw),
        "region": raw.get("region") or "",
        "url": raw.get("url") or "",
        # Skytap's own view of whether it is throttling this environment. Surfaced rather
        # than hidden: it explains a slow provision without anyone reading a log.
        "rate_limited": bool(raw.get("rate_limited")),
        "suspend_on_idle": raw.get("suspend_on_idle"),
        "shutdown_on_idle": raw.get("shutdown_on_idle"),
    }


def _vm(raw: dict) -> dict:
    private_ip, published = _interfaces(raw)
    return {
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "runstate": raw.get("runstate") or "",
        "os_family": _os_family(raw),
        "private_ip": private_ip,
        "published_services": published,
    }


# ── the READ_CONTRACT ────────────────────────────────────────────────────────

async def list_templates() -> list[dict]:
    """Every template this account can instantiate."""
    raw = await _client().list("/v2/templates")
    return [_template(t) for t in raw if isinstance(t, dict)]


async def list_environments() -> list[dict]:
    """Every environment this account can see.

    Includes environments the dashboard did not create. That is deliberate for a read-only
    view — an SE's existing hand-built POVs are exactly what they want to see next to the
    managed ones — and it is why the eventual POV table keys on the Skytap id rather than
    assuming it owns everything in the account.
    """
    raw = await _client().list("/v2/configurations")
    return [_environment(e) for e in raw if isinstance(e, dict)]


async def get_environment(env_id: str) -> dict:
    """One environment, with its VMs, private IPs and published services."""
    env_id = str(env_id or "").strip()
    if not env_id:
        raise SkytapError("an environment id is required")
    raw = await _client().get(f"/v2/configurations/{env_id}")
    if not isinstance(raw, dict):
        raise SkytapError(f"Skytap returned no environment for id {env_id!r}")
    out = _environment(raw)
    out["vms"] = [_vm(v) for v in (raw.get("vms") or []) if isinstance(v, dict)]
    # The nested read is authoritative where the collection read was not.
    out["vm_count"] = len(out["vms"])
    return out
