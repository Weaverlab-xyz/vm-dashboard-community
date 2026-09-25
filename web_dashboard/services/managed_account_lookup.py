"""Live Password Safe managed-account lists for the Config-Management run forms —
**ids and names only, never credentials**.

Two callers with one body:

* :func:`lookup_host` — one host, the single-run picker. Behaviour identical to what
  ``api/config_mgmt.list_managed_accounts`` did inline before this module existed.
* :func:`lookup_targets` — every target of a BULK run, answered from ONE estate read.

The split matters for a reason that is not obvious. ``btapi_service``'s per-host
lookup falls back to a full ``ps-cli managed-systems list`` whenever the name hint is
absent or misses, and every ``_ps_run`` is a subprocess with a 60s timeout. Calling it
once per target for a 50-host batch is up to 50 full-estate listings serialised
through ``asyncio.to_thread`` — minutes, and the HTTP request times out. So the batch
path reads the estate once and narrows locally with
``managed_accounts.select_systems``, which shares its precedence with the per-host
path so the two cannot disagree about which system a host is.

Neither function raises for a Password Safe failure. A lookup is a *convenience* over
the run itself: the run resolves its own credential at dispatch time and fails with a
specific message. A picker that 500s would take the whole form down for one
unreachable host, so failures come back as an ``error`` note beside an empty list —
and in the batch case that note is PER TARGET, so one bad host cannot blank the table.
"""
import logging

logger = logging.getLogger(__name__)

# Returned instead of the real ps-cli error. A raw BTAPIError string carries ps-cli
# stderr, so surfacing it to the caller leaks internal detail (CodeQL
# py/stack-trace-exposure). The real text is logged server-side.
LOOKUP_ERROR = ("Password Safe lookup failed — check the BeyondTrust configuration "
                "and server logs.")

# Ceiling on managed-account fetches for ONE batch request, mirroring
# ps_attribute_catalog.MAX_ATTRIBUTE_FETCHES. A selection whose hosts fan out to
# hundreds of managed systems is a misconfiguration (an IP that matches half the
# estate, say), and the answer is to say so — a silently shorter list would read as
# "this host has no accounts" and send the operator hunting in Password Safe.
MAX_ACCOUNT_FETCHES = 200


def _system_id(system: dict):
    """The id from a raw ps-cli managed-system row, across its field-name variants."""
    sid = (system.get("ManagedSystemID") or system.get("SystemId")
           or system.get("SystemID"))
    return None if sid is None else int(sid)


class _Batch:
    """What every target of one request shares: the per-system account cache, the
    fetch ceiling, and the lazily-read whole-tenant account list.

    All three exist for the same reason — ps-cli is a SUBPROCESS per call, so anything
    read once per host over a fifty-host fleet is fifty processes at a 60s timeout.
    """

    def __init__(self, limit: int = MAX_ACCOUNT_FETCHES):
        self.cache = {}          # system_id → raw accounts
        self.left = limit
        self.truncated = False
        self.all_accounts = None  # whole-tenant list, read at most once, on demand

    def take(self) -> bool:
        if self.left <= 0:
            self.truncated = True
            return False
        self.left -= 1
        return True

    async def accounts_for_system(self, sid: int) -> list:
        from . import btapi_service

        if sid in self.cache:
            return self.cache[sid]
        if not self.take():
            return []
        # Read the whole-tenant list ONCE, before the first per-system call that might
        # need it. The per-system verb only returns locally-managed accounts and falls
        # back to a full list-accounts for domain-linked ones — per host, that fallback
        # is the same full-estate trap as the managed-system listing.
        if self.all_accounts is None:
            try:
                self.all_accounts = await btapi_service.list_ps_managed_accounts_all()
            except btapi_service.BTAPIError as exc:
                logger.warning("whole-tenant managed-account read failed, falling back "
                               "to per-system lookups: %s", exc)
                self.all_accounts = []
        self.cache[sid] = await btapi_service.list_ps_managed_accounts_with_fallback(
            sid, self.all_accounts or None)
        return self.cache[sid]


async def _accounts_for(systems: list, batch: "_Batch") -> dict:
    """``{system_id: raw accounts}`` for these systems, fetching each system at most
    once per request. Two hosts on one managed system is common (a name match that
    did not narrow), and it must not cost two ps-cli calls."""
    out = {}
    for s in systems or []:
        sid = _system_id(s)
        if sid is None:
            continue
        out[sid] = await batch.accounts_for_system(sid)
    return out


