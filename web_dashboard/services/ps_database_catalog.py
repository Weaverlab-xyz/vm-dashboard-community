"""Shaping for the "Import from Password Safe" database candidate list.

Password Safe already runs a discovery scanner with managed credentials, so it
knows a database's platform, instance, port and accounts authoritatively — far
more than a credential-less socket probe could. This module turns the four raw
Password Safe collections (Platforms, ManagedSystems, Databases, ManagedAccounts)
into the candidate rows the /databases import modal renders, and turns a chosen
candidate back into a ``RegisterDatabaseRequest`` payload.

Two disciplines, both borrowed rather than invented:

* :data:`CANDIDATE_KEYS` is a **closed allowlist**, like ``agent_job_meta``'s
  ``FINDING_KEYS``, and for the same reason pointed the same way — a Password Safe
  managed-system name, account name and platform name are typed by whoever
  onboarded the system, so they are external text on their way to a browser.
  Every string goes through :func:`_clean_text`, which mirrors
  ``agent_job_meta._clean_text`` rather than importing it (see that function).

* Eligibility is decided **here**, server-side, and travels as ``eligible`` plus a
  human ``reason``. The UI never invents its own rule, so it cannot offer a
  checkbox the server would refuse — the same property ``inventory_service``'s
  ``cfg_runnable`` / ``cfg_reason`` pair gives the bulk-run page.

Pure and stdlib-only (no config, no FastAPI, no httpx), so the whole mapping is
testable by file path. The I/O half lives in ``services/ps_api_service``.
"""
import json

# What a candidate may show. Closed. `already_imported` is the one field this
# module does not decide — the API computes it against the dashboard's own rows
# (api/cloud_databases._annotate_imported), exactly as api/agent._annotate_findings
# computes `already_registered`. It is defaulted here so the shape is stable.
CANDIDATE_KEYS = (
    "system_id",        # int — Password Safe ManagedSystemID; the ONLY id a client sends back
    "name",             # str — managed system name
    "host",             # str — DnsName / HostName / IPAddress, in that preference
    "port",             # int | None
    "engine",           # str — postgres|mysql|sqlserver|oracle, "" when unmapped
    "platform",         # str — the Password Safe platform name, shown so an unmapped
                        #       platform is diagnosable rather than just absent
    "db_name",          # str
    "version",          # str — target-reported
    "workgroup_id",     # int | None
    "accounts",         # list — ACCOUNT_KEYS only
    "eligible",         # bool — server-decided
    "reason",           # str  — why not, when not
    "already_imported",  # bool — computed by the API, not here
)

ACCOUNT_KEYS = ("account_id", "name", "domain", "uses_ssh_key")

MAX_TEXT = 512
MAX_CANDIDATES = 500

# Mirrors cloud_database_service.VALID_ENGINES / _DEFAULT_PORTS. Duplicated rather
# than imported because this module is stdlib-only on purpose; the values are a
# stable part of the product, and a test pins them against the service's copy.
VALID_ENGINES = ("postgres", "mysql", "sqlserver", "oracle")
_DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "sqlserver": 1433, "oracle": 1521}

# Platform name → engine. Substring needles, checked against both Name and
# ShortName, lowercased, FIRST HIT WINS — so the order of this tuple is behaviour,
# not style:
#
#   * `mysql` MUST precede `oracle`. Oracle owns MySQL, so "Oracle MySQL" has to
#     resolve to mysql; the other order silently registers it as an Oracle
#     database and every connection attempt afterwards uses the wrong driver.
#   * There is deliberately no bare "sql" needle — it would swallow both
#     PostgreSQL and MySQL into sqlserver.
#   * "mariadb" resolves to mysql, matching the remap the databases page already
#     applies in openFromQuery().
_ENGINE_RULES = (
    ("sqlserver", ("ms sql", "mssql", "sql server", "sqlserver")),
    ("postgres",  ("postgresql", "postgres", "psql", "greenplum")),
    ("mysql",     ("mariadb", "mysql")),
    ("oracle",    ("oracle", "oradb")),
)

