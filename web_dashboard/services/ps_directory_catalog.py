"""Password Safe's directories, shaped into import candidates for the Directories page.

The directory twin of ``ps_database_catalog``, and pure for the same reason: the I/O
half (``ps_api_service.read_directory_inventory``) and this half are tested apart.
It reuses that module's text hygiene and account helpers rather than restating them.

A managed system is a candidate when it is linked to a Password Safe directory
(``DirectoryID``) or its platform names a directory kind. Each candidate carries what
``directory_service.register_onprem`` needs, read from Password Safe's own rows, so the
import route can re-resolve an id the client sent into a host, port and account without
trusting anything else in the request.
"""
from . import ps_database_catalog as _db

MAX_CANDIDATES = 500

# Platform name fragments → dashboard provider. Checked in order; "active directory"
# first so "Active Directory (LDAP)"-style names land on AD.
_PROVIDER_RULES = (
    ("onprem_ad", ("active directory", "microsoft ad")),
    ("ldap", ("openldap", "ldap", "389", "directory server", "freeipa", "red hat idm")),
)

REASON_NO_PROVIDER = ("Platform {platform!r} is not a directory kind the dashboard knows "
                      "(Active Directory or LDAP).")
REASON_NO_HOST = "Password Safe records no domain name or host for this directory."
REASON_NO_ACCOUNT = ("No account on this directory is requestable by the dashboard's API "
                     "identity. Grant it the Requestor role on a Smart Rule containing the "
                     "account you want the dashboard to use.")
REASON_NOT_FQDN = "An Active Directory domain must be a DNS name such as corp.example.com."


def provider_for_platform(name: str, short_name: str = "") -> str:
    haystack = f"{name or ''} {short_name or ''}".strip().lower()
    if not haystack:
        return ""
    for provider, needles in _PROVIDER_RULES:
        if any(n in haystack for n in needles):
            return provider
    return ""


def _index_directories(directories) -> dict:
    index = {}
    for row in directories or []:
        if not isinstance(row, dict):
            continue
        directory_id = _db._id_of(row, "DirectoryID", "DirectoryId", "ID")
        if directory_id is None:
            continue
        ssl = row.get("UseSSL")
        index[directory_id] = {
            "domain": _db._clean_text(_db._first(row, "DomainName", "ForestName")),
            "port": _db._id_of(row, "Port"),
            "use_ssl": None if ssl is None else bool(ssl),
            "platform_id": _db._id_of(row, "PlatformID", "PlatformId"),
        }
    return index


def build_candidates(*, platforms, systems, directories, accounts,
                     max_candidates=MAX_CANDIDATES):
    """``(candidates, truncated)``; every row carries ``eligible`` and, when false, a
    ``reason`` the dialog shows verbatim."""
    platform_index = _db._index_platforms(platforms)
    directory_index = _index_directories(directories)
    accounts_by_system = _db._group_accounts(accounts)

    rows = []
    for system in systems or []:
        if not isinstance(system, dict):
            continue
        system_id = _db._id_of(system, "ManagedSystemID", "SystemId", "SystemID")
        if system_id is None:
            continue
        directory_id = _db._id_of(system, "DirectoryID", "DirectoryId")
        directory = directory_index.get(directory_id) or {}
        platform_id = (_db._id_of(system, "PlatformID", "PlatformId")
                       or directory.get("platform_id"))
        platform = platform_index.get(platform_id) or {}
        provider = provider_for_platform(platform.get("name") or "",
                                         platform.get("short_name") or "")
        if directory_id is None and not provider:
            continue

        domain = directory.get("domain") or ""
        # A domain name is itself a usable host: its A records are its domain
        # controllers, and the agent resolves it per run. An explicit host wins.
        host = _db._clean_text(_db._first(system, "DnsName", "HostName", "IPAddress")) or domain
        use_ldaps = directory.get("use_ssl")
        if use_ldaps is None:
            use_ldaps = True
        port = (_db._id_of(system, "Port") or directory.get("port")
                or (636 if use_ldaps else 389))
        name = domain if provider == "onprem_ad" else _db._clean_text(
            _db._first(system, "SystemName", "Name")) or domain
        row_accounts = _db._normalize_accounts(accounts_by_system.get(system_id))

        if not provider:
            eligible, reason = False, REASON_NO_PROVIDER.format(
                platform=platform.get("name") or "(unknown)")
        elif not host:
            eligible, reason = False, REASON_NO_HOST
        elif provider == "onprem_ad" and "." not in (name or ""):
            eligible, reason = False, REASON_NOT_FQDN
        elif not row_accounts:
            eligible, reason = False, REASON_NO_ACCOUNT
        else:
            eligible, reason = True, ""

        rows.append({
            "system_id": system_id,
            "name": name,
            "provider": provider,
            "platform": _db._clean_text(platform.get("name") or ""),
            "host": host,
            "port": port,
            "use_ldaps": bool(use_ldaps),
            "accounts": row_accounts,
            "eligible": eligible,
            "reason": reason,
            "already_registered": False,
        })

    rows.sort(key=lambda r: (not r["eligible"], (r["name"] or "").lower(), r["system_id"]))
    cap = max_candidates if isinstance(max_candidates, int) and max_candidates > 0 \
        else MAX_CANDIDATES
    return rows[:cap], len(rows) > cap


def find_account(candidate: dict, account_id) -> dict:
    return _db.find_account(candidate, account_id)


def managed_account(candidate: dict, account_id) -> dict:
    """The ``managed_account`` ref ``register_onprem`` takes, from the candidate's own
    account row — never from anything the client sent."""
    account = find_account(candidate, account_id)
    if not account:
        return {}
    return {"system_id": candidate.get("system_id"), "account_id": account["account_id"],
            "account_name": account.get("name") or ""}
