"""An asset name can only ever address an asset: never a file outside the storage path.

``/api/storage/fetch/{backend}/{name:path}`` and its siblings hand ``name`` straight to the
backend. For local storage that was joined onto the base path unchecked, so
``..%2f..%2frun%2fsecrets%2fjwt_key`` read the JWT signing key (which also derived the
at-rest encryption key), an upload could write anywhere the app user can, and a delete
could remove the database. Object stores never needed a ``..`` either.

Checked twice, and both are pinned here: ``_check_asset_name`` refuses the name for every
backend before any operation, and ``_local_path_for`` resolves the real path and requires it
to stay in the base, which is what also stops a symlink placed inside the base.

Runs standalone:
    python tests/test_storage_asset_name_containment.py
"""
import asyncio
import io
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="storage-containment-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "storage-containment-test-secret")

from web_dashboard.services import storage_service as SS  # noqa: E402
from web_dashboard.services.storage_service import StorageError  # noqa: E402

_BASE = os.path.join(_TMPDIR, "assets")
os.makedirs(_BASE)
_SECRET = os.path.join(_TMPDIR, "jwt_key")
with open(_SECRET, "w", encoding="utf-8") as fh:
    fh.write("the-signing-key")
with open(os.path.join(_BASE, "site.yml"), "w", encoding="utf-8") as fh:
    fh.write("- hosts: all\n")
os.symlink(_SECRET, os.path.join(_BASE, "escape.yml"))

_CFG = {"storage_local_path": _BASE}
# No database in a unit test: these two are what read app_config.
SS._validate_backend = lambda backend: None
SS._cfg = lambda key: _CFG.get(key, "")

_TRAVERSALS = (
    "../jwt_key", "../../etc/passwd", "/etc/passwd", "sub/../../jwt_key", "..",
    ".", "", "a\\..\\jwt_key", "site.yml\x00.sh", "a//b.yml", "./site.yml",
)


def _run(coro):
    return asyncio.run(coro)


def _refused(coro):
    try:
        _run(coro)
    except StorageError:
        return True
    return False


def test_a_real_asset_still_round_trips():
    assert _run(SS.fetch_asset_in("local", "site.yml")) == b"- hosts: all\n"
    _run(SS.upload_asset_to("local", "new.sh", b"echo hi\n"))
    assert _run(SS.fetch_asset_in("local", "new.sh")) == b"echo hi\n"
    _run(SS.delete_asset_in("local", "new.sh"))
    assert not os.path.exists(os.path.join(_BASE, "new.sh"))


def test_fetch_upload_and_delete_refuse_every_traversal_on_local():
    for name in _TRAVERSALS:
        assert _refused(SS.fetch_asset_in("local", name)), f"fetch accepted {name!r}"
        assert _refused(SS.delete_asset_in("local", name)), f"delete accepted {name!r}"
    for name in ("../evil.sh", "/tmp/evil.sh", "sub/../../evil.sh", "a\\..\\evil.sh"):
        assert _refused(SS.upload_asset_to("local", name, b"x")), f"upload accepted {name!r}"
    assert not os.path.exists(os.path.join(_TMPDIR, "evil.sh"))
    with open(_SECRET, encoding="utf-8") as fh:
        assert fh.read() == "the-signing-key", "a refused operation still touched the file"


def test_local_storage_has_no_subfolders():
    """Its listing is one directory, so no legitimate name has a separator."""
    os.makedirs(os.path.join(_BASE, "sub"), exist_ok=True)
    with open(os.path.join(_BASE, "sub", "x.yml"), "w", encoding="utf-8") as fh:
        fh.write("x")
    assert _refused(SS.fetch_asset_in("local", "sub/x.yml"))


def test_a_symlink_inside_the_base_cannot_point_out_of_it():
    """The name is clean; only resolving the real path catches this one."""
    assert _refused(SS.fetch_asset_in("local", "escape.yml"))


def test_object_stores_refuse_traversal_but_keep_nested_keys():
    """S3 and friends list recursively, so `sub/play.yml` is a real key there."""
    SS._check_asset_name("s3", "sub/play.yml")
    for backend in ("s3", "azure_blob", "gcs", "oci_object_storage", "agent_local"):
        for name in ("../x.yml", "a/../../x.yml", "/abs.yml", "a\\b.yml", "", "a//b.yml"):
            try:
                SS._check_asset_name(backend, name)
            except StorageError:
                continue
            raise AssertionError(f"{backend} accepted {name!r}")


def test_every_operation_checks_the_name_before_reaching_a_backend():
    """The check sits in _run_op, so a backend function never sees a bad name -- including
    a backend added later."""
    seen = []
    saved = SS._BACKEND_OPS["s3"]
    SS._BACKEND_OPS["s3"] = {op: (lambda *a, _op=op: seen.append((_op, a)))
                             for op in saved}
    try:
        for op, args in (("fetch", ("../x.yml",)), ("upload", ("../x.yml", b"x")),
                         ("delete", ("../x.yml",))):
            assert _refused(SS._run_op("s3", op, *args)), op
        assert not seen, f"a backend function ran with a bad name: {seen}"
    finally:
        SS._BACKEND_OPS["s3"] = saved


def test_image_keys_stay_inside_the_base():
    """Image keys have subfolders (images/<name>-<ts>.<ext>) but no way out."""
    _run(SS.upload_image_to("local", "images/win-1.vhd", io.BytesIO(b"disk")))
    assert os.path.exists(os.path.join(_BASE, "images", "win-1.vhd"))
    for key in ("../escape.vhd", "images/../../escape.vhd", "/tmp/escape.vhd"):
        assert _refused(SS.upload_image_to("local", key, io.BytesIO(b"x"))), key
    assert not os.path.exists(os.path.join(_TMPDIR, "escape.vhd"))


def test_the_route_returns_an_error_not_the_file():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import auth, storage

    class _Admin:
        username = "root"
        is_admin = is_effective_admin = True
        accessor_env_id = None
        effective_permissions_dict = {}

    app = FastAPI()
    app.include_router(storage.router)
    app.dependency_overrides[auth.get_current_user] = lambda: _Admin()
    client = TestClient(app)
    prefix = storage.router.prefix
    resp = client.get(f"{prefix}/fetch/local/..%2fjwt_key")
    assert resp.status_code >= 400, resp.status_code
    assert "the-signing-key" not in resp.text
    import base64
    assert base64.b64encode(b"the-signing-key").decode() not in resp.text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