# Ineligibility copy. Kept as constants so the API, the tests and the docs quote
# the same sentences — these are the only explanation an operator gets for why a
# row they can see is a row they cannot import.
REASON_DASHBOARD_MANAGED = (
    "onboarded by this dashboard's own cloud-database Password Safe integration — "
    "it is already managed here")
REASON_NO_ENGINE = (
    "platform {platform!r} isn't a database engine the dashboard supports "
    "(postgres, mysql, sqlserver, oracle)")
REASON_NO_HOST = (
    "the managed system has no DNS name, host name or IP address — the runner "
    "would have nowhere to connect")
REASON_NO_ACCOUNT = (
    "no requestable Password Safe account — the API identity needs the Requestor "
    "role and an access policy granting View on a Smart Rule containing this "
    "account")


# ── text hygiene ──────────────────────────────────────────────────────────────

def _clean_text(value) -> str:
    """Strip C0/C1 control characters (except tab) and truncate.

    Mirrors ``agent_job_meta._clean_text`` rather than importing it: both modules
    are stdlib-only on purpose so their boundary can be tested without the app,
    and the rule is four lines. The threat is the same one — ANSI escapes in a
    name would otherwise be replayed into an operator's browser, and into their
    terminal the moment they copy it.
    """
    text = str(value if value is not None else "")
    cleaned = "".join(
        ch for ch in text
        if ch == "\t" or (ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F))
    ).strip()
    return cleaned[:MAX_TEXT]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(mapping: dict, *keys) -> str:
    """First non-empty value among ``keys``. Password Safe field names vary across
    collections and API versions, so every id and name is read with fallbacks —
    the same tolerance ``managed_accounts.normalize_managed_systems`` applies."""
    for key in keys:
        val = mapping.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _id_of(mapping: dict, *keys):
    for key in keys:
        val = mapping.get(key)
        if val not in (None, ""):
            return _int_or_none(val)
    return None


# ── platform → engine ─────────────────────────────────────────────────────────

def parse_platform_map(raw) -> dict:
    """Operator platform→engine overrides from the ``clouddb_ps_import_platform_map``
    JSON config key.

    Tolerant by design: bad JSON, a non-object, or an entry naming an engine the
    dashboard does not have returns/skips rather than raising. A typo in an
    optional convenience key must never take the import list down — the built-in
    rules still work, and the affected platform just reads as unsupported.
    """
    if isinstance(raw, dict):
        items = raw.items()
    else:
        text = (raw or "").strip() if isinstance(raw, str) else ""
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        items = parsed.items()
    out = {}
    for key, value in items:
        name = str(key or "").strip().lower()
        engine = str(value or "").strip().lower()
        if name and engine in VALID_ENGINES:
            out[name] = engine
    return out


def engine_for_platform(name: str, short_name: str = "", extra=None) -> str:
    """Dashboard engine for a Password Safe platform, or ``""`` when unmapped.

    ``extra`` is the operator override map from :func:`parse_platform_map`, checked
    first on an exact (case-insensitive) platform name so a site running Percona or
    a renamed custom plugin can add it without a code change.
    """
    raw_name = str(name or "").strip()
    raw_short = str(short_name or "").strip()
    overrides = extra if isinstance(extra, dict) else {}
    for candidate in (raw_name.lower(), raw_short.lower()):
        if candidate and candidate in overrides:
            return overrides[candidate]

    haystack = f"{raw_name} {raw_short}".lower()
    if not haystack.strip():
        return ""
    for engine, needles in _ENGINE_RULES:
        if any(needle in haystack for needle in needles):
            return engine
    return ""


# ── candidates ────────────────────────────────────────────────────────────────

def _account_sort_key(account: dict) -> tuple:
    """Plain account names ahead of scope-suffixed ones.

    Cloud-native plugins qualify a name with a ``;`` suffix (``svc-dba;local``).
    The dropdown preselects the first entry, and the plain name is far more often
    the login the operator means, so it sorts first.
    """
    name = account.get("name") or ""
    return (1 if ";" in name else 0, name.lower())


