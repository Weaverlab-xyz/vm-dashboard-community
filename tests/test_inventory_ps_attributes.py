"""/inventory's Password Safe column: the gate, the envelope, and who sees the orphans.

`ps_attribute_catalog` decides what matches; this file covers the half that talks to
Password Safe and to the caller. Four properties:

1. **Password Safe off costs nothing.** The flag is off on most installs, and a page that
   made a cross-product API call anyway would be paying for a feature nobody enabled.
2. **The orphan list is admin-only, decided by the SERVER.** `/inventory` is
   workgroup-filtered rather than admin-gated, and an orphan is by definition a record the
   row filter cannot vet — listing one to a non-admin discloses a machine they are not
   entitled to see on the page above it. Hiding it in the template would be a promise
   about one page; omitting the key is a property of the API.
3. **Unreachable Password Safe costs the column and nothing else.** /inventory is
   otherwise a database read; a tenant being down must not take the page with it.
4. **Attributes are read for MATCHED objects only.** The read is per object, so this is
   what keeps a page load proportional to the page rather than to the tenant — an estate
   of 800 records and 60 VMs does 60 reads, not 800.

Runs under pytest, or standalone:  python tests/test_inventory_ps_attributes.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "invps.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# Probe the app deps BY NAME; anything else must propagate. A blanket
# `except Exception: skip` would let this file exit 0 having tested nothing —
# see tests/test_import_guard_narrowness.py.
try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.api import inventory as inv_api
from web_dashboard.database import Base, engine
from web_dashboard.services import ps_api_service

# `_ps_workgroup` reads app_config, so the tables have to exist — without them the read
# raises inside the cached fetcher and every case below reports `error`, which would look
# like a real failure of the feature rather than of the fixture.
Base.metadata.create_all(bind=engine)


class _Recorder:
    """Stands in for read_attribute_inventory, counting calls and what was asked for."""

    def __init__(self, assets=(), systems=(), types=(), attributes=None):
        self.assets, self.systems, self.types = list(assets), list(systems), list(types)
        self.attributes = attributes or {}
        self.calls, self.wanted = 0, []

    async def __call__(self, *, workgroup="", wanted=None, tenant=None):
        self.calls += 1
        self.wanted.append(set(wanted or ()))
        return {
            "assets": {"state": "ok", "rows": self.assets, "detail": ""},
            "managed_systems": {"state": "ok", "rows": self.systems, "detail": ""},
            "attribute_types": {"state": "ok", "rows": self.types, "detail": ""},
            "attributes": {ref: {"state": "ok", "rows": rows}
                           for ref, rows in self.attributes.items()
                           if wanted is None or ref in wanted},
            "truncated": False, "reachable": True, "detail": "",
        }


def _run(items, *, is_admin=True, enabled=True, configured=True,
         reader=None, rows=None):
    """Drive _attach_ps_attributes with the tenant and the DB collect() replaced."""
    reader = reader or _Recorder()
    real = (inv_api._ps_enabled, ps_api_service.configured,
            ps_api_service.read_attribute_inventory, inv_api.inventory_service.collect)
    inv_api._ps_enabled = lambda: enabled
    ps_api_service.configured = lambda: configured
    ps_api_service.read_attribute_inventory = reader
    inv_api.inventory_service.collect = lambda db: list(rows if rows is not None else items)
    # The snapshot is cached; a stale entry from a previous case would hide a regression.
    asyncio.run(inv_api.cache_service.invalidate_prefix("ps_attributes"))
    try:
        env = asyncio.run(inv_api._attach_ps_attributes(items, is_admin=is_admin))
    finally:
        (inv_api._ps_enabled, ps_api_service.configured,
         ps_api_service.read_attribute_inventory, inv_api.inventory_service.collect) = real
    return env, reader


_ASSET = {"AssetID": 12, "AssetName": "WEB-1", "IPAddress": "10.0.0.4"}
_TYPES = [{"AttributeTypeID": 3, "Name": "Criticality"}]
_ATTRS = {"asset:12": [{"AttributeTypeID": 3, "ShortName": "High"}]}


# ── 1. the gate ──────────────────────────────────────────────────────────────

def test_password_safe_off_makes_no_call_at_all():
    items = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.0.0.4"]}]
    env, reader = _run(items, enabled=False)
    assert env == {"state": "off"}
    assert reader.calls == 0, "the tenant was called with the feature switched off"
    assert "ps" not in items[0]


def test_enabled_but_unconfigured_says_so_once_rather_than_per_row():
    items = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.0.0.4"]}]
    env, reader = _run(items, configured=False)
    assert env["state"] == "unconfigured" and reader.calls == 0
    assert "ps" not in items[0], "a per-row state would repeat one tenant fact forty times"


# ── 2. the orphan list is the server's decision ──────────────────────────────

def test_an_admin_sees_the_records_that_match_nothing():
    items = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.9.9.9"]}]
    env, _ = _run(items, is_admin=True,
                  reader=_Recorder(assets=[_ASSET], types=_TYPES, attributes=_ATTRS))
    assert [o["name"] for o in env["orphans"]] == ["WEB-1"]


def test_a_non_admin_is_not_told_those_records_exist():
    """Not hidden in the template — ABSENT from the payload. /inventory is workgroup
    filtered, and an orphan is precisely a record that filter cannot vet."""
    items = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.9.9.9"]}]
    env, _ = _run(items, is_admin=False,
                  reader=_Recorder(assets=[_ASSET], types=_TYPES, attributes=_ATTRS))
    assert "orphans" not in env


# ── 3. failure costs the column, not the page ────────────────────────────────

def test_an_unreachable_tenant_leaves_the_rows_alone():
    async def boom(*, workgroup="", wanted=None, tenant=None):
        raise RuntimeError("appliance unreachable")

    items = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.0.0.4"]}]
    env, _ = _run(items, reader=boom)
    assert env["state"] == "error"
    assert "ps" not in items[0]


def test_the_error_detail_carries_no_tenant_response_text():
    """A Password Safe error can quote a response body; it is logged whole and only a
    fixed string travels outward (CodeQL py/stack-trace-exposure)."""
    async def boom(*, workgroup="", wanted=None, tenant=None):
        raise RuntimeError("SECRET-TENANT-DETAIL")

    env, _ = _run([{"cloud": "aws", "kind": "vm", "name": "a", "ips": []}], reader=boom)
    assert "SECRET-TENANT-DETAIL" not in env.get("detail", "")


# ── 4. the read is proportional to the page ──────────────────────────────────

def test_attributes_are_read_only_for_objects_that_matched():
    """The whole performance story. The second pass asks for exactly the matched refs."""
    unrelated = [{"AssetID": i, "AssetName": f"x{i}", "IPAddress": f"10.5.0.{i}"}
                 for i in range(1, 40)]
    reader = _Recorder(assets=[_ASSET] + unrelated, types=_TYPES, attributes=_ATTRS)
    items = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.0.0.4"]}]
    _run(items, reader=reader)
    assert reader.wanted[-1] == {"asset:12"}, reader.wanted


def test_nothing_matched_means_no_second_pass():
    reader = _Recorder(assets=[_ASSET], types=_TYPES)
    _run([{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.9.9.9"]}], reader=reader)
    assert reader.calls == 1


def test_a_matched_row_gets_its_attributes_as_chips():
    items = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.0.0.4"]}]
    _run(items, reader=_Recorder(assets=[_ASSET], types=_TYPES, attributes=_ATTRS))
    ps = items[0]["ps"]
    assert ps["state"] == "ok" and ps["basis"] == "ip"
    assert [(c["key"], c["value"]) for c in ps["attributes"]] == [("Criticality", "High")]


def test_the_match_runs_against_every_row_not_just_the_callers_view():
    """The orphan list means "matches no VM", which cannot be answered from one caller's
    workgroup-filtered subset — so the snapshot matches against the whole inventory."""
    visible = [{"cloud": "aws", "kind": "vm", "name": "mine", "ips": ["10.9.9.9"]}]
    everything = visible + [{"cloud": "aws", "kind": "vm", "name": "theirs",
                             "ips": ["10.0.0.4"]}]
    reader = _Recorder(assets=[_ASSET], types=_TYPES, attributes=_ATTRS)
    env, _ = _run(visible, reader=reader, rows=everything)
    assert env.get("orphans") == [], "a row outside the caller's view still claims it"


def test_the_shared_cached_list_is_never_mutated():
    """`raw` is shared across callers for the TTL; the route copies before enriching.
    This asserts the property the copy protects — the same one test_inventory_cloud_tags
    pins for tags."""
    shared = [{"cloud": "aws", "kind": "vm", "name": "a", "ips": ["10.0.0.4"]}]
    copies = [dict(i) for i in shared]
    _run(copies, reader=_Recorder(assets=[_ASSET], types=_TYPES, attributes=_ATTRS))
    assert "ps" in copies[0] and "ps" not in shared[0]


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
