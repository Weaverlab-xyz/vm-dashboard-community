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
                # _ref carries the account's OWN name, not the operator's spelling —
                # it becomes ansible_user, and the suffix form is significant there.
                return _ref(system, account)
    return None


def system_name(system: dict) -> str:
    """A ps-cli managed-system row's name, with the same field fallbacks
    :func:`normalize_managed_systems` uses."""
    return (system.get("Name") or system.get("SystemName") or "").strip()


def narrow_by_ip(candidates: list, ip: str) -> list:
    """Pick between name candidates using the IP, or hand back the ambiguity.

    The precedence, which has exactly ONE definition because both the per-host ps-cli
    lookup and the batch estate lookup call this:

    1. Candidates whose ``IPAddress`` also matches — the unambiguous hit.
    2. Otherwise every candidate. A lone one is the system registered under a name
       with no (or a placeholder) IP, which is what cloud-native plugin onboarding
       looks like. Several is genuine ambiguity — two systems can share a name in
       different workgroups (``DC01`` in shield.int and ``DC01`` in weaverlab.xyz) —
       and it is handed to the caller to surface rather than silently resolved.
    """
    return [s for s in candidates or [] if s.get("IPAddress") == ip] or list(candidates or [])


def filter_by_ip(all_systems: list, ip: str) -> list:
    """Every managed system in an estate whose ``IPAddress`` matches."""
    return [s for s in all_systems or [] if s.get("IPAddress") == ip]


def select_systems(all_systems: list, ip: str, name: str) -> list:
    """The managed systems matching ``(ip, name)``, chosen from an ALREADY-FETCHED
    estate rather than by asking ps-cli about this one host.

    This exists because the per-host lookup's fallback is a full
    ``managed-systems list`` — a 60s-timeout subprocess. A bulk run resolving 50
    targets one at a time would pay for that estate up to 50 times and time the
    request out. This lets a batch caller read the estate ONCE and answer every
    target locally, reusing :func:`narrow_by_ip` so it cannot drift from the
    per-host path.

    The ONE deliberate difference from that path: the name candidates are matched
    HERE, case-insensitively against the system's own name, rather than by ps-cli's
    ``-n``. The estate is already in hand, so there is nothing to ask. Callers that
    need ps-cli's own name semantics should keep using the per-host lookup.

    Pure: fetching the raw rows is the caller's job.
    """
    ip = (ip or "").strip()
    name = (name or "").strip()
    if name:
        lowered = name.lower()
        candidates = [s for s in all_systems or []
                      if system_name(s).lower() == lowered]
        if candidates:
            return narrow_by_ip(candidates, ip)
    return filter_by_ip(all_systems, ip)


# Why the suggestion tiers below carry a BASIS rather than just a ref: the operator
# has to be able to tell an inference from their own pick before they submit a fleet
# run. These are the values ``suggest_account`` reports.
BASIS_DEFAULT_NAME = "default-name"      # matched the account name chosen for the batch
BASIS_RECORDED_SYSTEM = "recorded-system"  # the managed system this VM was onboarded into
BASIS_ONLY_ACCOUNT = "only-account"      # exactly one candidate existed


def suggest_account(systems: list, default_name: str = "", ps_system_id: str = ""):
    """Pre-select a managed account for one target of a bulk run.

    Returns ``(ref, basis)`` — a :func:`find_account_by_name`-shaped ref and one of
    the ``BASIS_*`` constants — or ``(None, "")`` when nothing is confident enough to
    suggest. A suggestion is a starting point the operator can override per row; it
    is never applied without being shown.

    Three tiers, first hit wins:

    1. **The name chosen for the batch.** Explicit intent, and it also GUARANTEES the
       pre-selection equals what the name-only fallback would resolve to for this
       host — which is what makes leaving a row untouched safe.
    2. **The managed system this VM was actually onboarded into** (``ps_system_id``,
       recorded at registration), when it has exactly one account. An exact key beats
       an address: plugin-onboarded systems carry a ``127.0.0.1`` placeholder and a
       packed locator, so they have no usable address to match on at all.
    3. **A single unambiguous candidate** — one system, one account.

    There is deliberately **no fuzzy/similar-name tier**. Password Safe names a system
    after its HostName, this dashboard has written four different things into that
    field across its onboarding paths, and two production bugs came out of matching on
    it — see the ``ps_attribute_catalog`` module docstring. A wrong suggestion here
    checks out the wrong machine's credential.
    """
    by_name = find_account_by_name(systems, default_name)
    if by_name:
        return by_name, BASIS_DEFAULT_NAME

    if (ps_system_id or "").strip():
        wanted = str(ps_system_id).strip()
        for system in systems or []:
            if str(system.get("system_id")) != wanted:
                continue
            accounts = system.get("accounts") or []
            if len(accounts) == 1:
                return _ref(system, accounts[0]), BASIS_RECORDED_SYSTEM
            # Recorded but ambiguous: fall through rather than guess between its
            # accounts. Tier 3 will decline too, which is the honest answer.
            break

    candidates = [(s, a) for s in systems or [] for a in (s.get("accounts") or [])]
    if len(candidates) == 1:
        return _ref(*candidates[0]), BASIS_ONLY_ACCOUNT
    return None, ""


def _ref(system: dict, account: dict) -> dict:
    """A ``ManagedAccountRef``-shaped dict for one normalized system+account pair."""
    return {
        "system_id":    system["system_id"],
        "account_id":   account["account_id"],
        "account_name": account.get("name") or "",
        "uses_ssh_key": bool(account.get("uses_ssh_key")),
    }


# A sentinel is required: ``None`` is a MEANINGFUL value in the per-target map (see
# pick_ref), so it cannot double as "absent".
_ABSENT = object()


def pick_ref(per_target: dict, target_id: str, default):
    """The managed-account ref for ONE target of a bulk run.

    **Presence decides, not truthiness.** A key mapped to ``None`` means the operator
    said this target gets no managed account; an ABSENT key means it falls back to the
    batch default. Collapsing the two would make "none for this host" silently mean
    "use the fleet account here", which is the bug this whole feature exists to fix.
    """
    if not per_target:
        return default
    found = per_target.get(target_id, _ABSENT)
    return default if found is _ABSENT else found


def stray_ids(keys, target_ids) -> list:
    """Per-target keys naming something that is not a target of this run.

    Refused by the caller, never ignored. A silently-dropped override is the
    one-account-for-every-object bug coming back: the operator picks a distinct
    account for a host, the key does not match, and that host quietly runs under the
    fleet default instead. Sorted so the error message is stable.
    """
    return sorted(set(keys or ()) - set(target_ids or ()))


def requires_ephemeral_store(has_managed: bool, eff_runner: str,
                             is_adhoc: bool, is_playbook: bool) -> bool:
    """True when a managed-account run would dispatch to a store-referencing cloud
    runner (ECS / Cloud Run), where a JIT-checked-out credential can't be injected
    inline and would need an ephemeral store copy.

    ACI is excluded — it injects inline via ``secure_value``, so managed accounts
    work there directly. The API rejects the ECS/GCP case up front unless/until
    ephemeral store copy is implemented + enabled."""
    return bool(has_managed) and eff_runner in EPHEMERAL_STORE_RUNNERS and is_adhoc and is_playbook
