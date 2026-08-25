"""Unit tests for storage_service.copy() — the cross-backend image copy.

The property that matters here was learned in production: the GCS→Azure hop of
a GCP image export staged the whole VHD through tempfile.mkstemp on the
dashboard container, whose ephemeral disk is single-digit GiB on ACA. The job
died at 99% with "[Errno 28] No space left on device" and — because the
post-build auto-export is non-fatal — the built image silently never appeared
in the image registry.

So the pinned behaviors are:
  1. A cross-backend copy INTO azure_blob never touches local disk: the source
     is presigned and Azure's storage service pulls each block itself
     (stage_block_from_url), with correct offsets/lengths and a final commit.
  2. The remaining temp-file fallback turns ENOSPC into a StorageError that
     names the fix (storage_hub_backend), instead of a bare errno.
  3. Same-backend copies still use the SDK server-side copy op.
  4. A `local` source can't be presigned and stays on the temp-file path.

Pure Python: config_service and cloud_executor are stubbed, no DB, no cloud
SDKs required. Runs under pytest, or standalone:
    python tests/test_storage_cross_backend_copy.py
"""
import asyncio
import errno
import importlib.util
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _install_stubs():
    """Stub parent packages so storage_service's relative imports resolve while
    it is loaded by file path — mirrors test_cloud_executor._install_stubs."""
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = []
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg

    # Pass-through executor: copy() only cares that the sync fn ran and that
    # its exceptions propagate untouched, which is the real contract too.
    ce = types.ModuleType("web_dashboard.services.cloud_executor")

    class CloudCallError(Exception):
        pass

    async def run(_provider, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    ce.CloudCallError = CloudCallError
    ce.run = run
    sys.modules["web_dashboard.services.cloud_executor"] = ce
    services.cloud_executor = ce

    # BlobBlock is imported inside _azure_pull_image_from_url_sync; provide a
    # stand-in so the tests run on a bare checkout with no azure SDK.
    try:
        import azure.storage.blob  # noqa: F401
    except ImportError:
        azure_pkg = sys.modules.setdefault("azure", types.ModuleType("azure"))
        azure_pkg.__path__ = []
        storage_pkg = types.ModuleType("azure.storage")
        storage_pkg.__path__ = []
        sys.modules["azure.storage"] = storage_pkg
        blob_mod = types.ModuleType("azure.storage.blob")

        class BlobBlock:
            def __init__(self, block_id):
                self.id = block_id

        blob_mod.BlobBlock = BlobBlock
        sys.modules["azure.storage.blob"] = blob_mod


_install_stubs()
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "storage_service.py")


def _fresh(**conf):
    CONF.clear()
    CONF.update({
        "storage_azure_account": "hubacct",
        "storage_azure_container": "playbooks",
        "storage_gcs_bucket": "buildside",
        "storage_s3_bucket": "buildside-s3",
        "storage_local_path": "/srv/images",
    })
    CONF.update({k: str(v) for k, v in conf.items()})
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.storage_service", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeBlobClient:
    def __init__(self, log):
        self.log = log

    def stage_block_from_url(self, block_id, source_url, source_offset=None, source_length=None):
        self.log.append(("stage", block_id, source_url, source_offset, source_length))

    def commit_block_list(self, blocks):
        self.log.append(("commit", [b.id if hasattr(b, "id") else b.block_id for b in blocks]))

    def upload_blob(self, data, overwrite=False):
        self.log.append(("upload_blob", data, overwrite))


class _FakeBlobService:
    def __init__(self, log):
        self.log = log

    def get_blob_client(self, container, blob):
        self.log.append(("client", container, blob))
        return _FakeBlobClient(self.log)


def _forbid(_msg):
    def fn(*_a, **_k):
        raise AssertionError(_msg)
    return fn


