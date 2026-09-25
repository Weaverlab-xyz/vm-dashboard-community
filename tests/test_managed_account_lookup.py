"""Unit tests: services/managed_account_lookup — the Password Safe account lists the
Config-Management run forms are built from.

The thing worth testing here is not the shape of the response; it is HOW MANY ps-cli
calls a bulk run costs. ps-cli is a subprocess with a 60s timeout, and two of its
lookups fall back to reading the whole tenant:

  * the per-host managed-SYSTEM lookup falls back to a full ``managed-systems list``
    whenever the name hint is absent or misses;
  * the per-system managed-ACCOUNT lookup falls back to a full ``list-accounts``
    whenever the system has no locally-managed accounts (which is how domain-linked
    accounts are found at all).

Done per target, a 50-host batch is up to a hundred whole-tenant reads and the HTTP
request times out long before the operator sees a table. So the call COUNTS below are
the real assertions; the rest is the contract they have to keep while being cheap.

btapi_service and config_service are stubbed — no ps-cli, no Password Safe, no DB.
Runs under pytest, or standalone:
    python tests/test_managed_account_lookup.py
"""
import asyncio
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {"password_safe_enabled": True, "ansible_cloud_ephemeral_secrets_enabled": False}
CALLS = []


class _StubBTAPIError(Exception):
    pass


# Two systems share the name DC01 (different workgroups); ssm-box is the
# plugin-onboarded shape — registered by name with a placeholder IP.
ESTATE = [
    {"ManagedSystemID": 1, "Name": "web-01", "IPAddress": "10.0.0.20"},
    {"ManagedSystemID": 2, "Name": "DC01", "IPAddress": "10.0.0.10"},
    {"ManagedSystemID": 3, "Name": "DC01", "IPAddress": "10.0.0.11"},
    {"ManagedSystemID": 4, "Name": "ssm-box", "IPAddress": "127.0.0.1"},
]
ACCOUNTS = {
    1: [{"ManagedAccountID": 10, "AccountName": "svc-ansible"}],
    2: [{"ManagedAccountID": 20, "AccountName": "root"}],
    3: [{"ManagedAccountID": 30, "AccountName": "root"}],
    4: [{"ManagedAccountID": 40, "AccountName": "adminuser;local"}],
}

FAIL_ESTATE = [False]
FAIL_HOST = [None]        # host string whose per-host lookup should raise


def _install_stubs():
    bt = types.ModuleType("web_dashboard.services.btapi_service")
    bt.BTAPIError = _StubBTAPIError

    async def list_all():
        CALLS.append(("systems_all",))
        if FAIL_ESTATE[0]:
            raise _StubBTAPIError("estate boom")
        return list(ESTATE)

    async def by_ip_or_name(ip, name):
        CALLS.append(("systems_host", ip, name))
        if FAIL_HOST[0] is not None and ip == FAIL_HOST[0]:
            raise _StubBTAPIError("host boom")
        hit = [s for s in ESTATE if s["IPAddress"] == ip
               or (name and s["Name"] == name)]
        return hit

    async def accounts_all():
        CALLS.append(("accounts_all",))
        return []

    async def accounts_for(sid, all_accounts=None):
        CALLS.append(("accounts", sid))
        return list(ACCOUNTS.get(sid, []))

    bt.list_ps_managed_systems_all = list_all
    bt.list_ps_managed_systems_by_ip_or_name = by_ip_or_name
    bt.list_ps_managed_accounts_all = accounts_all
    bt.list_ps_managed_accounts_with_fallback = accounts_for
    sys.modules["web_dashboard.services.btapi_service"] = bt

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    cfg.get = lambda key: CONF.get(key, "")
    sys.modules["web_dashboard.services.config_service"] = cfg

    # managed_accounts is pure; load it under the package name the module imports.
    path = os.path.join(_ROOT, "web_dashboard", "services", "managed_accounts.py")
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.managed_accounts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["web_dashboard.services.managed_accounts"] = mod

    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = [os.path.join(_ROOT, "web_dashboard")]
    svcs = sys.modules.setdefault("web_dashboard.services",
                                  types.ModuleType("web_dashboard.services"))
    svcs.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]


