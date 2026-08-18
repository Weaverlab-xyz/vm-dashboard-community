"""Startup cache warmers must agree with the API routes that read the same keys.

The warmers in ``web_dashboard/main.py`` write cache entries that the ``api/*``
routes then serve. Both halves of that contract have drifted in the past, and
neither failure raises anything — the app just serves wrong or uncached data:

  * PAYLOAD drift. ``_warm_aws_instances`` kept its own copy of the instance fetch
    that omitted ``region``/``workgroup``/``key_name``. ``GET /api/aws/instances``
    drops every row whose ``workgroup`` is None for a non-admin, and
    ``EC2InstanceInfo`` defaults those fields, so nothing errored — a warmer pass
    just silently emptied the instance list for every non-admin user and blanked
    the by-region dashboard tile.
  * KEY drift. The network-options warmers wrote ``key_global("aws_network_opts")``
    while the routes read ``key_param("aws_network_opts", region=...)``. The warmer
    filled a key nobody reads, so the first request of every session still paid a
    live cloud round-trip.
  * SCOPE drift. The warmers resolved the region from ``settings.aws_region``
    (process start) while the routes use ``_aws_region()`` (config_service), so
    after a Setup-wizard region change the warmer kept refilling the cache from the
    OLD region.

So these tests run a real warmer pass against stubbed cloud calls and assert the
key and payload match what the route's own fetcher produces. They also pin the
key SHAPE of setup.py's invalidation tuples, where using the wrong one
(``invalidate`` vs ``invalidate_prefix``) is a silent no-op rather than an error.

Heavy deps (fastapi/boto3/…) are only present in CI; when they're missing the file
SKIPs cleanly so the per-file runner stays green.

Run: python tests/test_cache_warmer_parity.py   (or under pytest)
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-warmer-parity")

try:
    from web_dashboard import main
    from web_dashboard.api import aws as aws_api
    from web_dashboard.api import azure as azure_api
    from web_dashboard.api import setup as setup_api
    from web_dashboard.services import cache_service
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"dashboard import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── stubs ─────────────────────────────────────────────────────────────────────

DEFAULT_REGION = "us-east-2"


class _FakeJob:
    """Stands in for a completed ec2_deploy Job row."""

    job_type = "ec2_deploy"
    status = "completed"

    def __init__(self, job_id, instance_id, region, workgroup, created_by):
        self.id = job_id
        self.workgroup = workgroup
        self.created_by = created_by
        self.metadata_dict = {"instance_id": instance_id, "region": region}


class _FakeQuery:
    def __init__(self, jobs):
        self._jobs = jobs

    def filter(self, *_a, **_kw):
        return self

    def order_by(self, *_a, **_kw):
        return self

    def all(self):
        return list(self._jobs)


class _FakeDB:
    def __init__(self, jobs):
        self._jobs = jobs
        self.closed = False

    def query(self, *_a, **_kw):
        return _FakeQuery(self._jobs)

    def close(self):
        self.closed = True


JOBS = [
    _FakeJob("job-a", "i-aaa", "us-west-2", "Eng", "alice"),
    _FakeJob("job-b", "i-bbb", "eu-west-1", None, "bob"),
]


async def _fake_describe_instances(region, ids):
    return [{"instance_id": i, "name": i, "state": "running",
             "key_name": f"kp-{region}"} for i in ids]


class _Patch:
    """Restore a set of monkeypatched attributes on exit."""

    def __init__(self):
        self._saved = []

    def set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._saved):
            setattr(obj, name, old)
        self._saved.clear()


async def _first_write(warmer, patch):
    """Run `warmer()` until its first cache write, then cancel it.

    Returns the (key, payload, ttl) it wrote. Patching cache_service.set is how we
    observe the warmer without letting it loop — a warmer sleeps ttl*0.8 after its
    first pass, so cancelling is what keeps the test fast.
    """
    captured = {}
    done = asyncio.Event()

    async def _fake_set(key, payload, ttl):
        captured.update(key=key, payload=payload, ttl=ttl)
        done.set()

    patch.set(cache_service, "set", _fake_set)
    task = asyncio.create_task(warmer())
    try:
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert captured, "warmer never wrote to the cache"
    return captured["key"], captured["payload"], captured["ttl"]


def _stub_instance_fetch(patch):
    """Point the warmer's DB + EC2 calls at the fakes.

    ``_aws_cfg`` is stubbed too, not just for speed: the warmer opens its session
    via ``database.SessionLocal``, and config_service opens one the same way, so a
    fake SessionLocal would otherwise be handed config lookups as well.
    """
    from web_dashboard import database
    patch.set(aws_api.aws_service, "describe_instances", _fake_describe_instances)
    patch.set(aws_api, "_aws_cfg", lambda key, fallback="":
              DEFAULT_REGION if key == "aws_region" else fallback)
    # _warm_aws_instances imports SessionLocal from .database at call time.
    patch.set(database, "SessionLocal", lambda: _FakeDB(JOBS))


# ── payload parity: the warmer must emit exactly what the reader filters on ────

def test_aws_instances_warmer_payload_matches_route_fetcher():
    """One shared fetcher, so warmer and /api/aws/instances can't diverge."""
    patch = _Patch()

    async def _run():
        _stub_instance_fetch(patch)
        key, payload, ttl = await _first_write(main._warm_aws_instances, patch)
        expected = await aws_api._fetch_instances(_FakeDB(JOBS))
        return key, payload, ttl, expected

    try:
        key, payload, ttl, expected = asyncio.run(_run())
    finally:
        patch.undo()

    assert payload == expected, (
        "warmer payload diverged from aws._fetch_instances:\n"
        f"  warmer:   {payload}\n  route:    {expected}")
    assert key == aws_api.instances_cache_key()
    assert ttl == cache_service.TTL[aws_api.CACHE_KEY_INSTANCES]