def test_copy_into_azure_is_fully_server_side():
    mod = _fresh()
    log = []
    block = mod._AZURE_STAGE_BLOCK_SIZE
    size = 2 * block + 7  # forces two full blocks + one runt
    mod._azure_blob_client = lambda: _FakeBlobService(log)
    mod._IMAGE_OPS["gcs"]["head"] = lambda key: {"size": size, "etag": "", "content_type": "", "last_modified": None}
    mod._IMAGE_OPS["gcs"]["presign"] = lambda key, exp, m: f"https://signed.example/{key}?m={m}&exp={exp}"
    # The whole point: neither the local download nor the local upload path may run.
    mod._IMAGE_OPS["gcs"]["download"] = _forbid("GCS download ran — the VHD is being staged on local disk again")
    mod._IMAGE_OPS["azure_blob"]["upload"] = _forbid("local upload ran — the VHD is being staged on local disk again")

    progress = []
    asyncio.run(mod.copy("gcs", "images/ot-sim.vhd", "azure_blob", "images/ot-sim-hub.vhd",
                         progress_cb=progress.append))

    stages = [e for e in log if e[0] == "stage"]
    assert [(e[3], e[4]) for e in stages] == [(0, block), (block, block), (2 * block, 7)], \
        f"wrong block ranges: {[(e[3], e[4]) for e in stages]}"
    assert all(e[2].startswith("https://signed.example/images/ot-sim.vhd") for e in stages), \
        "blocks were not pulled from the presigned source URL"
    commits = [e for e in log if e[0] == "commit"]
    assert len(commits) == 1 and commits[0][1] == [e[1] for e in stages], \
        "committed block list does not match the staged blocks in order"
    assert ("client", "playbooks", "images/ot-sim-hub.vhd") in log
    assert len(progress) == 3 and "GiB" in progress[0], f"no usable progress lines: {progress}"


def test_copy_into_azure_missing_source_is_a_clean_error():
    mod = _fresh()
    mod._azure_blob_client = _forbid("blob client built even though the source is missing")
    mod._IMAGE_OPS["gcs"]["head"] = lambda key: None
    try:
        asyncio.run(mod.copy("gcs", "images/gone.vhd", "azure_blob", "images/gone.vhd"))
        raise AssertionError("copy of a missing source did not raise")
    except mod.StorageError as e:
        assert "does not exist" in str(e)


def test_fallback_enospc_names_the_fix_and_cleans_up():
    mod = _fresh()
    created = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        created.append(path)
        return fd, path

    tempfile.mkstemp = recording_mkstemp
    try:
        def explode(_key, _fileobj):
            raise OSError(errno.ENOSPC, "No space left on device")
        # azure→gcs has no server-side pull, so it exercises the temp-file path.
        mod._IMAGE_OPS["azure_blob"]["download"] = explode
        mod._IMAGE_OPS["gcs"]["upload"] = _forbid("upload ran after the download failed")
        try:
            asyncio.run(mod.copy("azure_blob", "images/big.vhd", "gcs", "images/big.vhd"))
            raise AssertionError("ENOSPC did not surface")
        except mod.StorageError as e:
            msg = str(e)
            assert "storage_hub_backend" in msg and "disk" in msg, \
                f"ENOSPC error is not actionable: {msg}"
        assert created and not os.path.exists(created[0]), "temp staging file leaked"
    finally:
        tempfile.mkstemp = real_mkstemp


def test_local_source_still_uses_the_temp_path():
    mod = _fresh()
    moved = {}
    mod._azure_blob_client = _forbid("local source cannot be presigned — must not use the server-side pull")

    def fake_download(key, fileobj):
        fileobj.write(b"vhd-bytes")
        moved["downloaded"] = key

    def fake_upload(key, fileobj):
        moved["uploaded"] = (key, fileobj.read())

    mod._IMAGE_OPS["local"]["download"] = fake_download
    mod._IMAGE_OPS["azure_blob"]["upload"] = fake_upload
    asyncio.run(mod.copy("local", "images/seed.vhd", "azure_blob", "images/seed.vhd"))
    assert moved["downloaded"] == "images/seed.vhd"
    assert moved["uploaded"] == ("images/seed.vhd", b"vhd-bytes")


def test_same_backend_still_uses_sdk_server_side_copy():
    mod = _fresh()
    calls = []
    mod._IMAGE_OPS["gcs"]["copy"] = lambda src, dst: calls.append((src, dst))
    mod._IMAGE_OPS["gcs"]["head"] = _forbid("same-backend copy should not head the source")
    asyncio.run(mod.copy("gcs", "images/a.vhd", "gcs", "images/b.vhd"))
    assert calls == [("images/a.vhd", "images/b.vhd")]


def test_destination_extension_is_still_validated():
    mod = _fresh()
    try:
        asyncio.run(mod.copy("gcs", "images/a.vhd", "azure_blob", "images/a.exe"))
        raise AssertionError("unsupported destination extension was accepted")
    except mod.StorageError as e:
        assert "isn't a supported image format" in str(e)


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as exc:                       # noqa: BLE001
                failed += 1
                print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
    print("OK" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