async def _systems_for(host: str, name: str, estate) -> list:
    """The managed systems for one host — from an already-read estate when the caller
    has one (batch), else by asking ps-cli about this host alone (single)."""
    from . import btapi_service, managed_accounts as ma

    ip, name = ma.lookup_args(host, name)
    if estate is None:
        return await btapi_service.list_ps_managed_systems_by_ip_or_name(ip, name)
    return ma.select_systems(estate, ip, name)


async def lookup_host(host: str, name: str = "", *, estate=None, batch=None) -> dict:
    """``{"enabled", "ephemeral_enabled", "systems": [...]}`` for one host, plus an
    ``"error"`` key when the lookup failed.

    ``estate`` / ``batch`` belong to the batch caller; omitted, this is the ordinary
    one-host path and each call stands alone.
    """
    from . import btapi_service, config_service as cs, managed_accounts as ma

    # ephemeral_enabled tells the UI that managed accounts can run on ECS/GCP (via the
    # ephemeral store copy) and to nudge on change-after-release for those.
    ephemeral_enabled = cs.get_bool("ansible_cloud_ephemeral_secrets_enabled")
    base = {"enabled": True, "ephemeral_enabled": ephemeral_enabled, "systems": []}
    if not cs.get_bool("password_safe_enabled"):
        return {**base, "enabled": False}

    host = (host or "").strip()
    if not host:
        return base

    if batch is None:
        batch = _Batch()

    try:
        systems = await _systems_for(host, name, estate)
        accounts_by_system = await _accounts_for(systems, batch)
    except btapi_service.BTAPIError as exc:
        logger.warning("managed-account lookup for %r failed: %s", host, exc)
        return {**base, "error": LOOKUP_ERROR}
    return {**base, "systems": ma.normalize_managed_systems(systems, accounts_by_system)}


async def lookup_targets(targets: list, *, default_account_name: str = "",
                         default_become_name: str = "") -> dict:
    """Per-target managed-account lists for a whole bulk selection.

    ``targets`` is ``[{"inventory_id", "name", "host", "ps_system_id"}]``, built by the
    caller from its OWN rows — never from anything the browser supplied, which is the
    point of resolving the selection server-side.

    Returns ``{"enabled", "ephemeral_enabled", "truncated", "targets": [...]}`` where
    each target carries its own ``systems``, ``suggested_key`` /
    ``suggested_basis`` / ``suggested_become_key`` and its own ``error``.

    The suggestion key is ``"{system_id}:{account_id}"`` — the same composite the
    single-run picker's ``<option value>`` uses, so the browser parses one format.
    """
    from . import config_service as cs, btapi_service, managed_accounts as ma

    ephemeral_enabled = cs.get_bool("ansible_cloud_ephemeral_secrets_enabled")
    if not cs.get_bool("password_safe_enabled"):
        return {"enabled": False, "ephemeral_enabled": ephemeral_enabled,
                "truncated": False, "targets": []}

    # ONE estate read for the whole batch — see the module docstring. A failure here is
    # not fatal: estate=None makes every target fall back to its own per-host lookup,
    # which is slower but correct, and a small selection will not notice.
    estate = None
    try:
        estate = await btapi_service.list_ps_managed_systems_all()
    except btapi_service.BTAPIError as exc:
        logger.warning("batch managed-system estate read failed, falling back to "
                       "per-host lookups: %s", exc)

    batch = _Batch()
    out = []
    for t in targets or []:
        host = (t.get("host") or "").strip()
        result = await lookup_host(host, t.get("name") or "",
                                   estate=estate, batch=batch)
        systems = result.get("systems") or []
        ref, basis = ma.suggest_account(
            systems, default_account_name, t.get("ps_system_id") or "")
        become_ref = ma.find_account_by_name(systems, default_become_name)
        out.append({
            "inventory_id": t.get("inventory_id") or "",
            "name": t.get("name") or "",
            "host": host,
            "systems": systems,
            "suggested_key": _key(ref),
            "suggested_basis": basis,
            "suggested_become_key": _key(become_ref),
            "error": result.get("error") or "",
        })
    return {"enabled": True, "ephemeral_enabled": ephemeral_enabled,
            "truncated": batch.truncated, "targets": out}


def _key(ref) -> str:
    """The picker's composite ``"{system_id}:{account_id}"``, or ``""``."""
    if not ref:
        return ""
    return f"{ref['system_id']}:{ref['account_id']}"