def _normalize_accounts(raw_accounts) -> list:
    out = []
    for account in raw_accounts or []:
        if not isinstance(account, dict):
            continue
        account_id = _id_of(account, "ManagedAccountID", "AccountId", "AccountID")
        if account_id is None:
            continue
        out.append({
            "account_id": account_id,
            "name": _clean_text(_first(account, "AccountName", "Name")),
            "domain": _clean_text(_first(account, "DomainName")),
            "uses_ssh_key": bool(account.get("DSSAutoManagementFlag")),
        })
    out.sort(key=_account_sort_key)
    return out


def _group_accounts(accounts) -> dict:
    grouped = {}
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        system_id = _id_of(account, "ManagedSystemID", "SystemId", "SystemID")
        if system_id is None:
            continue
        grouped.setdefault(system_id, []).append(account)
    return grouped


def _index_platforms(platforms) -> dict:
    index = {}
    for platform in platforms or []:
        if not isinstance(platform, dict):
            continue
        platform_id = _id_of(platform, "PlatformID", "PlatformId", "ID")
        if platform_id is None:
            continue
        index[platform_id] = {
            "name": _first(platform, "Name", "PlatformName"),
            "short_name": _first(platform, "ShortName"),
            "default_port": _id_of(platform, "DefaultPort", "Port"),
        }
    return index


def _index_databases(databases) -> dict:
    index = {}
    for database in databases or []:
        if not isinstance(database, dict):
            continue
        database_id = _id_of(database, "DatabaseID", "DatabaseId", "ID")
        if database_id is None:
            continue
        index[database_id] = {
            "port": _id_of(database, "Port"),
            "instance_name": _first(database, "InstanceName"),
            "is_default_instance": bool(database.get("IsDefaultInstance")),
            "version": _first(database, "Version"),
        }
    return index


def build_candidates(*, platforms, systems, databases, accounts,
                     platform_map=None, dashboard_platforms=(),
                     max_candidates=MAX_CANDIDATES):
    """Candidate rows for the import modal, plus whether the cap truncated them.

    Returns ``(candidates, truncated)``. Every row carries ``eligible`` and, when
    false, a ``reason`` the UI shows verbatim on the disabled checkbox.

    **Inclusion is deliberately wider than "has a DatabaseID".** A managed system
    is kept when it has a ``DatabaseID`` *or* its platform maps to an engine. The
    second half is what surfaces the custom-plugin systems this dashboard's own
    onboarding creates ("psql SSM Custom Plugin", "MSSQL Azure Run Command
    Plugin"), which carry no Databases row. Filtering on ``EntityTypeID`` instead
    would be tidier and would silently drop exactly those.

    An unmapped platform on a system that *does* have a ``DatabaseID`` is kept and
    marked ineligible rather than dropped, because "my database isn't in the list"
    is otherwise undiagnosable from the UI.
    """
    platform_index = _index_platforms(platforms)
    database_index = _index_databases(databases)
    accounts_by_system = _group_accounts(accounts)
    overrides = platform_map if isinstance(platform_map, dict) else {}
    managed_here = {str(p or "").strip().lower() for p in (dashboard_platforms or ()) if p}

    rows = []
    for system in systems or []:
        if not isinstance(system, dict):
            continue
        system_id = _id_of(system, "ManagedSystemID", "SystemId", "SystemID")
        if system_id is None:
            continue

        platform_id = _id_of(system, "PlatformID", "PlatformId")
        platform = platform_index.get(platform_id) or {}
        platform_name = platform.get("name") or ""
        engine = engine_for_platform(platform_name, platform.get("short_name") or "", overrides)

        database_id = _id_of(system, "DatabaseID", "DatabaseId")
        database = database_index.get(database_id) or {}

        # The inclusion rule. Everything else about this system is irrelevant if
        # Password Safe does not think it is a database and neither do we.
        if database_id is None and not engine:
            continue

        host = _clean_text(_first(system, "DnsName", "HostName", "IPAddress"))
        port = (_id_of(system, "Port")
                or database.get("port")
                or platform.get("default_port")
                or _DEFAULT_PORTS.get(engine))

        instance = database.get("instance_name") or ""
        if database.get("is_default_instance"):
            instance = ""
        db_name = _clean_text(instance or _first(system, "InstanceName"))

        row_accounts = _normalize_accounts(accounts_by_system.get(system_id))
        eligible, reason = _eligibility(
            platform_name=platform_name, engine=engine, host=host,
            accounts=row_accounts, managed_here=managed_here)

        rows.append({
            "system_id": system_id,
            "name": _clean_text(_first(system, "SystemName", "Name")),
            "host": host,
            "port": port,
            "engine": engine,
            "platform": _clean_text(platform_name),
            "db_name": db_name,
            "version": _clean_text(database.get("version") or ""),
            "workgroup_id": _id_of(system, "WorkgroupID", "WorkgroupId"),
            "accounts": row_accounts,
            "eligible": eligible,
            "reason": reason,
            "already_imported": False,
        })

    # Importable first, then by name — an operator opening the modal is looking for
    # what they can act on, not for alphabetical order across both groups.
    rows.sort(key=lambda r: (not r["eligible"], (r["name"] or "").lower(), r["system_id"]))

    cap = max_candidates if isinstance(max_candidates, int) and max_candidates > 0 else MAX_CANDIDATES
    truncated = len(rows) > cap
    return rows[:cap], truncated


