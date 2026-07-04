"""Pure helpers for BeyondTrust Password Safe managed-account checkout in Ansible
runs. Kept stdlib-only (no config / FastAPI imports) so the shaping + guard logic
is unit-testable by file path, mirroring services/cloud_ansible_secrets.py.

The credential checkout itself (ps-cli I/O) lives in services/btapi_service; the
run wiring lives in api/config_mgmt. This module only shapes the *live list*
(names/ids, never credentials) and answers the local-runner-only guard.
"""
import re

CLOUD_RUNNERS = ("ecs", "aci", "gcp")

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def host_is_ip(host: str) -> bool:
    """True if host looks like a bare IPv4 address, so the managed-account lookup
    matches on IPAddress rather than system name."""
    return bool(_IPV4_RE.match((host or "").strip()))


def normalize_managed_systems(systems: list, accounts_by_system: dict) -> list:
    """Shape ps-cli managed-systems + their accounts into the API/UI response —
    **ids and names only, never credentials**.

    ps-cli field names vary (locally-managed vs domain-linked accounts, ``list``
    vs ``list-accounts``), so each is read with fallbacks. ``accounts_by_system``
    maps a system id → the raw account list for that system.
    ``DSSAutoManagementFlag`` True means the account is managed as an SSH key
    (checked out via ``-t dsskey``) rather than a password.
    """
    out = []
    for s in systems or []:
        sid = s.get("ManagedSystemID") or s.get("SystemId") or s.get("SystemID")
        if sid is None:
            continue
        sid = int(sid)
        accounts = []
        for a in accounts_by_system.get(sid, []) or []:
            aid = a.get("ManagedAccountID") or a.get("AccountId") or a.get("AccountID")
            if aid is None:
                continue
            accounts.append({
                "account_id":   int(aid),
                "name":         a.get("AccountName") or a.get("Name") or "",
                "domain":       a.get("DomainName") or "",
                "uses_ssh_key": bool(a.get("DSSAutoManagementFlag")),
            })
        out.append({
            "system_id": sid,
            "name":      s.get("Name") or s.get("SystemName") or "",
            "ip":        s.get("IPAddress") or "",
            "accounts":  accounts,
        })
    return out


def local_only_violation(has_managed: bool, eff_runner: str,
                         is_adhoc: bool, is_playbook: bool) -> bool:
    """True when a managed-account run would dispatch to a cloud runner — which is
    unsupported (a JIT-checked-out credential is ephemeral, so it can't live in a
    cloud store the way #217's cloud secrets must). Managed accounts are
    local-runner only; the API rejects this combination up front."""
    return bool(has_managed) and eff_runner in CLOUD_RUNNERS and is_adhoc and is_playbook
