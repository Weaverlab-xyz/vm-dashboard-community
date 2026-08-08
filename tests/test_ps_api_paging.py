"""Tests for the paged inventory read in services/ps_api_service.py.

No network and no Password Safe: the module is imported with a stubbed
``web_dashboard.services.config_service`` and ``web_dashboard.config`` so it can
resolve credentials, and every request goes through a fake client that replays
canned pages.

Runs under pytest, or standalone:  python tests/test_ps_api_paging.py
"""
import asyncio
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    """Import ps_api_service by file path with just enough package scaffolding.

    Its ``_cfg`` reads ``.config_service`` then falls back to ``..config.settings``,
    so both have to exist as importable modules — but neither needs to be the real
    one, and stubbing them keeps this file runnable without SQLAlchemy or FastAPI.
    """
    pkg_web = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg_web.__path__ = [os.path.join(_ROOT, "web_dashboard")]
    pkg_svc = sys.modules.setdefault("web_dashboard.services",
                                     types.ModuleType("web_dashboard.services"))
    pkg_svc.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]

    cfg_stub = types.ModuleType("web_dashboard.services.config_service")
    cfg_stub.get = lambda key: {
        "pscli_api_url": "https://ps.example.com",
        "pscli_client_id": "cid",
        "pscli_client_secret": "secret",
    }.get(key, "")
    sys.modules["web_dashboard.services.config_service"] = cfg_stub

    settings_stub = types.ModuleType("web_dashboard.config")
    settings_stub.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = settings_stub

    path = os.path.join(_ROOT, "web_dashboard", "services", "ps_api_service.py")
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.ps_api_service", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ps = _load()


