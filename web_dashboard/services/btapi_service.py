"""
BeyondTrust PRA service for Jump Group and Shell Jump management.
Uses btapi subprocess (asyncio.to_thread pattern, same as powershell.py).

Required environment variables (inherited from process env or set in .env):
  BT_CLIENT_ID     - BeyondTrust API account client ID
  BT_CLIENT_SECRET - BeyondTrust API account client secret
  BT_API_HOST      - BeyondTrust PRA appliance hostname (e.g. "pra.example.com")

Secret retrieval (get_ps_secret) uses ps-cli (beyondtrust-bips-cli Python
package) which handles OAuth2 auth automatically via PSCLI_* env vars and
works cross-platform. The btapi binary (Linux) is baked into the Docker
image at /usr/local/bin/btapi; on Windows dev use BTAPI_EXECUTABLE override.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
from typing import Optional

from ..config import settings


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


def _bt_env() -> dict:
    """Build environment dict for btapi (BeyondTrust PRA) subprocess calls.
    Resolution order for BT_* credentials:
      1. config_service (DB-backed, set via setup wizard / settings panel)
      2. settings (.env / pydantic-settings)
      3. Inherited os.environ
      4. Windows registry (set via setx or System Properties)
    """
    env = dict(os.environ)
    for cfg_key, env_key in [
        ("bt_api_host", "BT_API_HOST"),
        ("bt_client_id", "BT_CLIENT_ID"),
        ("bt_client_secret", "BT_CLIENT_SECRET"),
    ]:
        val = _cfg(cfg_key)
        if val:
            env[env_key] = val
        elif env_key not in env:
            reg_val = _read_windows_env(env_key)
            if reg_val:
                env[env_key] = reg_val
    return env


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
    """Raised when a btapi operation fails."""


_DNS_ERROR_FRAGMENTS = ("NameResolutionError", "No address associated with hostname", "getaddrinfo failed")


def _run(args: list, payload: dict = None, timeout: int = 60, retries: int = 3, retry_delay: float = 5.0):
    """
    Run btapi synchronously.
    Pass payload as JSON on stdin if provided.
    Returns parsed JSON output, or None if stdout is empty.
    Raises BTAPIError on non-zero exit code.
    Retries up to `retries` times on transient DNS/connection failures.
    """
    import time

    cmd = [settings.btapi_executable] + args
    # When no payload, explicitly close stdin so btapi doesn't block waiting
    # on inherited stdin (which is a closed pipe in a headless uvicorn process).
    run_kwargs = dict(capture_output=True, text=True, timeout=timeout, env=_bt_env())
    if payload is not None:
        run_kwargs["input"] = json.dumps(payload)
    else:
        run_kwargs["stdin"] = subprocess.DEVNULL

    last_error: BTAPIError | None = None
    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, **run_kwargs)

        # Strip PyInstaller deprecation warnings from stderr
        stderr_lines = [
            line for line in result.stderr.splitlines()
            if not any(kw in line for kw in (
                "UserWarning", "pkg_resources", "Setuptools<", "PyInstaller", "slated for removal"
            ))
        ]
        stderr = "\n".join(stderr_lines).strip()

        if result.returncode != 0:
            error_text = stderr or result.stdout.strip()
            last_error = BTAPIError(
                f"btapi {' '.join(str(a) for a in args[:2])} failed: {error_text}"
            )
            # Retry on transient DNS / connection failures
            if attempt < retries and any(frag in error_text for frag in _DNS_ERROR_FRAGMENTS):
                time.sleep(retry_delay)
                continue
            raise last_error

        stdout = result.stdout.strip()
        if not stdout:
            return None
        return json.loads(stdout)

    raise last_error  # exhausted retries


# ── Jump Group ────────────────────────────────────────────────────────────────

def _list_jump_groups() -> list:
    return _run(["list", "jump-group"]) or []


def _find_jump_group(name: str) -> Optional[dict]:
    for g in _list_jump_groups():
        if g.get("name") == name:
            return g
    return None


def _create_jump_group(name: str) -> dict:
    # Derive a safe code_name: "us-east-2" → "us_east_2"
    code_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    result = _run(["add", "jump-group"], payload={"name": name, "code_name": code_name})
    if not result:
        raise BTAPIError(f"No response when creating jump group '{name}'")
    return result


def _get_or_create_jump_group(name: str) -> dict:
    """Return existing jump group by name, or create it."""
    existing = _find_jump_group(name)
    if existing:
        return existing
    return _create_jump_group(name)


# ── Shell Jump ────────────────────────────────────────────────────────────────

def _create_shell_jump(
    name: str,
    hostname: str,
    jump_group_id: int,
    jumpoint_id: int,
    port: int = 22,
    tag: str = "AWS",
    comments: str = "",
) -> dict:
    payload = {
        "name": name,
        "hostname": hostname,
        "jump_group_id": jump_group_id,
        "jump_group_type": "shared",
        "jumpoint_id": jumpoint_id,
        "port": port,
        "protocol": "ssh",
        "terminal": "xterm",
        "keep_alive": 0,
        "tag": tag,
    }
    if comments:
        payload["comments"] = comments
    # Resource path confirmed from live API: jump-item/shell-jump
    result = _run(["add", "jump-item/shell-jump"], payload=payload)
    if not result:
        raise BTAPIError(f"No response when creating shell jump '{name}'")
    return result


def _delete_shell_jump(shell_jump_id: int) -> None:
    _run(["delete", "jump-item/shell-jump", str(shell_jump_id)])


# ── Group Policy ──────────────────────────────────────────────────────────────

def _find_group_policy(name: str) -> Optional[dict]:
    policies = _run(["list", "group-policy"]) or []
    for p in policies:
        if p.get("name") == name:
            return p
    return None


def _grant_jump_group_to_policy(group_policy_id: int, jump_group_id: int) -> None:
    """Grant a shared jump group access to a group policy."""
    _run(
        ["do", f"group-policy/{group_policy_id}/jump-group-policy"],
        payload={
            "jump_group_id": jump_group_id,
            "jump_group_type": "shared",
        },
    )


# ── Public async API ──────────────────────────────────────────────────────────

async def provision_ec2_jump(
    instance_name: str,
    hostname: str,
    jump_group_name: str,
    group_policy_name: str,
    jumpoint_id: int,
    port: int = 22,
    tag: str = "AWS",
) -> dict:
    """
    Provision BeyondTrust PRA access for a newly deployed EC2 instance:
      1. Get or create a Jump Group named jump_group_name.
      2. Create a Shell Jump pointing at hostname with the instance name,
         reachable via the specified Jumpoint.
      3. Grant group_policy_name access to the Jump Group (idempotent).

    Returns a dict with jump_group_id, shell_jump_id, shell_jump_name.
    Raises BTAPIError if btapi is unavailable or a required step fails.
    """
    # Step 1: Ensure the Jump Group exists
    jump_group = await asyncio.to_thread(_get_or_create_jump_group, jump_group_name)
    jump_group_id = jump_group["id"]

    # Step 2: Create the Shell Jump
    shell_jump = await asyncio.to_thread(
        _create_shell_jump,
        instance_name,
        hostname,
        jump_group_id,
        jumpoint_id,
        port,
        tag,
        "Auto-provisioned by Infrastructure Management Dashboard",
    )

    # Step 3: Grant group policy access (best-effort — may already be granted)
    policy = await asyncio.to_thread(_find_group_policy, group_policy_name)
    if policy:
        try:
            await asyncio.to_thread(
                _grant_jump_group_to_policy, policy["id"], jump_group_id
            )
        except BTAPIError:
            # Grant may fail if access is already granted — not fatal
            pass

    return {
        "jump_group_id": jump_group_id,
        "jump_group_name": jump_group_name,
        "shell_jump_id": shell_jump.get("id"),
        "shell_jump_name": instance_name,
    }


async def remove_ec2_jump(shell_jump_id: int) -> None:
    """Remove a Shell Jump when the EC2 instance is destroyed."""
    await asyncio.to_thread(_delete_shell_jump, shell_jump_id)


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
    """
    args = ["requests", "create", "-a-id", str(account_id),
            "-d", str(duration_min), "-s-id", str(system_id)]
    raw = _ps_run(args)
    # Response is a JSON string like "RequestID: 345"
    match = re.search(r"\d+", str(raw))
    if not match:
        raise BTAPIError(f"Could not parse RequestID from ps-cli response: {raw!r}")
    return int(match.group())


def _get_credential_by_request_sync(request_id: int, ssh_key: bool = False) -> str:
    """Retrieve the credential value for an approved request.
    Pass ssh_key=True to add -t dsskey and retrieve the SSH private key.
    """
    args = ["credentials", "get-by-request-id", "-r", str(request_id)]
    if ssh_key:
        args += ["-t", "dsskey"]
    # With -t dsskey ps-cli prints the raw OpenSSH PEM key to stdout (not JSON)
    data = _ps_run(args)
    if isinstance(data, str):
        if not data:
            raise BTAPIError(f"ps-cli returned empty string for request {request_id}")
        return data
    if isinstance(data, list):
        if not data:
            raise BTAPIError(f"ps-cli returned empty list for request {request_id}")
        data = data[0]
    if isinstance(data, str):
        return data
    value = (
        data.get("password") or data.get("Password") or
        data.get("text") or data.get("Text") or
        data.get("PrivateKey") or data.get("privateKey")
    )
    if not value:
        raise BTAPIError(
            f"No credential value in ps-cli response for request {request_id} "
            f"(fields={list(data.keys())!r})"
        )
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