def test_aws_instances_warmer_emits_rbac_and_region_fields():
    """The fields /instances filters on and the dashboard tile groups by.

    This is the actual regression: without `workgroup`, the non-admin branch of
    list_instances drops every row; without `region`, the by-region tile blanks.
    """
    patch = _Patch()

    async def _run():
        _stub_instance_fetch(patch)
        return await _first_write(main._warm_aws_instances, patch)

    try:
        _key, payload, _ttl = asyncio.run(_run())
    finally:
        patch.undo()

    assert len(payload) == len(JOBS)
    for row in payload:
        for field in ("region", "workgroup", "key_name", "job_id", "deployed_by"):
            assert field in row, f"warmer payload missing {field!r}: {row}"

    by_id = {r["instance_id"]: r for r in payload}
    # region comes from the deploy job, not the process-wide default.
    assert by_id["i-aaa"]["region"] == "us-west-2"
    assert by_id["i-bbb"]["region"] == "eu-west-1"
    # workgroup is lower-cased for the RBAC comparison; None stays None.
    assert by_id["i-aaa"]["workgroup"] == "eng"
    assert by_id["i-bbb"]["workgroup"] is None


def test_non_admin_still_sees_instances_after_a_warmer_pass():
    """End-to-end shape of the bug: filter the warmed payload the way the route does."""
    patch = _Patch()

    async def _run():
        _stub_instance_fetch(patch)
        return await _first_write(main._warm_aws_instances, patch)

    try:
        _key, payload, _ttl = asyncio.run(_run())
    finally:
        patch.undo()

    accessible = ["eng"]  # what _accessible_workgroups returns for a non-admin
    visible = [i for i in payload
               if i.get("workgroup") is not None and i.get("workgroup") in accessible]
    assert [i["instance_id"] for i in visible] == ["i-aaa"], (
        "a warmer pass hid every instance from non-admins")


# ── key parity: warmer and reader must build the same key ─────────────────────