def _eligibility(*, platform_name, engine, host, accounts, managed_here):
    """Why a candidate can or cannot be imported.

    Order is chosen for usefulness, not severity. "This dashboard already manages
    it" comes first because it is a classification rather than a fault and it makes
    every later check moot; the requestable-account check comes last because it is
    the one an operator can actually go and fix, and fixing it would not help if an
    earlier reason applies.
    """
    if platform_name and platform_name.strip().lower() in managed_here:
        return False, REASON_DASHBOARD_MANAGED
    if not engine:
        return False, REASON_NO_ENGINE.format(platform=platform_name or "(unknown)")
    if not host:
        return False, REASON_NO_HOST
    if not accounts:
        return False, REASON_NO_ACCOUNT
    return True, ""


def find_account(candidate: dict, account_id) -> dict:
    """The candidate's own account row for ``account_id``, or ``{}``.

    The API uses this to refuse an account that is not on the system the client
    named, which is the check that stops a caller pairing any managed account with
    any managed system.
    """
    wanted = _int_or_none(account_id)
    if wanted is None:
        return {}
    for account in (candidate or {}).get("accounts") or []:
        if account.get("account_id") == wanted:
            return account
    return {}


def import_request(candidate: dict, *, cloud: str, account_id) -> dict:
    """A ``RegisterDatabaseRequest`` payload for one candidate.

    ``region`` and ``instance_id`` are deliberately left empty even though the
    endpoint accepts them. Password Safe models no cloud region, and
    ``instance_id`` is what ``inventory_service`` uses to build a resource's
    display name — stamping a Password Safe id there makes every imported row read
    "postgres psms:42" on the inventory and Config-Management pages instead of its
    host. Provenance needs its own column.
    """
    account = find_account(candidate, account_id)
    return {
        "engine": candidate.get("engine") or "",
        "cloud": (cloud or "").strip().lower(),
        "host": candidate.get("host") or "",
        "port": candidate.get("port") or None,
        "db_name": candidate.get("db_name") or "",
        "region": "",
        "instance_id": "",
        "managed_account": {
            "system_id": candidate.get("system_id"),
            "account_id": account.get("account_id"),
            # The account's own name, not anything the client sent — it becomes
            # ansible_user at run time, where the ";" suffix form is significant.
            "account_name": account.get("name") or "",
            "uses_ssh_key": bool(account.get("uses_ssh_key")),
        },
    }
