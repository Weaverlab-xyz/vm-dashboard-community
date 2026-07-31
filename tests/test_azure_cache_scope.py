"""The Azure private-image cache is scoped to the location it was fetched for.

``/api/azure/images`` does not read one fixed gallery: it resolves
``resolve_azure_region(_loc())`` into a per-region ``gallery_name`` /
``gallery_resource_group`` / ``resource_group``, all of which differ per region under
``azure_region_configs``. The result was still cached under
``cache_service.key_global("azure_images")`` — literally ``vmcli:azure_images``, no
dimensions — so whichever region loaded first owned the list for the whole 5-minute
TTL and the Images tab showed another region's gallery. ``/images`` also took no
``location`` at all, so there was no way to ask for the right one.

Pinned here:
  * The key varies with location and nothing else, and stays ``invalidate_prefix``-able.
  * A change to the configured ``azure_location`` is a cache MISS.
  * ``?location=`` scopes the lookup — including which *gallery* is read, not just the
    cache slot — and echoes the location back.
  * A malformed ``?location=`` is a 400; a display-form name ("West Europe") normalises
    onto the same cache entry as the compact form rather than duplicating it.
  * ``invalidate_prefix("azure_images")`` still clears it — delete_image and the image
    runner switched to prefix form, and a scoped write with an exact-key invalidate is
    a silent no-op.

Follows the hermetic TestClient pattern from test_gcp_region.py. Heavy cloud deps
(fastapi/azure-sdk/…) are only present in CI; when missing the file SKIPs cleanly so
the per-file runner stays green.

Run: python tests/test_azure_cache_scope.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.api import azure
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
    from web_dashboard.services import cache_service, region_config
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"azure api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


CENTRALUS = "centralus"
WESTEUROPE = "westeurope"

_CFG: dict = {}
_CALLS: list = []


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


def _reset(location=CENTRALUS):
    """Fresh config + empty cache. The store is cleared directly rather than via
    ``invalidate`` so no event loop is needed between tests."""
    _CFG.clear()
    _CFG["azure_location"] = location
    _CALLS.clear()
    cache_service._store.clear()
    cache_service._inflight.clear()
    cache_service._pending.clear()


def _install_stubs():
    azure._cfg = lambda key, fallback="": _CFG.get(key) or fallback

    # Each region names its own gallery — this is the whole reason the key needs the
    # location. api/azure.py imports this lazily, so patching the module attribute is
    # what the endpoint will see.
    def _fake_resolve(location):
        return {"gallery_name": f"gallery-{location}",
                "gallery_resource_group": f"rg-gal-{location}",
                "resource_group": f"rg-{location}"}

    region_config.resolve_azure_region = _fake_resolve

    async def _fake_list_private_images(gallery, gallery_rg, rg):
        _CALLS.append(gallery)
        return {"images": [{"resource_id": f"/subscriptions/x/images/{gallery}-img",
                            "name": f"img-in-{gallery}",
                            "source": "gallery"}],
                "warnings": []}

    azure.azure_service.list_private_images = _fake_list_private_images


_app = FastAPI()
_app.include_router(azure.router)
_app.dependency_overrides[get_current_user] = lambda: _AdminUser()
_app.dependency_overrides[get_db] = lambda: object()
# One client for the whole module: cache_service's asyncio.Lock must not be shared
# across event loops.
_client = TestClient(_app, raise_server_exceptions=False)


# ── Key shape ─────────────────────────────────────────────────────────────────

def test_cache_key_varies_with_location_only():
    base = azure.images_cache_key(CENTRALUS)
    assert azure.images_cache_key(WESTEUROPE) != base, \
        "location must scope the key"
    assert azure.images_cache_key(CENTRALUS) == base
    # Distinct payloads keep distinct namespaces.
    assert base != cache_service.key_param("azure_network_opts", location=CENTRALUS)


def test_cache_key_stays_invalidate_prefix_compatible():
    """delete_image clears by prefix; ``vmcli:azure_images:`` must match."""
    assert azure.images_cache_key(CENTRALUS).startswith("vmcli:azure_images:")


# ── GET /api/azure/images ─────────────────────────────────────────────────────

def test_images_cached_then_location_change_is_a_miss():
    _install_stubs()
    _reset()

    r = _client.get("/api/azure/images")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["images"][0]["name"] == f"img-in-gallery-{CENTRALUS}"
    assert r.json()["location"] == CENTRALUS
    assert len(_CALLS) == 1

    # Second GET is served from cache — the TTL still has to work.
    assert _client.get("/api/azure/images").status_code == 200
    assert len(_CALLS) == 1, "second GET should hit the cache"

    # Operator switches region in Setup → Azure. The centralus gallery's images must
    # not be served for westeurope.
    _CFG["azure_location"] = WESTEUROPE
    r = _client.get("/api/azure/images")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["images"][0]["name"] == f"img-in-gallery-{WESTEUROPE}"
    assert r.json()["location"] == WESTEUROPE
    assert len(_CALLS) == 2, "location change must re-fetch"
    assert CENTRALUS not in r.text


def test_location_query_param_scopes_the_gallery_and_the_cache():
    _install_stubs()
    _reset()

    r = _client.get(f"/api/azure/images?location={WESTEUROPE}")
    assert r.status_code == 200, (r.status_code, r.text)
    # Not just a different cache slot — a different gallery was actually read.
    assert _CALLS == [f"gallery-{WESTEUROPE}"]
    assert r.json()["location"] == WESTEUROPE

    r = _client.get("/api/azure/images")
    assert r.status_code == 200
    assert r.json()["images"][0]["name"] == f"img-in-gallery-{CENTRALUS}"
    assert _CALLS == [f"gallery-{WESTEUROPE}", f"gallery-{CENTRALUS}"]

    # …and each location now has its own warm entry.
    assert _client.get(f"/api/azure/images?location={WESTEUROPE}").status_code == 200
    assert _client.get("/api/azure/images").status_code == 200
    assert len(_CALLS) == 2, "both locations should be cached independently"


def test_display_form_location_normalises_onto_one_entry():
    """``_resolve_location`` folds "West Europe" → "westeurope". If the key were built
    from the raw string the two spellings would be separate entries holding identical
    data, and the header picker's value would miss the warmer's."""
    _install_stubs()
    _reset()
    assert _client.get(f"/api/azure/images?location={WESTEUROPE}").status_code == 200
    assert len(_CALLS) == 1

    r = _client.get("/api/azure/images?location=West%20Europe")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["location"] == WESTEUROPE
    assert len(_CALLS) == 1, "the normalised form must hit the same cache entry"


def test_malformed_location_is_rejected_not_silently_defaulted():
    _install_stubs()
    _reset()
    r = _client.get("/api/azure/images?location=not/a/region")
    assert r.status_code == 400, (r.status_code, r.text)
    assert _CALLS == [], "a rejected location must not reach Azure"


# ── The write key and the invalidate must agree ───────────────────────────────

def test_invalidate_prefix_clears_every_location():
    """delete_image and the image runner clear by prefix. An exact-key
    ``invalidate(key_global("azure_images"))`` would match nothing now."""
    _install_stubs()
    _reset()
    assert _client.get("/api/azure/images").status_code == 200
    assert _client.get(f"/api/azure/images?location={WESTEUROPE}").status_code == 200
    assert len(_CALLS) == 2

    async def _exercise():
        assert await cache_service.invalidate_prefix("azure_images") == 2, \
            "both locations' entries must be cleared"
        for loc in (CENTRALUS, WESTEUROPE):
            key = azure.images_cache_key(loc)
            assert await cache_service.get(key) is None

    asyncio.run(_exercise())

    assert _client.get("/api/azure/images").status_code == 200
    assert _client.get(f"/api/azure/images?location={WESTEUROPE}").status_code == 200
    assert len(_CALLS) == 4


def test_wizard_save_invalidation_reaches_the_scoped_key():
    """``api/setup.py::_invalidate_data_caches`` lists ``azure_images`` and used to
    clear only the exact ``vmcli:azure_images``. Now that the key is parameterised that
    call matched nothing, so a wizard save left the pre-save gallery list live."""
    from web_dashboard.api import setup as setup_api

    _install_stubs()
    _reset()
    assert _client.get("/api/azure/images").status_code == 200
    assert len(_CALLS) == 1

    # It belongs in the PREFIX tuple now the key is scoped: the exact-key tuple is
    # cleared with invalidate(key_global(name)), which no longer matches anything.
    assert "azure_images" in setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES
    assert "azure_images" not in setup_api._CONFIG_DEPENDENT_CACHES
    asyncio.run(setup_api._invalidate_data_caches())

    assert _client.get("/api/azure/images").status_code == 200
    assert len(_CALLS) == 2, "a wizard save must force a re-fetch"


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