def test_network_opts_warmers_use_the_per_region_reader_key():
    """key_param(region/location=...), not key_global — and scoped per pass."""
    patch = _Patch()

    async def _run():
        async def _fake_aws_netopts(region):
            return {"region": region, "subnets": [], "security_groups": [],
                    "instance_types": []}

        patch.set(aws_api.aws_service, "get_network_options", _fake_aws_netopts)
        patch.set(aws_api, "_aws_region", lambda: "ap-south-1")
        aws_key, aws_payload, _ = await _first_write(
            main._warm_aws_network_opts, patch)

        async def _fake_azure_netopts(location):
            return {"location": location, "locations": [], "vm_sizes": [],
                    "subnets": [], "nsgs": [], "ssh_keys": []}

        patch.set(azure_api, "_fetch_network_options", _fake_azure_netopts)
        patch.set(azure_api, "_loc", lambda: "westeurope")
        az_key, az_payload, _ = await _first_write(
            main._warm_azure_network_opts, patch)
        return aws_key, aws_payload, az_key, az_payload

    try:
        aws_key, aws_payload, az_key, az_payload = asyncio.run(_run())
    finally:
        patch.undo()

    # Exactly the key the route reads.
    assert aws_key == aws_api.network_opts_cache_key("ap-south-1")
    assert az_key == azure_api.network_opts_cache_key("westeurope")
    # The pre-fix keys, which no reader ever looks at.
    assert aws_key != cache_service.key_global("aws_network_opts")
    assert az_key != cache_service.key_global("azure_network_opts")
    # The key describes the same region as the data under it.
    assert aws_payload["region"] == "ap-south-1"
    assert az_payload["location"] == "westeurope"


def test_amis_and_images_warmers_use_the_per_region_reader_key():
    """The AMI list and the private-image list are per-scope caches too.

    An AMI id only resolves in its own region, and under ``azure_region_configs`` each
    region names its own Shared Image Gallery — so both keys carry their scope and both
    warmers must go through ``_warm_scoped_loop``. Reverting either to the flat
    ``_warm_loop`` calls ``key_fn()`` with no argument: that raises TypeError, which
    ``_warm_loop`` swallows and logs, so the warmer silently never writes anything and
    every first request of a session pays a live cloud round-trip. ``_first_write``
    fails on exactly that (it asserts a write happened at all).
    """
    patch = _Patch()

    async def _run():
        async def _fake_list_amis(region):
            return [{"ami_id": f"ami-{region}", "name": f"img-{region}",
                     "state": "available"}]

        patch.set(aws_api.aws_service, "list_amis", _fake_list_amis)
        patch.set(aws_api, "_aws_region", lambda: "ap-south-1")
        aws_key, aws_payload, _ = await _first_write(main._warm_aws_amis, patch)

        async def _fake_private_images(location):
            return {"images": [{"resource_id": f"/subscriptions/x/{location}",
                                "name": f"img-{location}"}],
                    "warnings": []}

        patch.set(azure_api, "_fetch_private_images", _fake_private_images)
        patch.set(azure_api, "_loc", lambda: "westeurope")
        az_key, az_payload, _ = await _first_write(main._warm_azure_images, patch)
        return aws_key, aws_payload, az_key, az_payload

    try:
        aws_key, aws_payload, az_key, az_payload = asyncio.run(_run())
    finally:
        patch.undo()

    # Exactly the key the route reads.
    assert aws_key == aws_api.amis_cache_key("ap-south-1")
    assert az_key == azure_api.images_cache_key("westeurope")
    # The pre-fix keys, which no reader ever looks at.
    assert aws_key != cache_service.key_global(aws_api.CACHE_KEY_AMIS)
    assert az_key != cache_service.key_global(azure_api.CACHE_KEY_IMAGES)
    # The key describes the same scope as the data stored under it.
    assert aws_payload[0]["name"] == "img-ap-south-1"
    assert az_payload["images"][0]["name"] == "img-westeurope"


def test_warmers_resolve_scope_through_config_service_not_startup_env():
    """A Setup-wizard region change must reach the warmer without a restart.

    `_aws_region()` reads config_service first; the warmer used `settings.aws_region`,
    which is frozen at process start.
    """
    patch = _Patch()
    seen = []

    async def _run():
        async def _fake_netopts(region):
            seen.append(region)
            return {"region": region}

        patch.set(aws_api.aws_service, "get_network_options", _fake_netopts)
        patch.set(aws_api, "_aws_cfg", lambda key, fallback="":
                  "ca-central-1" if key == "aws_region" else fallback)
        return await _first_write(main._warm_aws_network_opts, patch)

    try:
        key, _payload, _ttl = asyncio.run(_run())
    finally:
        patch.undo()

    assert seen == ["ca-central-1"], f"warmer ignored the live config: {seen}"
    assert key == cache_service.key_param("aws_network_opts", region="ca-central-1")


def _referenced_names(fn):
    """Every attribute/global name `fn` touches, including nested closures."""
    names = set()
    stack = [fn.__code__]
    while stack:
        code = stack.pop()
        names.update(code.co_names)
        stack.extend(c for c in code.co_consts if hasattr(c, "co_names"))
    return names


