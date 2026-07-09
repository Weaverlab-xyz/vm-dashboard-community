"""
BeyondTrust Password Safe + ps-cli wrapper for the dashboard.

The previous Jump Group / Shell Jump provisioning surface here has been
replaced by the Terraform PRA provider (services/terraform_pra_service.py).
This module now exposes only what the rest of the codebase still needs:

  - get_ps_secret(title)           — fetch a vaulted secret by title
  - get_ps_credential(...)         — managed-account password / SSH key checkout
  - list_ps_managed_systems(...)   — Password Safe inventory queries

All secret + credential calls go through ps-cli (the beyondtrust-bips-cli
Python package), which handles OAuth2 auth via PSCLI_* env vars and works
cross-platform. The legacy btapi binary path is retained internally for
the OAuth2 endpoint that still uses BT_CLIENT_ID / BT_CLIENT_SECRET /
BT_API_HOST credentials.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys

from ..config import settings

logger = logging.getLogger(__name__)


def _cfg(key: str) -> str:
    """Read a config value, preferring the DB-backed config_service (where the
    setup wizard / settings panels write) over the env-var-derived `settings`
    object. Returns empty string if neither has a value.

    BT credentials are user-configurable secrets and live in the DB; this helper
    keeps `btapi_service` from depending on .env or process environment for them.
    """
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    return getattr(settings, key, "") or ""


def _read_windows_env(name: str) -> str:
    """Read a Windows user or machine environment variable directly from the registry.
    This works even when the variable is set via System Properties / setx but
    was not inherited by the current process (e.g. set before uvicorn started).
    Only runs on Windows — returns empty string on Linux/macOS.
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        for hive, subkey in [
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ]:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    return str(value)
            except (FileNotFoundError, OSError):
                continue
    except ImportError:
        pass
    return ""


def _pscli_env() -> dict:
    """Build environment dict for ps-cli (BeyondTrust Password Safe) subprocess calls.
    Resolution order for PSCLI_* credentials:
      1. config_service (DB-backed, set via setup wizard / settings panel)
      2. settings (.env / pydantic-settings)
      3. Inherited os.environ
      4. Windows registry (set via setx or System Properties)
    """
    env = dict(os.environ)
    for cfg_key, env_key in [
        ("pscli_api_url", "PSCLI_API_URL"),
        ("pscli_client_id", "PSCLI_CLIENT_ID"),
        ("pscli_client_secret", "PSCLI_CLIENT_SECRET"),
    ]:
        val = _cfg(cfg_key)
        if val:
            env[env_key] = val
        elif env_key not in env:
            reg_val = _read_windows_env(env_key)
            if reg_val:
                env[env_key] = reg_val
    return env


class BTAPIError(Exception):
    """Raised when a Password Safe / ps-cli operation fails."""


# Shell Jump / Jump Group / Group Policy provisioning was previously handled
# here via the legacy btapi binary. Community is now fully on the Terraform
# PRA provider (services/terraform_pra_service.py); the btapi-based path is
# gone. The remaining surface in this module is Password Safe secret +
# managed-account retrieval via ps-cli.


# ── Password Safe managed systems/accounts (ps-cli) ──────────────────────────