_install_stubs()

_LOOKUP = os.path.join(_ROOT, "web_dashboard", "services", "managed_account_lookup.py")
_spec = importlib.util.spec_from_file_location(
    "web_dashboard.services.managed_account_lookup", _LOOKUP)
mal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mal)


def _reset():
    CALLS.clear()
    FAIL_ESTATE[0] = False
    FAIL_HOST[0] = None
    CONF["password_safe_enabled"] = True


def _targets(*specs):
    return [{"inventory_id": i, "name": n, "host": h, "ps_system_id": p}
            for i, n, h, p in specs]


def _count(kind):
    return len([c for c in CALLS if c[0] == kind])


# ── the cost of a batch ─────────────────────────────────────────────────────────

def test_a_batch_reads_the_estate_once_not_once_per_target():
    """The assertion this module exists for."""
    _reset()
    tg = _targets(("job:1", "web-01", "10.0.0.20", ""),
                  ("job:2", "DC01", "10.0.0.10", ""),
                  ("job:3", "DC01", "10.0.0.11", ""))
    asyncio.run(mal.lookup_targets(tg))
    assert _count("systems_all") == 1, CALLS
    # And never the per-host lookup, which is the expensive path being avoided.
    assert _count("systems_host") == 0, CALLS


def test_one_system_matched_by_two_targets_is_fetched_once():
    """Two hosts can resolve to the same managed system (a name match that did not
    narrow). Accounts for it must not be read twice."""
    _reset()
    tg = _targets(("job:1", "web-01", "10.0.0.20", ""),
                  ("job:2", "web-01", "10.0.0.20", ""))
    asyncio.run(mal.lookup_targets(tg))
    assert _count("accounts") == 1, CALLS


def test_the_whole_tenant_account_list_is_read_at_most_once():
    _reset()
    tg = _targets(("job:1", "web-01", "10.0.0.20", ""),
                  ("job:2", "DC01", "10.0.0.10", ""))
    asyncio.run(mal.lookup_targets(tg))
    assert _count("accounts_all") <= 1, CALLS


def test_the_account_fetch_budget_is_per_request_and_reports_truncation():
    """A selection that fans out to more systems than the ceiling must SAY so — a
    silently shorter list reads as 'this host is not onboarded'."""
    _reset()
    mal.MAX_ACCOUNT_FETCHES  # documented constant, referenced so a rename is noticed
    batch = mal._Batch(limit=2)
    asyncio.run(batch.accounts_for_system(1))
    asyncio.run(batch.accounts_for_system(2))
    assert batch.truncated is False
    assert asyncio.run(batch.accounts_for_system(3)) == []
    assert batch.truncated is True


# ── falling back when the estate cannot be read ────────────────────────────────

def test_an_unreadable_estate_falls_back_to_per_host_lookups():
    """Slower, but correct — and it must not blank the whole table."""
    _reset()
    FAIL_ESTATE[0] = True
    tg = _targets(("job:1", "web-01", "10.0.0.20", ""),
                  ("job:2", "DC01", "10.0.0.10", ""))
    out = asyncio.run(mal.lookup_targets(tg))
    assert _count("systems_host") == 2, CALLS
    assert [t["name"] for t in out["targets"]] == ["web-01", "DC01"]
    assert out["targets"][0]["systems"], "per-host fallback returned nothing"


def test_one_hosts_failure_does_not_blank_the_others():
    """The error is PER TARGET. One unreachable host must not cost the operator the
    whole table."""
    _reset()
    FAIL_ESTATE[0] = True
    FAIL_HOST[0] = "10.0.0.20"
    tg = _targets(("job:1", "web-01", "10.0.0.20", ""),
                  ("job:2", "DC01", "10.0.0.10", ""))
    out = asyncio.run(mal.lookup_targets(tg))
    first, second = out["targets"]
    assert first["error"] == mal.LOOKUP_ERROR and first["systems"] == []
    assert second["error"] == "" and second["systems"]