def test_warmers_delegate_to_the_route_modules():
    """Each warmer must reference the api module's fetcher + key builder by name.

    A warmer that stops delegating and inlines its own fetch is exactly how the
    payload drifted last time, and no runtime assertion catches it — the app just
    caches a differently-shaped dict. This pins the delegation structurally.
    """
    expected = {
        main._warm_aws_amis:          {"_fetch_amis", "amis_cache_key"},
        main._warm_aws_network_opts:  {"network_opts_cache_key", "_aws_region",
                                      "get_network_options"},
        main._warm_aws_instances:     {"_fetch_instances", "instances_cache_key"},
        main._warm_azure_images:      {"_fetch_private_images", "images_cache_key"},
        main._warm_azure_network_opts: {"network_opts_cache_key",
                                       "_fetch_network_options", "_loc"},
    }
    for warmer, required in expected.items():
        names = _referenced_names(warmer)
        missing = required - names
        assert not missing, (
            f"{warmer.__name__} no longer delegates to the api module "
            f"(missing {sorted(missing)}) — warm what the reader reads")


def test_cache_key_builders_are_the_single_source_of_key_truth():
    # These identities are what make payload drift impossible by construction.
    # amis/images are per-region now (an AMI id and a gallery are both region-local),
    # so their builders take the scope — same shape as network_opts below.
    assert aws_api.amis_cache_key("eu-west-1") == cache_service.key_param(
        aws_api.CACHE_KEY_AMIS, region="eu-west-1")
    assert azure_api.images_cache_key("westeurope") == cache_service.key_param(
        azure_api.CACHE_KEY_IMAGES, location="westeurope")
    # …and never the flat key, which no reader looks at any more.
    assert aws_api.amis_cache_key("eu-west-1") != cache_service.key_global(
        aws_api.CACHE_KEY_AMIS)
    assert azure_api.images_cache_key("westeurope") != cache_service.key_global(
        azure_api.CACHE_KEY_IMAGES)
    assert aws_api.CACHE_KEY_AMIS in cache_service.TTL
    assert aws_api.CACHE_KEY_INSTANCES in cache_service.TTL
    assert aws_api.CACHE_KEY_NETWORK_OPTS in cache_service.TTL
    assert azure_api.CACHE_KEY_IMAGES in cache_service.TTL
    assert azure_api.CACHE_KEY_NETWORK_OPTS in cache_service.TTL


# ── setup.py invalidation: right delete for the key shape ─────────────────────

def test_config_dependent_cache_tuples_are_disjoint():
    overlap = set(setup_api._CONFIG_DEPENDENT_CACHES) & set(
        setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES)
    assert not overlap, f"a name must be in exactly one tuple: {overlap}"


def test_prefix_entries_clear_the_key_param_keys_the_readers_write():
    """invalidate_prefix matches 'vmcli:<name>:', so it reaches key_param families.

    The pre-fix code invalidated these with key_global(name) -> 'vmcli:<name>', which
    matches nothing these endpoints ever write. Assert both halves.
    """
    async def _run():
        results = {}
        for name in setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES:
            probe = cache_service.key_param(name, region="probe")
            await cache_service.set(probe, {"v": 1}, 300)
            # The old, wrong delete.
            await cache_service.invalidate(cache_service.key_global(name))
            survived_exact = await cache_service.get(probe) is not None
            # The right one.
            await cache_service.invalidate_prefix(name)
            gone = await cache_service.get(probe) is None
            results[name] = (survived_exact, gone)
        return results

    for name, (survived_exact, gone) in asyncio.run(_run()).items():
        assert survived_exact, (
            f"{name}: exact-key invalidation unexpectedly matched — recheck which "
            "tuple it belongs in")
        assert gone, f"{name}: invalidate_prefix did not clear its key_param keys"


def test_global_entries_are_cleared_by_exact_key_invalidation():
    async def _run():
        results = {}
        for name in setup_api._CONFIG_DEPENDENT_CACHES:
            key = cache_service.key_global(name)
            await cache_service.set(key, {"v": 1}, 300)
            await cache_service.invalidate(key)
            results[name] = await cache_service.get(key) is None
        return results

    for name, gone in asyncio.run(_run()).items():
        assert gone, f"{name}: exact-key invalidation failed"