def _ps_run(args: list, timeout: int = 60):
    """Run ps-cli synchronously, return parsed JSON. Raises BTAPIError on failure."""
    try:
        result = subprocess.run(
            [settings.pscli_executable, "--format", "json"] + args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_pscli_env(),
        )
    except subprocess.TimeoutExpired:
        raise BTAPIError(f"ps-cli timed out running {' '.join(args[:2])} after {timeout}s")
    if result.returncode != 0:
        raise BTAPIError(
            f"ps-cli {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    stdout = result.stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout  # Return raw text (e.g. "RequestID: 345") for callers that parse it


def _list_managed_systems_by_name_sync(name: str) -> list:
    return _ps_run(["managed-systems", "list", "-n", name]) or []


def _list_managed_systems_by_ip_sync(ip: str) -> list:
    """List all managed systems and return those whose IPAddress matches."""
    all_systems = _ps_run(["managed-systems", "list"]) or []
    return [s for s in all_systems if s.get("IPAddress") == ip]


def _list_managed_systems_by_ip_or_name_sync(ip: str, name: str) -> list:
    """
    Find the best-matching managed system for a Windows VM.

    Strategy (avoids fetching the full system list which can be slow/paginated):
    1. If name given: use ps-cli -n <name> to get a small candidate set,
       then prefer the candidate whose IPAddress matches ip.
    2. If no name or no -n match: fall back to listing all systems filtered by IP.

    This correctly handles multiple systems with the same name in different
    workgroups (e.g. DC01 in shield.int and DC01 in weaverlab.xyz).
    """
    if name:
        candidates = _ps_run(["managed-systems", "list", "-n", name]) or []
        # Prefer the candidate whose IP matches exactly
        by_ip = [s for s in candidates if s.get("IPAddress") == ip]
        if by_ip:
            return by_ip
        # If no IP match but name matched exactly one system, use it
        # (handles cases where IP isn't registered in PS at all)
        if len(candidates) == 1:
            return candidates
        # Multiple name matches, no IP disambiguation — return all so caller
        # can surface the ambiguity rather than silently picking the wrong one
        if candidates:
            return candidates

    # No name or name lookup returned nothing — try full list filtered by IP
    all_systems = _ps_run(["managed-systems", "list"]) or []
    return [s for s in all_systems if s.get("IPAddress") == ip]


def _list_managed_accounts_sync(system_id: int) -> list:
    return _ps_run(["managed-accounts", "list", "-id", str(system_id)]) or []


def _list_managed_accounts_with_fallback_sync(system_id: int) -> list:
    """
    Fetch managed accounts for a system, with fallback to list-accounts.

    ps-cli managed-accounts list -id <n> only returns locally managed accounts.
    Domain-linked accounts (e.g. AD accounts linked to a Windows system) are
    only visible via list-accounts, which returns all accounts across all systems
    with different field names (AccountId/SystemId vs ManagedAccountID).

    Returns a normalised list where every entry has ManagedAccountID and
    ManagedSystemID so callers don't need to know which path was taken.
    """
    results = _ps_run(["managed-accounts", "list", "-id", str(system_id)]) or []
    if results:
        return results

    # Fallback: fetch all accounts and filter by SystemId
    all_accounts = _ps_run(["managed-accounts", "list-accounts"]) or []
    matched = [a for a in all_accounts if a.get("SystemId") == system_id]

    # Normalise field names to match the list -id schema expected by callers
    normalised = []
    for a in matched:
        normalised.append({
            "ManagedAccountID": a.get("AccountId"),
            "ManagedSystemID":  a.get("SystemId"),
            "AccountName":      a.get("AccountName", ""),
            "DSSAutoManagementFlag": False,   # domain accounts use password, not SSH key
            "ApiEnabled": True,
            "DomainName": a.get("DomainName"),
            "UserPrincipalName": a.get("UserPrincipalName"),
        })
    return normalised


def _create_ps_request_sync(
    system_id: int, account_id: int, duration_min: int = 30
) -> int:
    """Create a credential request; returns the numeric RequestID.
    No -a-type flag — ps-cli request creation does not take a type argument.

    ``-c-op reuse`` (ConflictOption): when an active request already exists for this
    account — a prior just-in-time checkout that hasn't been checked in or expired —
    Password Safe returns that request instead of a 409 Conflict. Without it, the
    409 error text ("...statuscode: 409") got mis-parsed as a bogus RequestID and
    the subsequent credential fetch failed opaquely.
    """
    args = ["requests", "create", "-a-id", str(account_id),
            "-d", str(duration_min), "-s-id", str(system_id), "-c-op", "reuse"]
    raw = _ps_run(args)
    # Success is a string like "RequestID: 345" — parse the id that follows the
    # RequestID label specifically, so an error body (which may contain other
    # numbers, e.g. a 409 status) can't be mistaken for a request id.
    raw_str = str(raw)
    match = re.search(r"RequestID\D*(\d+)", raw_str)
    if not match:
        raise BTAPIError(f"Could not create a Password Safe request: {raw_str.strip()}")
    return int(match.group(1))


def _get_credential_by_request_sync(request_id: int, ssh_key: bool = False) -> str:
    """Retrieve the credential value for an approved request.
    Pass ssh_key=True to add -t dsskey and retrieve the SSH private key.
    """
    args = ["credentials", "get-by-request-id", "-r", str(request_id)]
    if ssh_key:
        args += ["-t", "dsskey"]
    # With -t dsskey ps-cli prints the raw OpenSSH PEM key to stdout (not JSON)
    data = _ps_run(args)

    # ps-cli can exit 0 but print a soft-failure to stdout INSTEAD of the
    # credential — e.g. "It was not possible to get a credential for Request ID: N"
    # when the request isn't releasable (the access policy requires approval, or the
    # requestor can't auto-release). Detect it and fail loudly; otherwise the error
    # text is handed back as the "credential" and only surfaces much later as an
    # opaque SSH "Load key: error in libcrypto" once written to the key file.
    if isinstance(data, str) and "not possible to get a credential" in data.lower():
        raise BTAPIError(
            f"{data.strip()} — Password Safe would not release a credential. This "
            f"usually means the account's access policy requires approval; the "
            f"just-in-time checkout needs auto-approval for the API requestor.")

    if isinstance(data, list):
        if not data:
            raise BTAPIError(f"ps-cli returned empty list for request {request_id}")
        data = data[0]
    if isinstance(data, str):
        value = data
    elif isinstance(data, dict):
        value = (
            data.get("password") or data.get("Password") or
            data.get("text") or data.get("Text") or
            data.get("PrivateKey") or data.get("privateKey")
        )
    else:
        value = None
    if not value:
        raise BTAPIError(f"No credential value in ps-cli response for request {request_id}")
    # An SSH-key checkout that came back without a PEM marker isn't a key (e.g. a
    # ps-cli soft-error not caught above) — don't hand it on to be written as one.
    if ssh_key and "PRIVATE KEY" not in value:
        raise BTAPIError(
            f"Password Safe did not return an SSH private key for request {request_id} "
            f"({len(value)} chars) — check the account is DSS-managed with a minted key.")
    return value


async def list_ps_managed_systems(name: str) -> list:
    """List Password Safe managed systems matching the given hostname."""
    return await asyncio.to_thread(_list_managed_systems_by_name_sync, name)


async def list_ps_managed_systems_by_ip(ip: str) -> list:
    """List Password Safe managed systems matching the given IP address."""
    return await asyncio.to_thread(_list_managed_systems_by_ip_sync, ip)


async def list_ps_managed_systems_by_ip_or_name(ip: str, name: str) -> list:
    """
    List Password Safe managed systems matching IP address, falling back to
    SystemName/HostName if no IP match. Handles Windows machines registered
    by hostname rather than IP.
    """
    return await asyncio.to_thread(_list_managed_systems_by_ip_or_name_sync, ip, name)


async def list_ps_managed_accounts(system_id: int) -> list:
    """List managed accounts for a Password Safe managed system."""
    return await asyncio.to_thread(_list_managed_accounts_sync, system_id)


async def list_ps_managed_accounts_with_fallback(system_id: int) -> list:
    """
    List managed accounts with fallback to list-accounts for domain-linked accounts.
    Use this for Windows VMs where accounts may be domain-linked rather than local.
    """
    return await asyncio.to_thread(_list_managed_accounts_with_fallback_sync, system_id)


async def get_ps_credential(
    system_id: int, account_id: int, duration_min: int = 30, uses_ssh_key: bool = False
) -> str:
    """Check out a credential from Password Safe for the given account.
    Pass uses_ssh_key=True to retrieve the SSH private key via -t dsskey.
    """
    request_id = await asyncio.to_thread(
        _create_ps_request_sync, system_id, account_id, duration_min
    )
    return await asyncio.to_thread(
        _get_credential_by_request_sync, request_id, uses_ssh_key
    )


async def get_ps_credential_with_request(
    system_id: int, account_id: int, duration_min: int = 30, uses_ssh_key: bool = False
) -> tuple:
    """Like get_ps_credential, but also return the numeric request id so the caller
    can flag rotate-on-check-in and check the request in when done. Returns
    ``(request_id, credential)``."""
    request_id = await asyncio.to_thread(
        _create_ps_request_sync, system_id, account_id, duration_min
    )
    cred = await asyncio.to_thread(
        _get_credential_by_request_sync, request_id, uses_ssh_key
    )
    return request_id, cred


# Per-request rotation, so a credential can be rotated on release without the
# managed account's standing "Change Password After Release" setting. Sent as raw
# ps-cli API calls (`ps-cli raw PUT <endpoint>`) so they map exactly to the
# documented REST endpoints PUT /api/public/v3/Requests/{id}/rotateoncheckin and
# .../checkin — no dependency on subcommand naming. Best-effort: a failure is
# logged, never fatal (the ephemeral store copy is force-deleted regardless).

def _rotate_on_checkin_sync(request_id: int) -> None:
    _ps_run(["raw", "PUT", f"Requests/{request_id}/rotateoncheckin"])


def _checkin_request_sync(request_id: int) -> None:
    _ps_run(["raw", "PUT", f"Requests/{request_id}/checkin"])


async def rotate_ps_request_on_checkin(request_id: int) -> bool:
    """Flag a credential request so Password Safe rotates the password when it's
    checked in. Returns True on success, False (logged) on any failure."""
    try:
        await asyncio.to_thread(_rotate_on_checkin_sync, request_id)
        return True
    except BTAPIError as exc:
        # request_id is omitted from the message: it's unpacked from the
        # (request_id, credential) checkout tuple, which CodeQL taints as sensitive.
        logger.warning("Password Safe rotate-on-check-in failed: %s", exc)
        return False


async def checkin_ps_request(request_id: int) -> bool:
    """Check a credential request back in (releases it; triggers rotation if the
    request was flagged rotate-on-check-in). Returns True on success."""
    try:
        await asyncio.to_thread(_checkin_request_sync, request_id)
        return True
    except BTAPIError as exc:
        logger.warning("Password Safe check-in failed: %s", exc)
        return False


# ── Password Safe secrets (ps-cli) ────────────────────────────────────────────

def _get_ps_secret_sync(title: str) -> str:
    """Retrieve a Secrets Safe secret value via ps-cli subprocess.

    ps-cli authenticates automatically using PSCLI_API_URL / PSCLI_CLIENT_ID /
    PSCLI_CLIENT_SECRET environment variables (set from Key Vault / .env).
    Works cross-platform — beyondtrust-bips-cli installs ps-cli via pip.
    """
    try:
        result = subprocess.run(
            [settings.pscli_executable, "--format", "json", "secrets", "get", "-t", title, "-d"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            env=_pscli_env(),
        )
    except subprocess.TimeoutExpired:
        raise BTAPIError(f"ps-cli timed out fetching secret '{title}' after 120s.")
    if result.returncode != 0:
        raise BTAPIError(
            f"ps-cli failed to get secret '{title}': "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise BTAPIError(
            f"ps-cli returned non-JSON for secret '{title}': {result.stdout[:200]!r}"
        ) from e
    if isinstance(data, list):
        if not data:
            raise BTAPIError(f"ps-cli returned empty list for secret '{title}'")
        data = data[0]
    # Credential secrets use Password; Text secrets use Text/SecretText
    value = (
        data.get("Password") or data.get("password") or
        data.get("Text") or data.get("text") or
        data.get("SecretText") or data.get("secretText") or
        data.get("Value") or data.get("value")
    )
    if not value:
        raise BTAPIError(
            f"Secret '{title}' has no password/text value "
            f"(SecretType={data.get('SecretType')!r}, fields={list(data.keys())!r})"
        )
    return value


async def get_ps_secret(title: str) -> str:
    """Retrieve a secret value from BeyondTrust Password Safe via ps-cli."""
    return await asyncio.to_thread(_get_ps_secret_sync, title)
