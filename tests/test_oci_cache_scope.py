"""OCI read caches are scoped to the region + compartment they were fetched for.

Every OCID `/api/oci/images` and `/api/oci/network-options` hand to the deploy form
is local to one region and one compartment. Both were cached under
``cache_service.key_global(...)`` — literally ``vmcli:oci_images``, no dimensions —
so for a full TTL after an operator changed ``oci_region`` or
``oci_compartment_ocid`` in Setup → OCI the form kept offering the *previous*
region's OCIDs. LaunchInstance rejects a foreign OCID as an opaque
``404 NotAuthorizedOrNotFound`` naming neither region, and ``/images`` accepted no
``bust`` parameter, so there was no way to force the refresh either.

Pinned here:
  * ``_cache_key`` varies with region AND compartment (and only those).
  * A region change is a cache MISS — no cross-region payload can be served.
  * ``?bust=true`` re-reads on both ``/images`` and ``/network-options``; the plain
    GET still serves cache (the refresh must not become a no-op, and the page load
    must not become an uncached OCI round-trip).
  * ``/instances`` reads the exact key ``_build_oci_instances`` writes, and the
    deploy/destroy runners' ``invalidate_prefix`` still clears it — a scoped write
    with an unscoped invalidate is a silent no-op.

Follows the hermetic TestClient pattern from test_gcp_region.py. Heavy cloud deps
(fastapi/oci/…) are only present in CI; when missing the file SKIPs cleanly so the
per-file runner stays green.

Run: python tests/test_oci_cache_scope.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.api import oci
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
    from web_dashboard.services import cache_service
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"oci api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


ASHBURN = "us-ashburn-1"
CHICAGO = "us-chicago-1"
COMP_A = "ocid1.compartment.oc1..aaaacompartmentA"
COMP_B = "ocid1.compartment.oc1..aaaacompartmentB"

_CFG: dict = {}
_CALLS: list = []


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


def _reset(region=ASHBURN, compartment=COMP_A):
    """Fresh config + empty cache. The store is cleared directly rather than via
    ``invalidate`` so no event loop is needed between tests."""
    _CFG.clear()
    _CFG.update({
        "oci_tenancy_ocid":  "ocid1.tenancy.oc1..aaaatenancy",
        "oci_user_ocid":     "ocid1.user.oc1..aaaauser",
        "oci_private_key":   "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "oci_region":        region,
        "oci_compartment_ocid": compartment,
        "oci_vcn_ocid":      "ocid1.vcn.oc1..aaaavcn",
    })
    _CALLS.clear()
    cache_service._store.clear()


def _install_stubs():
    oci._oci_cfg = lambda key, fallback="": _CFG.get(key) or fallback

    async def _fake_list_images(compartment_id=""):
        _CALLS.append(("images", _CFG["oci_region"], compartment_id))
        # OCIDs are region-local: tag them so a cross-region serve is visible.
        return [{"ocid": f"ocid1.image.oc1.{_CFG['oci_region']}..img1",
                 "display_name": f"img-in-{_CFG['oci_region']}",
                 "source": "custom"}]

    # availability_domain/image_ocid are the shape-picker scope (the route passes them
    # positionally); the shape list is AD- and image-scoped, so they are cache-key
    # dimensions too — see tests/test_oci_shape_scoping.py.
    async def _fake_network_options(compartment_id="", vcn_id="",
                                    availability_domain="", image_ocid=""):
        _CALLS.append(("netopts", _CFG["oci_region"], compartment_id))
        return {"availability_domains": [f"AD-1-{_CFG['oci_region']}"],
                "shapes": [], "subnets": [],
                "region": _CFG["oci_region"], "compartment_ocid": compartment_id,
                "availability_domain": availability_domain, "image_ocid": image_ocid}

    oci.oci_service.list_images = _fake_list_images
    oci.oci_service.get_network_options = _fake_network_options


_app = FastAPI()
_app.include_router(oci.router)
_app.dependency_overrides[get_current_user] = lambda: _AdminUser()
_app.dependency_overrides[get_db] = lambda: object()
# One client for the whole module: cache_service's asyncio.Lock must not be shared
# across event loops.
_client = TestClient(_app, raise_server_exceptions=False)


# ── _cache_key ────────────────────────────────────────────────────────────────

def test_cache_key_varies_with_region_and_compartment():
    _install_stubs()
    _reset()
    base = oci._cache_key("oci_images")

    _CFG["oci_region"] = CHICAGO
    assert oci._cache_key("oci_images") != base, "region must scope the key"

    _CFG["oci_region"] = ASHBURN
    _CFG["oci_compartment_ocid"] = COMP_B
    assert oci._cache_key("oci_images") != base, "compartment must scope the key"

    # Same dimensions → same key, or nothing would ever hit.
    _CFG["oci_compartment_ocid"] = COMP_A
    assert oci._cache_key("oci_images") == base
    # Distinct payloads keep distinct namespaces.
    assert oci._cache_key("oci_images") != oci._cache_key("oci_network_opts")
    # An explicit compartment overrides the configured one (the write-side call).
    assert oci._cache_key("oci_images", COMP_B) != base


def test_cache_key_stays_invalidate_prefix_compatible():
    """The runners clear by prefix; ``vmcli:<name>:`` must still match."""
    _install_stubs()
    _reset()
    for name in ("oci_images", "oci_network_opts", "oci_instances"):
        assert oci._cache_key(name).startswith(f"vmcli:{name}:")
    # Extra scope dimensions (the shape picker's AD + image) must stay under the same
    # prefix. A scoped key the runners' invalidate_prefix can't reach is a silent
    # no-op — the exact failure mode this test was written for.
    scoped = oci._cache_key("oci_network_opts", ad="AD-2", image="ocid1.image..a")
    assert scoped.startswith("vmcli:oci_network_opts:")
    assert scoped != oci._cache_key("oci_network_opts")


# ── GET /api/oci/images ───────────────────────────────────────────────────────

def test_images_cached_then_region_change_is_a_miss():
    _install_stubs()
    _reset()

    r = _client.get("/api/oci/images")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["images"][0]["display_name"] == f"img-in-{ASHBURN}"
    assert len(_CALLS) == 1

    # Second GET is served from cache — the TTL still has to work.
    r = _client.get("/api/oci/images")
    assert r.status_code == 200
    assert len(_CALLS) == 1, "second GET should hit the cache"

    # Operator switches region in Setup → OCI. The cached Ashburn OCIDs must not
    # be served for Chicago: that is the 404 NotAuthorizedOrNotFound this fixes.
    _CFG["oci_region"] = CHICAGO
    r = _client.get("/api/oci/images")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["images"][0]["display_name"] == f"img-in-{CHICAGO}"
    assert len(_CALLS) == 2, "region change must re-fetch"
    assert ASHBURN not in r.text


def test_images_compartment_change_is_a_miss():
    _install_stubs()
    _reset()
    assert _client.get("/api/oci/images").status_code == 200
    _CFG["oci_compartment_ocid"] = COMP_B
    r = _client.get("/api/oci/images")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["compartment_ocid"] == COMP_B
    assert [c[2] for c in _CALLS] == [COMP_A, COMP_B]


def test_images_bust_forces_refetch():
    _install_stubs()
    _reset()
    assert _client.get("/api/oci/images").status_code == 200
    assert len(_CALLS) == 1

    r = _client.get("/api/oci/images?bust=true")
    assert r.status_code == 200, (r.status_code, r.text)
    assert len(_CALLS) == 2, "?bust=true must ignore a warm entry"

    # …and it repopulates, so the next plain GET is a hit again.
    assert _client.get("/api/oci/images").status_code == 200
    assert len(_CALLS) == 2


def test_network_options_bust_and_region_scope():
    """Same two properties on the sibling endpoint — its ``bust`` predates this
    change, its region scoping did not."""
    _install_stubs()
    _reset()
    r = _client.get("/api/oci/network-options")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["availability_domains"] == [f"AD-1-{ASHBURN}"]
    assert _client.get("/api/oci/network-options").status_code == 200
    assert len(_CALLS) == 1, "second GET should hit the cache"

    assert _client.get("/api/oci/network-options?bust=true").status_code == 200
    assert len(_CALLS) == 2

    _CFG["oci_region"] = CHICAGO
    r = _client.get("/api/oci/network-options")
    assert r.status_code == 200
    assert r.json()["availability_domains"] == [f"AD-1-{CHICAGO}"]
    assert len(_CALLS) == 3, "region change must re-fetch"


def test_network_options_ad_and_image_are_cache_dimensions_too():
    """The shape list is AD- and image-scoped (OCI offers different shapes in
    different ADs, and an image boots only on the shapes it supports), so the key has
    to carry both — served from an AD-blind key, switching the form to AD-2 gets the
    list built for AD-1, which is the bug the scoping exists to fix.

    These dimensions NARROW the region+compartment key; they must not replace it, so
    the region check is repeated on a scoped key at the end."""
    _install_stubs()
    _reset()
    r = _client.get("/api/oci/network-options")
    assert r.status_code == 200, (r.status_code, r.text)
    assert len(_CALLS) == 1

    # The route must thread the parameter through to the service, not drop it.
    r = _client.get("/api/oci/network-options?availability_domain=AD-2")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["availability_domain"] == "AD-2"
    assert len(_CALLS) == 2, "an AD change must re-fetch"
    assert _client.get("/api/oci/network-options?availability_domain=AD-2").status_code == 200
    assert len(_CALLS) == 2, "the same AD should hit the cache"

    r = _client.get("/api/oci/network-options"
                    "?availability_domain=AD-2&image_ocid=ocid1.image..a")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["image_ocid"] == "ocid1.image..a"
    assert len(_CALLS) == 3, "an image change must re-fetch"

    _CFG["oci_region"] = CHICAGO
    assert _client.get("/api/oci/network-options?availability_domain=AD-2").status_code == 200
    assert len(_CALLS) == 4, "region change must re-fetch even on an AD-scoped key"


# ── /instances: write key == read key, and the runners still clear it ─────────

class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return []


class _FakeDB:
    def query(self, *_a, **_k):
        return _FakeQuery()


def test_instances_write_key_matches_read_key_and_prefix_clears_it():
    _install_stubs()
    _reset()

    async def _fake_describe(compartment_id, ocids):
        return []

    oci.oci_service.describe_instances = _fake_describe

    async def _exercise():
        await oci._build_oci_instances(_FakeDB(), oci._compartment())
        # The read path in list_instances must find what the build path wrote.
        assert await cache_service.get(oci._cache_key("oci_instances")) is not None
        # oci_vm_service clears by prefix after a deploy/destroy.
        assert await cache_service.invalidate_prefix("oci_instances") == 1
        assert await cache_service.get(oci._cache_key("oci_instances")) is None

    asyncio.run(_exercise())


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
