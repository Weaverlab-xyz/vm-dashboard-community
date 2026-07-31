"""The AWS AMI list cache is scoped to the region it was fetched for.

An AMI id only resolves in the region that owns it, and this router is multi-region
(``DeployRequest.region``, ``?region=`` on ``/network-options``). ``aws_amis`` was
nevertheless cached under ``cache_service.key_global("aws_amis")`` — literally
``vmcli:aws_amis``, no dimensions — so whichever region warmed the cache first owned
the list for the whole 5-minute TTL. The deploy form then offered another region's ids
and EC2 rejected the launch with ``InvalidAMIID.NotFound``, which names no region and
reads like a deleted image. ``/amis`` also took no ``region`` at all, so the deploy
modal could only ever show the *default* region's AMIs.

Pinned here:
  * The key varies with region and nothing else, and stays ``invalidate_prefix``-able.
  * A change to the configured ``aws_region`` is a cache MISS — no cross-region payload
    can be served.
  * ``?region=`` scopes the lookup, caches per region, and echoes the region back so
    the form knows which region the ids it is offering belong to.
  * A malformed ``?region=`` is a 400, not a silent fall back to the default.
  * ``invalidate_prefix("aws_amis")`` still clears it — the deregister/ENA/copy paths
    switched to prefix form, and a scoped write with an exact-key invalidate is a
    silent no-op.

Follows the hermetic TestClient pattern from test_gcp_region.py. Heavy cloud deps
(fastapi/boto3/…) are only present in CI; when missing the file SKIPs cleanly so the
per-file runner stays green.

Run: python tests/test_aws_cache_scope.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.api import aws
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
    from web_dashboard.services import cache_service
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"aws api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


OHIO = "us-east-2"
IRELAND = "eu-west-1"

_CFG: dict = {}
_CALLS: list = []


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


def _reset(region=OHIO):
    """Fresh config + empty cache. The store is cleared directly rather than via
    ``invalidate`` so no event loop is needed between tests."""
    _CFG.clear()
    _CFG["aws_region"] = region
    _CALLS.clear()
    cache_service._store.clear()
    cache_service._inflight.clear()
    cache_service._pending.clear()


def _install_stubs():
    aws._aws_cfg = lambda key, fallback="": _CFG.get(key) or fallback

    async def _fake_list_amis(region):
        _CALLS.append(region)
        # AMI ids are region-local: tag them so a cross-region serve is visible.
        return [{"ami_id": f"ami-{region.replace('-', '')}0001",
                 "name": f"img-in-{region}",
                 "state": "available"}]

    aws.aws_service.list_amis = _fake_list_amis


_app = FastAPI()
_app.include_router(aws.router)
_app.dependency_overrides[get_current_user] = lambda: _AdminUser()
_app.dependency_overrides[get_db] = lambda: object()
# One client for the whole module: cache_service's asyncio.Lock must not be shared
# across event loops.
_client = TestClient(_app, raise_server_exceptions=False)


# ── Key shape ─────────────────────────────────────────────────────────────────

def test_cache_key_varies_with_region_only():
    base = aws.amis_cache_key(OHIO)
    assert aws.amis_cache_key(IRELAND) != base, \
        "region must scope the key"
    # Same region → same key, or nothing would ever hit.
    assert aws.amis_cache_key(OHIO) == base


def test_cache_key_stays_invalidate_prefix_compatible():
    """The deregister/ENA/copy paths clear by prefix; ``vmcli:aws_amis:`` must match."""
    assert aws.amis_cache_key(OHIO).startswith("vmcli:aws_amis:")


# ── GET /api/aws/amis ─────────────────────────────────────────────────────────

def test_amis_cached_then_region_change_is_a_miss():
    _install_stubs()
    _reset()

    r = _client.get("/api/aws/amis")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["amis"][0]["name"] == f"img-in-{OHIO}"
    assert r.json()["region"] == OHIO
    assert len(_CALLS) == 1

    # Second GET is served from cache — the TTL still has to work.
    assert _client.get("/api/aws/amis").status_code == 200
    assert len(_CALLS) == 1, "second GET should hit the cache"

    # Operator switches region in Settings. The cached Ohio ids must not be served
    # for Ireland: that is the InvalidAMIID.NotFound this fixes.
    _CFG["aws_region"] = IRELAND
    r = _client.get("/api/aws/amis")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["amis"][0]["name"] == f"img-in-{IRELAND}"
    assert r.json()["region"] == IRELAND
    assert len(_CALLS) == 2, "region change must re-fetch"
    assert OHIO not in r.text


def test_region_query_param_scopes_the_lookup_and_the_cache():
    _install_stubs()
    _reset()

    r = _client.get(f"/api/aws/amis?region={IRELAND}")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["amis"][0]["name"] == f"img-in-{IRELAND}"
    assert r.json()["region"] == IRELAND
    assert _CALLS == [IRELAND], "the fetcher must be called for the requested region"

    # The default region is a different key, so it must not be served the Ireland
    # payload — this is the deploy-modal bug: one region's list for every region.
    r = _client.get("/api/aws/amis")
    assert r.status_code == 200
    assert r.json()["amis"][0]["name"] == f"img-in-{OHIO}"
    assert _CALLS == [IRELAND, OHIO]

    # …and each region now has its own warm entry.
    assert _client.get(f"/api/aws/amis?region={IRELAND}").status_code == 200
    assert _client.get("/api/aws/amis").status_code == 200
    assert _CALLS == [IRELAND, OHIO], "both regions should be cached independently"


def test_blank_region_falls_back_to_configured_default():
    _install_stubs()
    _reset()
    r = _client.get("/api/aws/amis?region=")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["region"] == OHIO
    assert _CALLS == [OHIO]


def test_malformed_region_is_rejected_not_silently_defaulted():
    """A typo must not deploy-list against the default region under a bogus key."""
    _install_stubs()
    _reset()
    r = _client.get("/api/aws/amis?region=not a region")
    assert r.status_code == 400, (r.status_code, r.text)
    assert _CALLS == [], "a rejected region must not reach AWS"


# ── The write key and the invalidate must agree ───────────────────────────────

def test_invalidate_prefix_clears_every_region():
    """deregister_ami / enable_ena / the AMI-copy runner clear by prefix. An
    exact-key ``invalidate(key_global("aws_amis"))`` would match nothing now."""
    _install_stubs()
    _reset()
    assert _client.get("/api/aws/amis").status_code == 200
    assert _client.get(f"/api/aws/amis?region={IRELAND}").status_code == 200
    assert len(_CALLS) == 2

    async def _exercise():
        assert await cache_service.invalidate_prefix("aws_amis") == 2, \
            "both regions' entries must be cleared"
        for region in (OHIO, IRELAND):
            key = aws.amis_cache_key(region)
            assert await cache_service.get(key) is None

    asyncio.run(_exercise())

    # Both re-fetch afterwards, proving the clear was real and not a no-op.
    assert _client.get("/api/aws/amis").status_code == 200
    assert _client.get(f"/api/aws/amis?region={IRELAND}").status_code == 200
    assert len(_CALLS) == 4


def test_wizard_save_invalidation_reaches_the_scoped_key():
    """``api/setup.py::_invalidate_data_caches`` lists ``aws_amis`` and used to clear
    only the exact ``vmcli:aws_amis``. Now that the key is parameterised that call
    matched nothing, so a wizard save left the pre-save AMI list live for the whole
    TTL — exactly the staleness the save is meant to flush."""
    from web_dashboard.api import setup as setup_api

    _install_stubs()
    _reset()
    assert _client.get("/api/aws/amis").status_code == 200
    assert len(_CALLS) == 1

    # It belongs in the PREFIX tuple now the key is scoped: the exact-key tuple is
    # cleared with invalidate(key_global(name)), which no longer matches anything.
    assert "aws_amis" in setup_api._CONFIG_DEPENDENT_CACHE_PREFIXES
    assert "aws_amis" not in setup_api._CONFIG_DEPENDENT_CACHES
    asyncio.run(setup_api._invalidate_data_caches())

    assert _client.get("/api/aws/amis").status_code == 200
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
