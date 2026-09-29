"""Deleting an Azure private image must use the delete that matches its SOURCE.

Live failure this pins (2026-09-29): on /azure → Private Images, Delete did nothing
for any "Gallery" row. The route only ever called ``compute.images.begin_delete``
(standalone managed images) in the flat ``azure_resource_group``. A gallery row is
a gallery image DEFINITION, which that API does not address, and ARM answers a
DELETE of an absent resource with 204 — so the call "succeeded", the toast said
deleted, and the row came straight back.

What these pin:

- ``source=gallery`` deletes every version and then the definition, in the gallery
  and gallery RG from REGION CONFIG — never from the caller;
- ``source=managed`` deletes in the row's resource group, but only if it is one of
  the groups the listing scanned (the gallery RG or the VM RG); anything else is
  refused, so the param cannot aim a delete at an arbitrary group;
- a gallery-version delete that ARM refuses with 403 says the SP needs
  Contributor, rather than surfacing a bare AuthorizationFailed.

fastapi/httpx are probed by name and SKIP the file when absent; the first-party
imports are unguarded, so a broken one fails instead of skipping.

Run: python tests/test_azure_delete_gallery_image.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401 — TestClient's transport
except ModuleNotFoundError as exc:  # pragma: no cover — app deps absent outside CI
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_dashboard.api import azure
from web_dashboard.api.auth import get_current_user
from web_dashboard.database import get_db
from web_dashboard.services import azure_service


VM_RG = "vm-cli-rg"
GALLERY_RG = "gallery-rg"
GALLERY = "dashboard_gallery"

# The route tests stub these on the shared module; the service tests need the real ones.
_REAL_DELETE_GALLERY_IMAGE = azure_service.delete_gallery_image


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


_CALLS: list = []


def _client():
    del _CALLS[:]
    from web_dashboard.services import region_config
    region_config.resolve_azure_region = lambda loc: {
        "gallery_name": GALLERY, "gallery_resource_group": GALLERY_RG, "resource_group": VM_RG,
    }

    async def _del_managed(rg, name):
        _CALLS.append(("managed", rg, name))

    async def _del_gallery(grg, gallery, name):
        _CALLS.append(("gallery", grg, gallery, name))

    async def _noop(*_a, **_k):
        return None

    azure._loc = lambda: "centralus"
    azure.azure_service.delete_image = _del_managed
    azure.azure_service.delete_gallery_image = _del_gallery
    azure.cache_service.invalidate_prefix = _noop
    azure.job_service.log_audit = lambda *a, **k: None

    app = FastAPI()
    app.include_router(azure.router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


def test_gallery_row_deletes_the_definition_from_config():
    c = _client()
    r = c.delete("/api/azure/images/ot-sim-cell?source=gallery&resource_group=evil-rg")
    assert r.status_code == 200, r.text
    # The caller's resource_group is ignored for gallery rows.
    assert _CALLS == [("gallery", GALLERY_RG, GALLERY, "ot-sim-cell")]


def test_managed_row_uses_its_own_scanned_rg():
    c = _client()
    r = c.delete(f"/api/azure/images/debian12?source=managed&resource_group={GALLERY_RG}")
    assert r.status_code == 200, r.text
    assert _CALLS == [("managed", GALLERY_RG, "debian12")]
    # No RG given (older callers) → the region's VM RG.
    _CALLS.clear()
    assert c.delete("/api/azure/images/debian12").status_code == 200
    assert _CALLS == [("managed", VM_RG, "debian12")]


def test_unscanned_rg_and_bad_source_are_refused():
    c = _client()
    assert c.delete("/api/azure/images/x?source=managed&resource_group=someone-elses-rg").status_code == 400
    assert c.delete("/api/azure/images/x?source=vhd").status_code == 400
    assert _CALLS == []


class _Poller:
    def __init__(self, log, what):
        self._log, self._what = log, what

    def result(self):
        self._log.append(self._what)


class _Version:
    def __init__(self, name):
        self.name = name


def test_gallery_delete_removes_versions_before_definition():
    log: list = []

    class _Versions:
        def list_by_gallery_image(self, rg, g, d):
            return [_Version("1.0.0"), _Version("1.0.1")]

        def begin_delete(self, rg, g, d, v):
            return _Poller(log, ("version", v))

    class _Defs:
        def begin_delete(self, rg, g, d):
            assert len(log) == 2, "definition deleted before its versions finished"
            return _Poller(log, ("definition", d))

    class _Compute:
        gallery_image_versions = _Versions()
        gallery_images = _Defs()

    orig = azure_service._get_compute
    azure_service._get_compute = lambda cred, sub: _Compute()
    try:
        azure_service._delete_gallery_image_sync(None, "sub", GALLERY_RG, GALLERY, "img")
    finally:
        azure_service._get_compute = orig
    assert log == [("version", "1.0.0"), ("version", "1.0.1"), ("definition", "img")]


def test_gallery_delete_403_names_the_missing_role():
    class _Forbidden(Exception):
        status_code = 403

    async def _creds():
        return None, "sub"

    def _boom(*_a):
        raise _Forbidden("AuthorizationFailed")

    orig_c, orig_s = azure_service._ensure_creds, azure_service._delete_gallery_image_sync
    azure_service._ensure_creds = _creds
    azure_service._delete_gallery_image_sync = _boom
    try:
        asyncio.run(_REAL_DELETE_GALLERY_IMAGE(GALLERY_RG, GALLERY, "img"))
        raise AssertionError("expected AzureError")
    except azure_service.AzureError as e:
        assert "Contributor" in str(e) and GALLERY_RG in str(e)
    finally:
        azure_service._ensure_creds, azure_service._delete_gallery_image_sync = orig_c, orig_s


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
