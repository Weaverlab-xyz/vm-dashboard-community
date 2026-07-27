"""Pure helpers for BeyondTrust Password Safe managed-account checkout in Ansible
runs. Kept stdlib-only (no config / FastAPI imports) so the shaping + guard logic
is unit-testable by file path, mirroring services/cloud_ansible_secrets.py.

The credential checkout itself (ps-cli I/O) lives in services/btapi_service; the
run wiring lives in api/config_mgmt. This module only shapes the *live list*
(names/ids, never credentials) and answers the local-runner-only guard.
"""
import re

# Runners that *reference* a store secret (the task identity fetches the value at
# launch) rather than taking it inline. A checked-out managed-account credential is
# ephemeral, so it can't be injected inline on these — it would need an ephemeral
# store copy. ACI is NOT here: it injects inline via secure_value.
EPHEMERAL_STORE_RUNNERS = ("ecs", "gcp")

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def host_is_ip(host: str) -> bool:
    """True if host looks like a bare IPv4 address, so the managed-account lookup
    matches on IPAddress rather than system name."""
    return bool(_IPV4_RE.match((host or "").strip()))


def lookup_args(host: str, name: str = "") -> tuple:
    """Resolve ``(ip, name)`` for a Password Safe managed-system lookup.

    ``host`` is the connection address the operator picked (a cloud VM's IP, or a
    free-text on-prem host). ``name`` is an optional system-name hint — for a cloud
    VM it's the deploy name, which is how cloud-native onboarding registers the
    system (e.g. the AWS Systems Manager plugin keys the managed system on the
    instance name with a placeholder IP, so an IP-only lookup never finds it).

    - IP ``host``  → match on IPAddress, but still pass the ``name`` hint so a
      name-registered system with no matching IP is found (falls back to it).
    - non-IP host  → the host is itself the system name; an explicit ``name`` wins.
    """
    host = (host or "").strip()
    name = (name or "").strip()
    if host_is_ip(host):
        return host, name
    return "", (name or host)


def ssh_login_user(account_name: str) -> str:
    """The OS login user for a managed account name used as ``ansible_user``.

    Cloud-native plugins qualify the account name with a scope suffix after a
    ``;`` — the AWS Systems Manager plugin registers ``{user};{suffix}`` (suffix
    ``local`` for IAM-user mode or an AssumeRole ARN for EC2 mode). That suffix is
    a Password Safe naming detail, not part of the Unix username, so strip it. A
    ``;`` can't appear in a real Unix username, so this is a no-op for ordinary
    accounts (e.g. ``root``, ``svc-ansible``)."""
    return (account_name or "").split(";", 1)[0].strip()


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
            # Change-after-release: BeyondTrust rotates the password when the
            # request/session is released. Recommended for accounts used on the
            # ECS/GCP ephemeral path — a missed cleanup then leaves only a rotated,
            # dead credential. None when ps-cli doesn't report the flag (unknown).
            car = a.get("ChangePasswordAfterAnyReleaseFlag")
            accounts.append({
                "account_id":   int(aid),
                "name":         a.get("AccountName") or a.get("Name") or "",
                "domain":       a.get("DomainName") or "",
                "uses_ssh_key": bool(a.get("DSSAutoManagementFlag")),
                "change_after_release": None if car is None else bool(car),
            })
        out.append({
            "system_id": sid,
            "name":      s.get("Name") or s.get("SystemName") or "",
            "ip":        s.get("IPAddress") or "",
            "accounts":  accounts,
        })
    return out


def find_account_by_name(systems: list, name: str):
    """Locate a managed account by NAME across the normalized systems for one host.

    This is what makes a managed account usable in a bulk run. A
    ``ManagedAccountRef`` pins ``system_id`` + ``account_id``, both specific to one
    managed system, so the same ref cannot be reused across a fleet — it would check
    out one machine's credential and connect everywhere with it. Instead each job
    looks up the chosen account name against ITS OWN host and builds the ref from
    that host's ids.

    Matching accepts the raw account name or its :func:`ssh_login_user` form, so a
    cloud-plugin registration like ``svc-ansible;local`` (AWS Systems Manager appends
    a scope suffix) matches a plain ``svc-ansible``. Comparison is case-insensitive:
    Password Safe account names are not case-sensitive in practice, and a case
    mismatch here would surface as a confusing per-host "account not found".

    Returns a ``ManagedAccountRef``-shaped dict, or ``None`` when the host has no
    such account. Pure — the ps-cli calls that produce ``systems`` are the caller's.
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    for system in systems or []:
        for account in (system.get("accounts") or []):
            raw = (account.get("name") or "").strip().lower()
            if wanted in (raw, ssh_login_user(raw)):
                return {
                    "system_id":    system["system_id"],
                    "account_id":   account["account_id"],
                    # The account's own name, not the operator's spelling — it
                    # becomes ansible_user, and the suffix form is significant there.
                    "account_name": account.get("name") or "",
                    "uses_ssh_key": bool(account.get("uses_ssh_key")),
                }
    return None


def requires_ephemeral_store(has_managed: bool, eff_runner: str,
                             is_adhoc: bool, is_playbook: bool) -> bool:
    """True when a managed-account run would dispatch to a store-referencing cloud
    runner (ECS / Cloud Run), where a JIT-checked-out credential can't be injected
    inline and would need an ephemeral store copy.

    ACI is excluded — it injects inline via ``secure_value``, so managed accounts
    work there directly. The API rejects the ECS/GCP case up front unless/until
    ephemeral store copy is implemented + enabled."""
    return bool(has_managed) and eff_runner in EPHEMERAL_STORE_RUNNERS and is_adhoc and is_playbook