def test_invalidate_data_caches_clears_both_shapes():
    """The real wizard path, not a reimplementation of it."""
    async def _run():
        seeded = []
        for name in setup_api._CONFIG_DEPENDENT_CACHES:
            k = cache_service.key_global(name)
            await cache_service.set(k, {"v": 1}, 300)
            seeded.append(k)
        for name in setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES:
            k = cache_service.key_param(name, region="probe")
            await cache_service.set(k, {"v": 1}, 300)
            seeded.append(k)

        await setup_api._invalidate_data_caches()
        return [k for k in seeded if await cache_service.get(k) is not None]

    survivors = asyncio.run(_run())
    assert not survivors, f"wizard save left stale cache entries: {survivors}"


def test_live_cloud_caches_are_all_listed_for_invalidation():
    """Config-dependent caches must be invalidated on a wizard save."""
    listed = set(setup_api._CONFIG_DEPENDENT_CACHES) | set(
        setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES)
    for name in ("aws_amis", "aws_instances", "aws_network_opts",
                 "azure_images", "azure_vms", "azure_network_opts",
                 "gcp_custom_images", "gcp_instances", "gcp_network_opts"):
        assert name in listed, f"{name} is config-dependent but never invalidated"


def test_oci_needs_no_invalidation_entry_because_its_keys_are_scoped():
    """A key scoped to the dimension that changed self-heals — prefer that to listing.

    api/oci.py::_cache_key keys on region + compartment, so a Setup change lands on
    a new key and misses. If that ever regresses to key_global, OCI has to be listed
    in setup.py again; this catches the regression from the other side.
    See tests/test_oci_cache_scope.py.
    """
    from web_dashboard.api import oci as oci_api

    listed = set(setup_api._CONFIG_DEPENDENT_CACHES) | set(
        setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES)
    for name in ("oci_images", "oci_network_opts", "oci_instances"):
        assert name not in listed, (
            f"{name} is listed in setup.py — that is only needed if its key stopped "
            "being region/compartment-scoped")

    patch = _Patch()
    try:
        patch.set(oci_api, "_region", lambda: "us-ashburn-1")
        patch.set(oci_api, "_compartment", lambda: "ocid1.compartment.oc1..aaa")
        first = oci_api._cache_key("oci_images")
        patch.set(oci_api, "_region", lambda: "uk-london-1")
        second = oci_api._cache_key("oci_images")
    finally:
        patch.undo()

    assert first != second, "a region change must produce a different OCI cache key"
    assert first != cache_service.key_global("oci_images")


def test_the_cost_warmer_calls_the_route_entrypoint():
    """The cost warmer must go through services/cost_cache, the same entrypoint
    /api/costs/{summary,breakdown} use.

    Stronger than the delegation check above rather than weaker: there is no cache key to
    share and no second fetch path, so there is nothing left to drift onto. The
    cache_service assertion is the real regression guard — cost data moved to a table
    precisely because that store caches a 429 exactly like a real figure, holds a private
    copy per gunicorn worker, and loses everything on a rebuild."""
    names = _referenced_names(main._warm_cost_summary)
    assert "cost_cache" in names, "the cost warmer no longer delegates to cost_cache"
    assert "warm" in names
    assert "cache_service" not in names, "cost data must not go back into cache_service"
    assert "cost_service" not in names, "warm what the reader reads, not the layer below it"


def test_no_dead_cache_names_remain():
    """These had a TTL entry and/or an invalidation call but no reader or writer."""
    dead = ("cfgmgmt_instances", "cfgmgmt_s3status", "aws_ssh_key_secrets",
            "azure_marketplace",
            # The /api/images/{ovas,isos} listing endpoints are gone; the TTLs outlived them.
            "images_ovas", "images_isos",
            # Moved to the cloud_cost_cache table — see cost_cache.py's docstring.
            "cost_summary", "cost_breakdown_v2")
    listed = set(setup_api._CONFIG_DEPENDENT_CACHES) | set(
        setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES)
    for name in dead:
        assert name not in cache_service.TTL, (
            f"{name} is back in cache_service.TTL — if it now has a real reader, "
            "drop it from this list; otherwise it is dead weight")
        assert name not in listed, f"{name} is being invalidated but nothing writes it"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
