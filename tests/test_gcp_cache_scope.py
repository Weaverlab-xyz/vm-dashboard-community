"""The GCP read caches are scoped to the project they were fetched from.

``gcp_custom_images`` and ``gcp_instances`` were cached under
``cache_service.key_global(...)`` — literally ``vmcli:gcp_instances``, no dimensions —
but ``gcp_project_id`` is operator-changeable in Settings. After a project switch the
old project's images and instances were served for the whole TTL, and the
``gcp_instances`` payload *embeds* the ``project_id`` it was built for, so the page
actively reported the wrong project rather than merely going stale.

Unlike AWS/Azure this is not a region bug: GCE images are global within a project and
``_build_gcp_instances`` already iterates every zone a deploy job recorded, so the
payload spans regions by construction. Project is the missing dimension.

Pinned here:
  * Both keys vary with project, and stay ``invalidate_prefix``-able.
  * A project change is a cache MISS on both, and the instances payload reports the
    NEW project.
  * ``_build_gcp_instances`` writes the exact key ``list_instances`` and
    ``_gcp_instances_unfiltered`` read — three call sites, one helper, because a write
    key that disagrees with the read key is a silent permanent miss.
  * ``invalidate_prefix`` still clears both — the deploy/destroy/image runners switched
    to prefix form, and a scoped write with an exact-key invalidate is a no-op.
  * ``gcp_public_images_{os_filter}`` is deliberately left global: the public image
    projects are fixed constants in gcp_service.py, so it varies with nothing.

Follows the hermetic TestClient pattern from test_gcp_region.py. Heavy cloud deps
(fastapi/google-cloud/…) are only present in CI; when missing the file SKIPs cleanly so
the per-file runner stays green.

Run: python tests/test_gcp_cache_scope.py   (or under pytest)
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.api import gcp
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
    from web_dashboard.services import cache_service
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"gcp api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


PROJ_A = "proj-alpha"
PROJ_B = "proj-beta"
ZONE = "us-central1-a"

_STATE: dict = {"project": PROJ_A}
_CALLS: list = []


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


class _FakeJob:
    id = "job-1"
    created_by = "tester"
    workgroup = "eng"
    extra_data = json.dumps({"instance_name": "vm-1", "zone": ZONE})


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return [_FakeJob()]


class _FakeDB:
    def query(self, *_a, **_k):
        return _FakeQuery()


def _reset(project=PROJ_A):
    """Fresh config + empty cache. The store is cleared directly rather than via
    ``invalidate`` so no event loop is needed between tests."""
    _STATE["project"] = project
    _CALLS.clear()
    cache_service._store.clear()
    cache_service._inflight.clear()
    cache_service._pending.clear()


def _install_stubs():
    gcp._gcp_project = lambda: _STATE["project"]
    gcp._gcp_zone = lambda: ZONE
    gcp._gcp_region = lambda: "us-central1"

    async def _fake_list_custom_images(project_id=""):
        _CALLS.append(("images", project_id))
        # An image self_link embeds its project: tag it so a cross-project serve shows.
        return [{"self_link": f"projects/{project_id}/global/images/img1",
                 "name": f"img-in-{project_id}",
                 "source": "custom"}]

    async def _fake_describe_instances(project_id="", zone="", instance_names=None):
        _CALLS.append(("instances", project_id))
        return [{"instance_name": f"vm-in-{project_id}", "zone": zone,
                 "status": "RUNNING", "workgroup": "eng"}]

    gcp.gcp_service.list_custom_images = _fake_list_custom_images
    gcp.gcp_service.describe_instances = _fake_describe_instances


_app = FastAPI()
_app.include_router(gcp.router)
_app.dependency_overrides[get_current_user] = lambda: _AdminUser()
_app.dependency_overrides[get_db] = lambda: _FakeDB()
# One client for the whole module: cache_service's asyncio.Lock must not be shared
# across event loops.
_client = TestClient(_app, raise_server_exceptions=False)


# ── Key shapes ────────────────────────────────────────────────────────────────

def test_cache_keys_vary_with_project():
    for builder in (gcp.instances_cache_key, gcp.custom_images_cache_key):
        base = builder(PROJ_A)
        assert builder(PROJ_B) != base, f"project must scope {builder.__name__}"
        # Same project → same key, or nothing would ever hit.
        assert builder(PROJ_A) == base
    # Distinct payloads keep distinct namespaces.
    assert gcp.custom_images_cache_key(PROJ_A) != gcp.instances_cache_key(PROJ_A)


def test_cache_keys_stay_invalidate_prefix_compatible():
    """The deploy/destroy/image runners clear by prefix; ``vmcli:<name>:`` must match.
    The prefix comes from the same constant the runners pass to invalidate_prefix."""
    assert gcp.instances_cache_key(PROJ_A).startswith(f"vmcli:{gcp.CACHE_KEY_INSTANCES}:")
    assert gcp.custom_images_cache_key(PROJ_A).startswith(
        f"vmcli:{gcp.CACHE_KEY_CUSTOM_IMAGES}:")


def test_public_images_key_stays_global():
    """The public image projects are fixed constants, so this one genuinely varies with
    nothing but the os_filter it already carries — it must NOT gain a project."""
    assert cache_service.key_global("gcp_public_images_all") == "vmcli:gcp_public_images_all"


# ── GET /api/gcp/custom-images ────────────────────────────────────────────────

def test_custom_images_cached_then_project_change_is_a_miss():
    _install_stubs()
    _reset()

    r = _client.get("/api/gcp/custom-images")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["images"][0]["name"] == f"img-in-{PROJ_A}"
    assert r.json()["project_id"] == PROJ_A
    assert len(_CALLS) == 1

    assert _client.get("/api/gcp/custom-images").status_code == 200
    assert len(_CALLS) == 1, "second GET should hit the cache"

    # Operator repoints the dashboard at another project in Settings.
    _STATE["project"] = PROJ_B
    r = _client.get("/api/gcp/custom-images")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["images"][0]["name"] == f"img-in-{PROJ_B}"
    assert r.json()["project_id"] == PROJ_B
    assert len(_CALLS) == 2, "project change must re-fetch"
    assert PROJ_A not in r.text


# ── GET /api/gcp/instances ────────────────────────────────────────────────────

def test_instances_cached_then_project_change_is_a_miss():
    _install_stubs()
    _reset()

    r = _client.get("/api/gcp/instances")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["project_id"] == PROJ_A
    assert r.json()["instances"][0]["instance_name"] == f"vm-in-{PROJ_A}"
    assert len(_CALLS) == 1

    assert _client.get("/api/gcp/instances").status_code == 200
    assert len(_CALLS) == 1, "second GET should hit the cache"

    # The payload embeds project_id, so a stale serve here does not just show old
    # instances — it mislabels which project they are in.
    _STATE["project"] = PROJ_B
    r = _client.get("/api/gcp/instances")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["project_id"] == PROJ_B, \
        "the cached payload's embedded project_id must not survive the switch"
    assert r.json()["instances"][0]["instance_name"] == f"vm-in-{PROJ_B}"
    assert len(_CALLS) == 2, "project change must re-fetch"
    assert PROJ_A not in r.text


def test_instances_write_key_matches_read_key():
    """``_build_gcp_instances`` writes; ``list_instances`` and
    ``_gcp_instances_unfiltered`` read. All three go through ``_instances_cache_key``,
    because a write/read key that disagree is a permanent silent miss."""
    _install_stubs()
    _reset()

    async def _exercise():
        await gcp._build_gcp_instances(_FakeDB(), PROJ_A)
        assert await cache_service.get(gcp.instances_cache_key(PROJ_A)) is not None, \
            "the read key must find what the build path wrote"
        # The other reader must find it too, without a second cloud call.
        before = len(_CALLS)
        found = await gcp._gcp_instances_unfiltered(_FakeDB(), PROJ_A)
        assert found and len(_CALLS) == before, "_gcp_instances_unfiltered must hit cache"
        # A different project must not see it.
        assert await cache_service.get(gcp.instances_cache_key(PROJ_B)) is None

    asyncio.run(_exercise())


# ── The write keys and the invalidates must agree ─────────────────────────────

def test_invalidate_prefix_clears_every_project():
    """The deploy/destroy runners (gcp_vm_service) and the image-delete route clear by
    prefix. An exact-key ``invalidate(key_global(...))`` would match nothing now."""
    _install_stubs()
    _reset()
    assert _client.get("/api/gcp/instances").status_code == 200
    assert _client.get("/api/gcp/custom-images").status_code == 200
    _STATE["project"] = PROJ_B
    assert _client.get("/api/gcp/instances").status_code == 200
    assert _client.get("/api/gcp/custom-images").status_code == 200
    assert len(_CALLS) == 4

    async def _exercise():
        assert await cache_service.invalidate_prefix("gcp_instances") == 2, \
            "both projects' instance entries must be cleared"
        assert await cache_service.invalidate_prefix("gcp_custom_images") == 2, \
            "both projects' image entries must be cleared"
        for project in (PROJ_A, PROJ_B):
            assert await cache_service.get(gcp.instances_cache_key(project)) is None
            assert await cache_service.get(
                gcp.custom_images_cache_key(project)) is None

    asyncio.run(_exercise())

    # Re-fetches afterwards, proving the clear was real and not a no-op.
    assert _client.get("/api/gcp/instances").status_code == 200
    assert _client.get("/api/gcp/custom-images").status_code == 200
    assert len(_CALLS) == 6


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
