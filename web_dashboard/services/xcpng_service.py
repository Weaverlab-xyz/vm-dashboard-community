"""
XCP-ng / XenServer service layer — XAPI via XML-RPC (stdlib xmlrpc.client).

Uses Python's built-in xmlrpc.client — no external SDK required.
Sessions are opened per call and always logged out in a finally block.

All blocking XAPI calls run in asyncio.to_thread() so the FastAPI event loop
is never blocked.
"""
import asyncio
import http.client as _http
import logging
import ssl
import xmlrpc.client

logger = logging.getLogger(__name__)

NULL_REF = "OpaqueRef:NULL"


class XcpNgError(Exception):
    pass


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


# ── Transport with configurable timeout ───────────────────────────────────────

class _TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: int = 60, context=None):
        super().__init__(context=context)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


# ── Session management ────────────────────────────────────────────────────────

def _connect():
    """Open an XAPI session. Caller must call server.session.logout(session) when done."""
    host = _cfg("xcpng_host")
    if not host:
        raise XcpNgError("XCPNG_HOST is not configured")

    username   = _cfg("xcpng_username") or "root"
    password   = _cfg("xcpng_password")
    if not password:
        raise XcpNgError("XCPNG_PASSWORD is not configured")
    verify_ssl = _cfg_bool("xcpng_verify_ssl", False)

    if verify_ssl:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE

    transport = _TimeoutTransport(timeout=120, context=ctx)
    server    = xmlrpc.client.ServerProxy(f"https://{host}/", transport=transport)

    result = server.session.login_with_password(username, password, "2.0", "vm-dashboard")
    if result.get("Status") != "Success":
        err = result.get("ErrorDescription", ["Login failed"])
        raise XcpNgError(f"Authentication failed: {err}")

    return server, result["Value"]   # Value is the session opaque-ref


def _call(server, session: str, method: str, *args):
    """Call an XAPI method and unwrap the result, raising XcpNgError on failure."""
    obj = server
    for part in method.split("."):
        obj = getattr(obj, part)
    result = obj(session, *args)
    if isinstance(result, dict):
        if result.get("Status") == "Failure":
            err = result.get("ErrorDescription", ["Unknown error"])
            raise XcpNgError(f"{method} failed: {err}")
        return result.get("Value")
    return result


# ── List VMs ──────────────────────────────────────────────────────────────────

def _list_vms_sync() -> list[dict]:
    server, session = _connect()
    try:
        # Batch fetch everything in three calls — no per-VM round trips
        all_vms   = _call(server, session, "VM.get_all_records")
        all_gm    = _call(server, session, "VM_guest_metrics.get_all_records")
        all_hosts = _call(server, session, "host.get_all_records")
    finally:
        try:
            server.session.logout(session)
        except Exception:
            pass

    results: list[dict] = []
    for ref, vm in all_vms.items():
        # Skip templates, control domain (dom0), and snapshots
        if vm.get("is_a_template") or vm.get("is_control_domain") or vm.get("is_snapshot_from_vmpp"):
            continue

        gm_ref  = vm.get("guest_metrics", NULL_REF)
        gm      = all_gm.get(gm_ref, {}) if gm_ref != NULL_REF else {}
        networks = gm.get("networks", {})
        ips = [
            v for k, v in networks.items()
            if k.endswith("/ip") and ":" not in v and v not in ("127.0.0.1", "")
        ]

        host_ref  = vm.get("resident_on", NULL_REF)
        host_rec  = all_hosts.get(host_ref, {}) if host_ref != NULL_REF else {}
        host_name = host_rec.get("name_label", "")

        power_state = vm.get("power_state", "Unknown")

        results.append({
            "uuid":            vm.get("uuid", ""),
            "name":            vm.get("name_label", ""),
            "power_state":     power_state,
            "is_running":      power_state == "Running",
            "host":            host_name,
            "vcpus":           int(vm.get("VCPUs_max", 0)),
            "mem_mb":          int(vm.get("memory_dynamic_max", 0)) // (1024 * 1024),
            "ip_addresses":    ips,
            "tools_installed": gm_ref != NULL_REF,
            "os_version":      (gm.get("os_version") or {}).get("name", ""),
            "description":     vm.get("name_description", ""),
        })

    return sorted(results, key=lambda v: v["name"].lower())


# ── Power operations ──────────────────────────────────────────────────────────

def _power_op_sync(uuid: str, name: str, op: str) -> dict:
    server, session = _connect()
    try:
        vm_ref = _call(server, session, "VM.get_by_uuid", uuid)
        logger.info("XCP-ng: %s on %s (%s)", op, name or uuid, uuid)

        if op == "start":
            _call(server, session, "VM.start", vm_ref, False, False)
        elif op == "shutdown":
            _call(server, session, "VM.clean_shutdown", vm_ref)
        elif op == "stop":
            _call(server, session, "VM.hard_shutdown", vm_ref)
        elif op == "reboot":
            _call(server, session, "VM.clean_reboot", vm_ref)
        elif op == "hard_reboot":
            _call(server, session, "VM.hard_reboot", vm_ref)
        elif op == "suspend":
            _call(server, session, "VM.suspend", vm_ref)
        elif op == "resume":
            _call(server, session, "VM.resume", vm_ref, False, False)
        elif op == "pause":
            _call(server, session, "VM.pause", vm_ref)
        elif op == "unpause":
            _call(server, session, "VM.unpause", vm_ref)
        else:
            raise XcpNgError(f"Unknown operation: {op}")

    finally:
        try:
            server.session.logout(session)
        except Exception:
            pass

    return {"uuid": uuid, "name": name, "op": op, "status": "OK"}


_VALID_OPS = frozenset(
    {"start", "shutdown", "stop", "reboot", "hard_reboot", "suspend", "resume", "pause", "unpause"}
)


# ── Async public API ──────────────────────────────────────────────────────────

async def list_vms() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_vms_sync)
    except XcpNgError:
        raise
    except Exception as e:
        raise XcpNgError(f"Failed to list VMs: {e}") from e


async def power_op(uuid: str, name: str, op: str) -> dict:
    if op not in _VALID_OPS:
        raise XcpNgError(
            f"Invalid operation '{op}'. Must be one of: {', '.join(sorted(_VALID_OPS))}"
        )
    try:
        return await asyncio.to_thread(_power_op_sync, uuid, name, op)
    except XcpNgError:
        raise
    except Exception as e:
        raise XcpNgError(f"Power operation '{op}' on {name or uuid} failed: {e}") from e