# ── fakes ───────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    """Stands in for httpx.AsyncClient. Records every call so a test can assert on
    the request sequence, which is the point of most of these tests."""

    def __init__(self, pages=None, flat=None, status=None):
        self.pages = pages or {}      # path -> list[list[dict]] served in order
        self.flat = flat or {}        # path -> list[dict] (unpaged collections)
        self.status = status or {}    # path -> status code override
        self.calls = []               # (method, path, params)
        self.posts = []
        self.headers = {}
        self._served = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, params=None):
        self.calls.append(("GET", path, dict(params or {})))
        code = self.status.get(path, 200)
        if code != 200:
            return FakeResponse(code, {"error": "nope"})
        if path in self.flat:
            return FakeResponse(200, self.flat[path])
        served = self._served.get(path, 0)
        pages = self.pages.get(path, [])
        body = pages[served] if served < len(pages) else []
        self._served[path] = served + 1
        return FakeResponse(200, body)

    async def post(self, path, **kw):
        self.posts.append(path)
        if path == "Auth/Connect/Token":
            return FakeResponse(200, {"access_token": "tok"})
        return FakeResponse(200, {})


def _rows(id_key, start, count):
    return [{id_key: start + i, "Name": f"row{start + i}"} for i in range(count)]


def _full(id_key, start):
    return _rows(id_key, start, ps._PAGE_SIZE)


def _run(coro):
    return asyncio.run(coro)


# ── _page_items: both response shapes ───────────────────────────────────────────

def test_page_items_accepts_a_bare_array():
    assert ps._page_items(FakeResponse(200, [{"a": 1}])) == [{"a": 1}]


def test_page_items_accepts_the_data_envelope():
    resp = FakeResponse(200, {"TotalCount": 1, "Data": [{"a": 1}]})
    assert ps._page_items(resp) == [{"a": 1}]


def test_page_items_survives_a_non_json_body():
    assert ps._page_items(FakeResponse(200, ValueError("not json"))) == []
    assert ps._page_items(FakeResponse(200, "a string")) == []


# ── _get_paged ──────────────────────────────────────────────────────────────────

def test_two_full_pages_then_a_short_one_returns_everything():
    client = FakeClient(pages={"X": [_full("ID", 0), _full("ID", 1000),
                                     _rows("ID", 2000, 7)]})
    out = _run(ps._get_paged(client, "X", id_keys=("ID",)))
    assert len(out) == ps._PAGE_SIZE * 2 + 7
    # limit/offset advance correctly, and it stops on the short page.
    assert [c[2] for c in client.calls] == [
        {"limit": ps._PAGE_SIZE, "offset": 0},
        {"limit": ps._PAGE_SIZE, "offset": ps._PAGE_SIZE},
        {"limit": ps._PAGE_SIZE, "offset": ps._PAGE_SIZE * 2},
    ]


def test_a_single_short_page_costs_one_request():
    client = FakeClient(pages={"X": [_rows("ID", 0, 3)]})
    assert len(_run(ps._get_paged(client, "X", id_keys=("ID",)))) == 3
    assert len(client.calls) == 1


def test_a_tenant_that_ignores_offset_stops_and_does_not_duplicate():
    # The failure this guard exists for: forty copies of page one, which reads
    # downstream as a tenant with forty identical databases.
    page = _full("ID", 0)
    client = FakeClient(pages={"X": [page] * ps._MAX_PAGES})
    out = _run(ps._get_paged(client, "X", id_keys=("ID",)))
    assert len(out) == ps._PAGE_SIZE
    assert len({r["ID"] for r in out}) == ps._PAGE_SIZE
    assert len(client.calls) == 2      # page one, then the repeat that trips it


def test_partially_overlapping_pages_keep_only_the_new_rows():
    client = FakeClient(pages={"X": [_full("ID", 0),
                                     _rows("ID", ps._PAGE_SIZE - 5, 10)]})
    out = _run(ps._get_paged(client, "X", id_keys=("ID",)))
    assert len(out) == ps._PAGE_SIZE + 5
    assert len({r["ID"] for r in out}) == len(out)


def test_max_items_caps_the_walk():
    client = FakeClient(pages={"X": [_full("ID", 0), _full("ID", 1000)]})
    out = _run(ps._get_paged(client, "X", id_keys=("ID",), max_items=42))
    assert len(out) == 42
    assert len(client.calls) == 1


def test_the_page_cap_bounds_a_collection_with_no_recognisable_ids():
    # Condition 3 cannot fire without an id, so _MAX_PAGES is the only bound left.
    client = FakeClient(pages={"X": [[{"Other": i} for i in range(ps._PAGE_SIZE)]] * 60})
    out = _run(ps._get_paged(client, "X", id_keys=("ID",)))
    assert len(client.calls) == ps._MAX_PAGES
    assert len(out) == ps._PAGE_SIZE * ps._MAX_PAGES


def test_an_empty_first_page_returns_nothing():
    client = FakeClient(pages={"X": [[]]})
    assert _run(ps._get_paged(client, "X", id_keys=("ID",))) == []


def test_a_non_200_raises_ps_api_error():
    client = FakeClient(status={"X": 403})
    try:
        _run(ps._get_paged(client, "X", id_keys=("ID",)))
    except ps.PSApiError as exc:
        assert "403" in str(exc)
    else:
        raise AssertionError("expected PSApiError")


def test_alternate_id_keys_are_tried_in_order():
    client = FakeClient(pages={"X": [[{"SystemId": 1}, {"SystemId": 1}, {"SystemId": 2}]]})
    out = _run(ps._get_paged(client, "X", id_keys=("ManagedSystemID", "SystemId")))
    assert [r["SystemId"] for r in out] == [1, 2]


def test_extra_params_ride_along_with_limit_and_offset():
    client = FakeClient(pages={"X": [_rows("ID", 0, 1)]})
    _run(ps._get_paged(client, "X", id_keys=("ID",), params={"workgroupID": "7"}))
    assert client.calls[0][2] == {"workgroupID": "7", "limit": ps._PAGE_SIZE, "offset": 0}


# ── read_database_inventory ─────────────────────────────────────────────────────

def _inventory_client(**kw):
    defaults = dict(
        flat={"Platforms": [{"PlatformID": 1, "Name": "PostgreSQL"}],
              "Workgroups": [{"ID": 7, "Name": "DBs"}]},
        pages={"ManagedSystems": [[{"ManagedSystemID": 1, "WorkgroupID": 7},
                                   {"ManagedSystemID": 2, "WorkgroupID": 9}]],
               "Databases": [[{"DatabaseID": 100}]],
               "ManagedAccounts": [[{"ManagedAccountID": 50, "SystemId": 1}]]},
    )
    defaults.update(kw)
    return FakeClient(**defaults)


def _read(client, **kw):
    ps._list_client = lambda: client
    return _run(ps.read_database_inventory(**kw))


def test_the_inventory_read_signs_in_once_for_every_collection():
    # Every other public function here does a full Token + SignAppIn + Signout.
    # Repeating that per collection is twelve extra round trips for one modal open.
    client = _inventory_client()
    out = _read(client)
    assert client.posts.count("Auth/Connect/Token") == 1
    assert client.posts.count("Auth/SignAppIn") == 1
    assert client.posts.count("Auth/Signout") == 1
    assert {"platforms", "systems", "databases", "accounts",
            "workgroup_id", "warnings"} == set(out)
    assert len(out["platforms"]) == 1 and len(out["accounts"]) == 1


def test_no_workgroup_filter_reads_every_system():
    out = _read(_inventory_client())
    assert [s["ManagedSystemID"] for s in out["systems"]] == [1, 2]
    assert out["workgroup_id"] is None


def test_a_workgroup_filter_is_applied_even_if_the_tenant_ignores_it():
    # The fake serves both systems regardless of the query param, standing in for a
    # deployment that does not honour workgroupID. The client-side pass is what
    # makes the filter mean something.
    client = _inventory_client()
    out = _read(client, workgroup="DBs")
    assert [s["ManagedSystemID"] for s in out["systems"]] == [1]
    assert out["workgroup_id"] == "7"
    systems_call = [c for c in client.calls if c[1] == "ManagedSystems"][0]
    assert systems_call[2]["workgroupID"] == "7"     # and still sent as a hint


def test_a_numeric_workgroup_needs_no_lookup():
    client = _inventory_client()
    out = _read(client, workgroup="7")
    assert out["workgroup_id"] == "7"
    assert not [c for c in client.calls if c[1] == "Workgroups"]


def test_losing_the_databases_collection_degrades_to_a_warning():
    client = _inventory_client(status={"Databases": 403})
    out = _read(client)
    assert out["databases"] == []
    assert out["systems"]                      # the import still works
    assert any("Databases" in w for w in out["warnings"])


def test_losing_the_accounts_collection_degrades_to_a_warning_naming_the_fix():
    client = _inventory_client(status={"ManagedAccounts": 403})
    out = _read(client)
    assert out["accounts"] == []
    assert any("Requestor" in w for w in out["warnings"])


def test_losing_managed_systems_is_fatal():
    # Nothing to show, so failing loudly beats an empty list that reads as
    # "Password Safe knows about no databases".
    client = _inventory_client(status={"ManagedSystems": 500})
    try:
        _read(client)
    except ps.PSApiError:
        pass
    else:
        raise AssertionError("expected PSApiError")


def test_the_session_is_signed_out_even_when_a_read_fails():
    client = _inventory_client(status={"ManagedSystems": 500})
    try:
        _read(client)
    except ps.PSApiError:
        pass
    assert "Auth/Signout" in client.posts


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
    sys.exit(1 if failures else 0)