def test_the_error_text_never_carries_the_ps_cli_message():
    """A raw BTAPIError string carries ps-cli stderr — returning it leaks internal
    detail to the caller (CodeQL py/stack-trace-exposure)."""
    _reset()
    FAIL_ESTATE[0] = True
    FAIL_HOST[0] = "10.0.0.20"
    out = asyncio.run(mal.lookup_targets(_targets(("job:1", "web-01", "10.0.0.20", ""))))
    assert "boom" not in out["targets"][0]["error"]


# ── suggestions ────────────────────────────────────────────────────────────────

def test_the_default_name_pre_selects_the_matching_account_per_target():
    _reset()
    tg = _targets(("job:1", "web-01", "10.0.0.20", ""),
                  ("job:2", "DC01", "10.0.0.10", ""))
    out = asyncio.run(mal.lookup_targets(tg, default_account_name="root"))
    by_id = {t["inventory_id"]: t for t in out["targets"]}
    # web-01 has no 'root' — it falls to the only-account tier, and says so.
    assert by_id["job:1"]["suggested_key"] == "1:10"
    assert by_id["job:1"]["suggested_basis"] == "only-account"
    assert by_id["job:2"]["suggested_key"] == "2:20"
    assert by_id["job:2"]["suggested_basis"] == "default-name"


def test_a_recorded_system_id_disambiguates_a_shared_name():
    """Two systems named DC01. Without the recorded id there is nothing to choose
    between them; with it, the row this VM was onboarded into wins."""
    _reset()
    out = asyncio.run(mal.lookup_targets(_targets(("job:1", "DC01", "10.9.9.9", "3"))))
    t = out["targets"][0]
    assert len(t["systems"]) == 2, "the ambiguity should be surfaced, not resolved away"
    assert t["suggested_key"] == "3:30"
    assert t["suggested_basis"] == "recorded-system"


def test_a_target_with_no_accounts_suggests_nothing_and_is_not_an_error():
    """This is the amber row in the picker: no match, but the batch still runs."""
    _reset()
    out = asyncio.run(mal.lookup_targets(_targets(("job:1", "nope", "10.9.9.9", ""))))
    t = out["targets"][0]
    assert t["systems"] == [] and t["suggested_key"] == "" and t["error"] == ""


def test_the_suggested_key_is_the_same_composite_the_picker_parses():
    _reset()
    out = asyncio.run(mal.lookup_targets(_targets(("job:1", "web-01", "10.0.0.20", ""))))
    sid, aid = out["targets"][0]["suggested_key"].split(":")
    assert (int(sid), int(aid)) == (1, 10)


def test_the_plugin_onboarded_suffix_form_matches_a_plain_name():
    """Cloud-native onboarding registers `{user};{suffix}`; picking `adminuser` has to
    match `adminuser;local` or the account looks absent."""
    _reset()
    out = asyncio.run(mal.lookup_targets(
        _targets(("job:1", "ssm-box", "10.99.1.7", "")),
        default_account_name="adminuser"))
    assert out["targets"][0]["suggested_basis"] == "default-name"


# ── the single-host path keeps its contract ────────────────────────────────────

def test_lookup_host_shape_and_password_safe_off():
    _reset()
    out = asyncio.run(mal.lookup_host("10.0.0.20", "web-01"))
    assert out["enabled"] is True and out["systems"][0]["system_id"] == 1
    assert "ephemeral_enabled" in out

    CONF["password_safe_enabled"] = False
    off = asyncio.run(mal.lookup_host("10.0.0.20", "web-01"))
    assert off == {"enabled": False, "ephemeral_enabled": False, "systems": []}


def test_lookup_host_with_no_host_costs_no_ps_cli_call():
    _reset()
    out = asyncio.run(mal.lookup_host("  "))
    assert out["systems"] == [] and CALLS == []


def test_lookup_targets_is_empty_for_a_disabled_tenant():
    _reset()
    CONF["password_safe_enabled"] = False
    out = asyncio.run(mal.lookup_targets(_targets(("job:1", "web-01", "10.0.0.20", ""))))
    assert out["enabled"] is False and out["targets"] == [] and CALLS == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
