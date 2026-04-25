"""
Nutanix AHV service layer — Prism Central REST API v3.

Works with both Prism Central (multi-cluster) and Prism Element (single-cluster).
Authentication is HTTP Basic; no SDK required — httpx is sufficient.

All blocking HTTP calls run in asyncio.to_thread() so the FastAPI event loop
is never blocked.
"""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class NutanixError(Exception):
    pass


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


def _client() -> httpx.Client:
    host = _cfg("nutanix_host")
    if not host:
        raise NutanixError("NUTANIX_HOST is not configured")

    port     = int(_cfg("nutanix_port") or "9440")
    username = _cfg("nutanix_username") or "admin"
    password = _cfg("nutanix_password")
    if not password:
        raise NutanixError("NUTANIX_PASSWORD is not configured")

    verify = _cfg_bool("nutanix_verify_ssl", False)

    return httpx.Client(
        base_url=f"https://{host}:{port}/api/nutanix/v3",
        auth=(username, password),
        verify=verify,
        timeout=60.0,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )


# ── Normalisation ─────────────────────────────────────────────────────────────

def _normalise_vm(entity: dict) -> dict:
    metadata  = entity.get("metadata", {})
    status    = entity.get("status", {})
    resources = status.get("resources", {})

    power_state = (resources.get("power_state") or "UNKNOWN").upper()

    vcpus   = resources.get("num_vcpus_per_socket", 1) * resources.get("num_sockets", 1)
    mem_mib = resources.get("memory_size_mib", 0)

    ips: list[str] = []
    for nic in resources.get("nic_list", []):
        for ep in nic.get("ip_endpoint_list", []):
            ip = ep.get("ip", "")
            if ip and ":" not in ip and not ip.startswith("127."):
                ips.append(ip)

    ngt      = (resources.get("guest_tools") or {}).get("nutanix_guest_tools") or {}
    ngt_on   = ngt.get("ngt_state") == "ENABLED"
    ngt_live = bool(ngt.get("is_reachable", False))

    return {
        "uuid":        metadata.get("uuid", ""),
        "name":        status.get("name", ""),
        "power_state": power_state,
        "is_running":  power_state == "ON",
        "vcpus":       vcpus,
        "mem_mib":     mem_mib,
        "ip_addresses": ips,
        "ngt_enabled":  ngt_on,
        "ngt_reachable": ngt_live,
        "cluster":     (status.get("cluster_reference") or {}).get("name", ""),
        "description": status.get("description", ""),
    }


# ── List VMs (paginated) ──────────────────────────────────────────────────────

def _list_vms_sync() -> list[dict]:
    with _client() as c:
        offset, length = 0, 500
        entities: list[dict] = []

        while True:
            r = c.post("/vms/list", json={"kind": "vm", "offset": offset, "length": length})
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise NutanixError(f"Prism API error {r.status_code}: {r.text[:300]}") from e

            data  = r.json()
            batch = data.get("entities") or []
            entities.extend(batch)

            total = (data.get("metadata") or {}).get("total_matches", 0)
            offset += length
            if offset >= total or not batch:
                break

    return sorted([_normalise_vm(e) for e in entities], key=lambda v: v["name"].lower())


# ── Task polling ──────────────────────────────────────────────────────────────

def _wait_for_task(c: httpx.Client, task_uuid: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while True:
        r = c.get(f"/tasks/{task_uuid}")
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"Task poll error: {r.status_code}") from e

        data   = r.json()
        status = (data.get("status") or "").upper()

        if status == "SUCCEEDED":
            return
        if status == "FAILED":
            msg = data.get("error_detail") or data.get("progress_message") or "Task failed"
            raise NutanixError(msg)
        if time.monotonic() > deadline:
            raise NutanixError(f"Task {task_uuid} timed out after {timeout}s")
        time.sleep(3)


# ── Power operations ──────────────────────────────────────────────────────────

# Valid Prism v3 power-state transitions
_TRANSITIONS = {
    "start":    "ON",
    "shutdown": "ACPI_SHUTDOWN",   # graceful — requires NGT
    "stop":     "OFF",             # force power-off
    "reboot":   "ACPI_REBOOT",     # graceful reboot — requires NGT
    "reset":    "RESET",           # hard reset
    "pause":    "PAUSE",
    "resume":   "RESUME",
}


def _power_op_sync(uuid: str, name: str, op: str) -> dict:
    if op not in _TRANSITIONS:
        raise NutanixError(f"Unknown operation: {op}")

    transition = _TRANSITIONS[op]
    logger.info("Nutanix: %s (%s) on %s (%s)", op, transition, name or uuid, uuid)

    with _client() as c:
        r = c.post(
            f"/vms/{uuid}/set_power_state",
            json={"transition": transition},
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NutanixError(f"Power op failed {r.status_code}: {r.text[:300]}") from e

        task_uuid = r.json().get("task_uuid")
        if task_uuid:
            _wait_for_task(c, task_uuid)

    return {"uuid": uuid, "name": name, "op": op, "status": "OK"}


# ── Async public API ──────────────────────────────────────────────────────────

async def list_vms() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_vms_sync)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Failed to list VMs: {e}") from e


async def power_op(uuid: str, name: str, op: str) -> dict:
    if op not in _TRANSITIONS:
        raise NutanixError(
            f"Invalid operation '{op}'. Must be one of: {', '.join(sorted(_TRANSITIONS))}"
        )
    try:
        return await asyncio.to_thread(_power_op_sync, uuid, name, op)
    except NutanixError:
        raise
    except Exception as e:
        raise NutanixError(f"Power operation '{op}' on {name or uuid} failed: {e}") from e
