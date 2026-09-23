"""/inventory's Tags column on a CLOUD row — the join, and the two ways it can go wrong.

A cloud row on /inventory is projected from a deploy Job, and a Job records what the
dashboard ASKED for, never what the provider holds now. So those rows carry no tags of
their own and the Tags column would be empty for most of an estate. `_attach_cloud_tags`
closes that by reading the tags already sitting in each cloud's instance cache.

Two properties, and the second is the one that would hurt:

1. **The join is case-insensitive.** AWS tag values are case-sensitive, Azure resource
   names are not, and GCE names are lowercase by rule — so the deploy Job's spelling and
   the live listing's spelling routinely differ in case. An exact match silently finds
   nothing, which looks exactly like "this VM has no tags".
2. **The shared cached list must not be mutated.** `get_or_refresh` hands every caller
   the SAME list of dicts. Writing enrichment into those dicts leaks one caller's view
   into every other caller's rows for the rest of the TTL — and because /inventory is
   workgroup-filtered per request, that is a cross-tenant leak, not a cosmetic bug.

Also pinned: the join NEVER makes a cloud call. /inventory is a cheap DB aggregation and
adding four cloud fan-outs to it would be a different page.

Runs under pytest, or standalone:  python tests/test_inventory_cloud_tags.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "invtags.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# The only legitimate reason to skip is a bare interpreter with no app deps, so probe
# for those BY NAME and let every other ImportError propagate as a failure. A blanket
# `except Exception: skip` around the first-party import would let this file exit 0
# having tested nothing the first time api/inventory grew an import a stub cannot
# satisfy — see tests/test_import_guard_narrowness.py, which enforces exactly this.
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


CHIPS = [{"key": "env", "value": "prod", "cls": "user", "tone": 1, "title": "AWS tag"}]


class _FakeCloud:
    """Stands in for api/aws.py etc. Counts calls so a fetch-per-row shows up."""
    def __init__(self, mapping, boom=False):
        self.mapping, self.boom, self.calls = mapping, boom, 0

    async def cached_tags_by_name(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("provider not configured")
        return self.mapping


def _run(items, clouds):
    """Drive _attach_cloud_tags with the four cloud modules replaced."""
    import web_dashboard.api.aws, web_dashboard.api.azure
    import web_dashboard.api.gcp, web_dashboard.api.oci
    real = {}
    mods = {"aws": web_dashboard.api.aws, "azure": web_dashboard.api.azure,
            "gcp": web_dashboard.api.gcp, "oci": web_dashboard.api.oci}
    for name, mod in mods.items():
        real[name] = mod.cached_tags_by_name
        stub = clouds.get(name) or _FakeCloud({})
        mod.cached_tags_by_name = stub.cached_tags_by_name
    try:
        asyncio.run(inv_api._attach_cloud_tags(items))
    finally:
        for name, mod in mods.items():
            mod.cached_tags_by_name = real[name]
    return items


def test_a_cloud_row_gains_the_tags_from_its_clouds_cache():
    items = [{"cloud": "aws", "kind": "vm", "name": "web-1"}]
    _run(items, {"aws": _FakeCloud({"web-1": CHIPS})})
    assert [c["key"] for c in items[0]["tags"]] == ["env"]


def test_the_join_is_case_insensitive_on_the_name():
    """The Job says `Web-1`, the live listing says `web-1`. An exact match finds
    nothing and the column just looks empty."""
    items = [{"cloud": "aws", "kind": "vm", "name": "Web-1"}]
    _run(items, {"aws": _FakeCloud({"web-1": CHIPS})})
    assert items[0].get("tags"), "a case difference lost the tags"


def test_a_row_with_no_match_is_left_alone():
    items = [{"cloud": "aws", "kind": "vm", "name": "orphan"}]
    _run(items, {"aws": _FakeCloud({"web-1": CHIPS})})
    assert not items[0].get("tags")


def test_a_row_that_already_has_tags_is_not_overwritten():
    """A synced hypervisor row carries its own, and they are the truth for it."""
    own = [{"key": "prod", "value": None, "cls": "user", "tone": 0, "title": "Proxmox tag"}]
    items = [{"cloud": "proxmox", "kind": "vm", "name": "web-1", "tags": own}]
    _run(items, {"aws": _FakeCloud({"web-1": CHIPS})})
    assert items[0]["tags"] == own


def test_a_non_vm_row_is_not_joined_against_the_vm_cache():
    """A database and a VM can share a name; the VM cache says nothing about the db."""
    items = [{"cloud": "aws", "kind": "database", "name": "web-1"}]
    _run(items, {"aws": _FakeCloud({"web-1": CHIPS})})
    assert not items[0].get("tags")


def test_one_unconfigured_cloud_does_not_cost_the_others_their_tags():
    items = [{"cloud": "azure", "kind": "vm", "name": "az-1"},
             {"cloud": "aws", "kind": "vm", "name": "web-1"}]
    _run(items, {"azure": _FakeCloud({}, boom=True), "aws": _FakeCloud({"web-1": CHIPS})})
    assert not items[0].get("tags")
    assert items[1].get("tags"), "an Azure failure took AWS's tags with it"


def test_the_cache_is_read_once_per_cloud_not_once_per_row():
    """A read per row would turn a page-sized join into an estate-sized one."""
    stub = _FakeCloud({f"web-{i}": CHIPS for i in range(50)})
    items = [{"cloud": "aws", "kind": "vm", "name": f"web-{i}"} for i in range(50)]
    _run(items, {"aws": stub})
    assert stub.calls == 1, f"read the cache {stub.calls} times for 50 rows"


def test_a_cloud_with_no_rows_is_not_consulted_at_all():
    stub = _FakeCloud({})
    _run([{"cloud": "proxmox", "kind": "vm", "name": "pve-1", "tags": []}], {"aws": stub})
    assert stub.calls == 0


def test_the_helper_never_reaches_a_cloud_sdk():
    """Pinned as source text: every module it consults must be asked for the CACHE.

    /inventory makes no cloud call today, and the Tags column must not be the thing that
    changes that — a page that silently became four fan-outs would still pass every
    assertion above.
    """
    import ast
    import inspect
    src = inspect.getsource(inv_api._attach_cloud_tags)
    called = {n.func.attr for n in ast.walk(ast.parse(src.lstrip()))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert called <= {"cached_tags_by_name", "get", "lower", "warning", "items", "append"}, (
        f"unexpected calls in _attach_cloud_tags: {sorted(called)}")


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
