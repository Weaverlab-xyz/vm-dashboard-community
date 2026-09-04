"""Unit tests for the chunked (multipart) asset upload lane.

The feature exists so a ~311 MB installer can reach a cloud storage backend from the
browser at all: the inline lane is base64-in-JSON and caps at 64 MB. Three surfaces meet
here and each has a distinct way of going wrong:

  * ``storage_chunked`` layout — the server, not the client, decides the part size and the
    part count. S3 and OCI reject undersized non-final parts at COMMIT (after the whole
    file has been sent), and GCS resumable uploads are positional, so a part of the wrong
    length has to be refused at that part.
  * ``storage_chunked`` session handle — there is no session row in the database, because
    ``gunicorn -w 2`` means consecutive parts land in different processes. The handle is
    an encrypted blob held by the client, so its authentication is load-bearing: a
    hand-written plain-text handle must not be accepted, since the handle names the
    destination key.
  * per-backend staging — one shape for four object stores. The Azure commit in particular
    must recompute its block ids from the part numbers rather than trusting the ones the
    browser echoes back.

Pure Python: the cloud clients and ``storage_service``'s config reader are stubbed, so no
DB, no FastAPI app and no cloud credentials are needed. Runs under pytest, or standalone:
    python tests/test_storage_chunked_upload.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Fernet key derivation reads this; any stable value works for a round trip.
os.environ.setdefault("JWT_SECRET_KEY", "chunked-upload-test-secret")


def _stub_azure_blob_module():
    """Install a minimal ``azure.storage.blob`` only when the real SDK is absent.

    CI installs the full requirements, so the real ``BlobBlock`` is used there. Locally
    the SDK may not be present, and the commit path's ``from azure.storage.blob import
    BlobBlock`` would fail for a reason that has nothing to do with what is being tested.
    """
    try:
        import azure.storage.blob  # noqa: F401
        return
    except Exception:
        pass

    class BlobBlock:
        def __init__(self, block_id):
            self.id = block_id

    azure_mod = sys.modules.setdefault("azure", types.ModuleType("azure"))
    storage_mod = sys.modules.setdefault("azure.storage", types.ModuleType("azure.storage"))
    blob_mod = types.ModuleType("azure.storage.blob")
    blob_mod.BlobBlock = BlobBlock
    sys.modules["azure.storage.blob"] = blob_mod
    azure_mod.storage = storage_mod
    storage_mod.blob = blob_mod


_stub_azure_blob_module()

from web_dashboard.services import storage_chunked as SC          # noqa: E402
from web_dashboard.services import storage_service as SS          # noqa: E402
from web_dashboard.services.storage_service import (              # noqa: E402
    StorageError, UploadTooLarge,
)

_CFG = {
    "storage_azure_account":   "povstorage",
    "storage_azure_container": "playbooks",
    "storage_azure_prefix":    "config-mgmt",
    "storage_s3_bucket":       "a-bucket",
    "storage_s3_prefix":       "config-mgmt",
    "storage_gcs_bucket":      "a-gcs-bucket",
    "storage_gcs_prefix":      "config-mgmt",
}

# Neither of these may touch the database in a unit test: `_validate_backend` asks
# config_service whether a backend is configured, and `_cfg` reads app_config rows.
SS._validate_backend = lambda backend: None
SS._cfg = lambda key: _CFG.get(key, "")


def _run(coro):
    return asyncio.run(coro)


# ── The layout the server imposes ────────────────────────────────────────────

def test_a_311mb_installer_becomes_39_parts_with_a_short_last_one():
    """The real file that motivated this: BeyondTrust.Agents.Bootstrapper.exe."""
    total = 311 * 1024 * 1024
    assert SC.part_count(total) == 39
    assert SC.expected_part_bytes(total, 1) == SC.PART_BYTES
    assert SC.expected_part_bytes(total, 38) == SC.PART_BYTES
    last = SC.expected_part_bytes(total, 39)
    assert 0 < last < SC.PART_BYTES
    assert 38 * SC.PART_BYTES + last == total


def test_the_part_size_clears_the_minimum_s3_and_oci_enforce_at_commit():
    """5 MiB is the floor for a non-final part on S3 and OCI, and both apply it when the
    upload is COMPLETED — i.e. after every byte has already been sent. A part size below
    it would make every multi-part upload fail at the last possible moment."""
    assert SC.PART_BYTES >= 5 * 1024 * 1024
    # And a multiple of 256 KiB, which GCS resumable uploads require of every part but
    # the last.
    assert SC.PART_BYTES % (256 * 1024) == 0


def test_a_part_of_the_wrong_length_is_refused_at_that_part():
    """Not at commit. The whole point of chunking is that a mistake costs one part."""
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 3 * SC.PART_BYTES, "parts": 3, "user": "alice",
    })
    try:
        _run(SC.stage_part(handle, 1, b"x" * 1024, username="alice"))
    except StorageError as e:
        assert "expected" in str(e), e
        # Names the fixed size, so the operator (or a script) can correct the slicing.
        assert str(SC.PART_BYTES) in str(e), e
    else:
        raise AssertionError("a short non-final part was accepted")


def test_a_part_number_outside_the_upload_is_refused():
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 100, "parts": 1, "user": "alice",
    })
    for bad in (0, 2, 99):
        try:
            _run(SC.stage_part(handle, bad, b"x" * 100, username="alice"))
        except StorageError:
            pass
        else:
            raise AssertionError(f"part {bad} was accepted for a one-part upload")


# ── The session handle is what names the destination ─────────────────────────

def test_a_handwritten_plaintext_handle_is_refused():
    """The property the whole session design rests on.

    ``config_service.decrypt_value`` returns its INPUT unchanged when it cannot decrypt —
    correct for reading a config row written before encryption, and fatal here: it would
    make this JSON string a valid handle, and the handle is what says which key to write.
    ``decrypt_value_strict`` exists for this call site.
    """
    forged = ('{"backend":"azure_blob","name":"evil.exe","key":"../../evil.exe",'
              '"ref":"","total":100,"parts":1,"user":"alice",'
              '"marker":"vmdash-chunked-upload-1"}')
    try:
        SC._decode_handle(forged, "alice")
    except StorageError:
        return
    raise AssertionError("a plain-text handle was accepted — the destination key is "
                         "attacker-controlled")


def test_a_tampered_handle_is_refused():
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 100, "parts": 1, "user": "alice",
    })
    try:
        SC._decode_handle(handle[:-6] + "aaaaaa", "alice")
    except StorageError:
        return
    raise AssertionError("a mutated handle was accepted")


def test_a_handle_stays_bound_to_the_user_it_was_minted_for():
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 100, "parts": 1, "user": "alice",
    })
    assert SC._decode_handle(handle, "alice")["name"] == "x.exe"
    try:
        SC._decode_handle(handle, "bob")
    except StorageError:
        return
    raise AssertionError("one user's upload session was usable by another")


def test_the_handle_does_not_expose_the_stores_upload_reference():
    """A GCS resumable session URI is a write capability that authenticates by
    possession. The page holds the handle, so the handle must be opaque."""
    uri = "https://storage.googleapis.com/upload/resumable?upload_id=SECRET-CAPABILITY"
    handle = SC._encode_handle({
        "backend": "gcs", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": uri, "total": 100, "parts": 1, "user": "alice",
    })
    assert "SECRET-CAPABILITY" not in handle
    assert "storage.googleapis.com" not in handle
    assert SC._decode_handle(handle, "alice")["ref"] == uri


# ── begin: everything that can be refused before a byte is sent ──────────────

def test_an_unsupported_extension_is_refused_before_any_part():
    try:
        _run(SC.begin_upload("azure_blob", "notes.txt", 100 * 1024 * 1024,
                             username="alice"))
    except StorageError as e:
        assert "Unsupported file type" in str(e), e
        return
    raise AssertionError("a .txt was accepted, and would be refused only at the end")


def test_a_file_over_the_chunked_ceiling_is_refused_as_too_large_not_as_an_error():
    """UploadTooLarge and not a bare StorageError, because the endpoint answers 413 on
    the distinct type and folds everything else into a 502."""
    try:
        _run(SC.begin_upload("azure_blob", "big.exe",
                             SC.MAX_CHUNKED_UPLOAD_BYTES + 1, username="alice"))
    except UploadTooLarge as e:
        # Names the remedy: an object this big belongs in the bucket out of band.
        assert "out of band" in str(e), e
        return
    raise AssertionError("a file over the ceiling was accepted")


def test_a_filesystem_backend_has_no_chunked_lane_and_the_refusal_names_the_remedy():
    """Same rule the inline ceiling's message follows: an operator told only "no" goes
    looking for a setting to raise, and for these backends there is none to find."""
    for backend in ("local", "agent_local"):
        assert not SC.supports_chunked_upload(backend)
        assert "use a cloud backend" in SC.chunked_upload_reason(backend), backend
        try:
            _run(SC.begin_upload(backend, "x.exe", 300 * 1024 * 1024, username="alice"))
        except StorageError as e:
            assert "cannot accept a chunked upload" in str(e), e
        else:
            raise AssertionError(f"{backend} accepted a chunked upload")


def test_the_chunked_backends_are_exactly_the_ones_that_can_be_addressed_by_key():
    """A new cloud backend gets both surfaces or neither. Both tables answer "is this an
    object store"; the drift shape is a backend that can be presigned but silently has no
    chunked lane, which reads to an operator as an arbitrary size limit."""
    assert set(SC._CHUNKED_OPS) == set(SS._ASSET_PREFIX_FN)
    for backend, ops in SC._CHUNKED_OPS.items():
        assert set(ops) == {"begin", "stage", "commit", "abort"}, backend


# ── commit: the parts list is checked before anything is written ─────────────

def _azure_recorder():
    """A fake Azure blob client that records what it was asked to stage and commit."""
    calls = {"staged": [], "committed": None}

    class FakeBlobClient:
        def stage_block(self, block_id, data, length=None):
            calls["staged"].append((block_id, len(data)))

        def commit_block_list(self, blocks):
            calls["committed"] = [b.id for b in blocks]

    class FakeService:
        def get_blob_client(self, container, blob):
            calls.setdefault("target", (container, blob))
            return FakeBlobClient()

    return calls, FakeService()


def test_azure_commits_block_ids_it_derived_itself_in_part_order():
    calls, svc = _azure_recorder()
    SS._azure_blob_client = lambda: svc
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 3, "parts": 3, "user": "alice",
    })
    # Shuffled, and with refs the client made up. Neither may influence the commit: the
    # ids come from the part NUMBERS, so the browser cannot name a block.
    _run(SC.commit_upload(handle, [
        {"part": 3, "ref": "junk-3"},
        {"part": 1, "ref": "junk-1"},
        {"part": 2, "ref": "../../etc/passwd"},
    ], username="alice"))
    assert calls["committed"] == [SC._azure_block_id(n) for n in (1, 2, 3)], \
        calls["committed"]
    assert calls["target"] == ("playbooks", "config-mgmt/x.exe"), calls["target"]


def test_azure_block_ids_are_equal_length_which_azure_requires():
    ids = [SC._azure_block_id(n) for n in (1, 9, 10, 999, 50000)]
    assert len({len(i) for i in ids}) == 1, ids


def test_a_missing_part_refuses_the_commit_and_writes_nothing():
    """A commit that silently dropped a part would produce a truncated installer — one
    that copies to the target and fails there, a long way from this code."""
    calls, svc = _azure_recorder()
    SS._azure_blob_client = lambda: svc
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 3, "parts": 3, "user": "alice",
    })
    try:
        _run(SC.commit_upload(handle, [{"part": 1, "ref": ""}, {"part": 3, "ref": ""}],
                              username="alice"))
    except StorageError as e:
        assert "never arrived" in str(e), e
        assert "2" in str(e), e            # names the first missing part
        assert calls["committed"] is None, "a partial upload was committed"
        return
    raise AssertionError("a commit missing a part succeeded")


def test_a_part_listed_twice_is_refused():
    calls, svc = _azure_recorder()
    SS._azure_blob_client = lambda: svc
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 2, "parts": 2, "user": "alice",
    })
    try:
        _run(SC.commit_upload(handle, [{"part": 1, "ref": ""}, {"part": 1, "ref": ""}],
                              username="alice"))
    except StorageError as e:
        assert "twice" in str(e), e
        assert calls["committed"] is None
        return
    raise AssertionError("a duplicated part was accepted")


# ── S3 and GCS staging shapes ────────────────────────────────────────────────

def test_s3_completes_with_the_etags_the_client_collected_in_part_order():
    """Unlike Azure, S3's part references ARE the client's to carry: an ETag is minted by
    S3 per part and returned through the browser. What must not drift is the ORDER."""
    seen = {}

    class FakeS3:
        def complete_multipart_upload(self, **kw):
            seen.update(kw)

    SS._s3_client = lambda: FakeS3()
    handle = SC._encode_handle({
        "backend": "s3", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "upload-id-1", "total": 2, "parts": 2, "user": "alice",
    })
    _run(SC.commit_upload(handle, [{"part": 2, "ref": '"etag2"'},
                                   {"part": 1, "ref": '"etag1"'}], username="alice"))
    assert seen["UploadId"] == "upload-id-1"
    assert seen["MultipartUpload"]["Parts"] == [
        {"PartNumber": 1, "ETag": '"etag1"'},
        {"PartNumber": 2, "ETag": '"etag2"'},
    ], seen["MultipartUpload"]


def test_gcs_puts_a_positional_content_range_and_does_not_follow_the_308():
    """Two things that are silently wrong if unpinned:

      * the byte offset is computed here from the part number, so it must be in the
        header — a resumable upload writes wherever the range says;
      * 308 "Resume Incomplete" is the SUCCESS answer for every part but the last, and
        ``requests`` treats 308 as a redirect. Following it re-PUTs the part with no
        Content-Range at all.
    """
    import requests
    recorded = {}

    class FakeResp:
        status_code = 308
        text = ""

    def fake_put(url, data=None, headers=None, allow_redirects=None, timeout=None):
        recorded.update(url=url, headers=headers, allow_redirects=allow_redirects,
                        length=len(data))
        return FakeResp()

    real_put = requests.put
    requests.put = fake_put
    try:
        total = 3 * SC.PART_BYTES
        handle = SC._encode_handle({
            "backend": "gcs", "name": "x.exe", "key": "config-mgmt/x.exe",
            "ref": "https://example.invalid/session", "total": total, "parts": 3,
            "user": "alice",
        })
        _run(SC.stage_part(handle, 2, b"z" * SC.PART_BYTES, username="alice"))
    finally:
        requests.put = real_put

    start = SC.PART_BYTES
    end = 2 * SC.PART_BYTES - 1
    assert recorded["headers"]["Content-Range"] == f"bytes {start}-{end}/{total}", \
        recorded["headers"]
    assert recorded["allow_redirects"] is False


def test_gcs_commit_verifies_the_object_rather_than_assuming_it():
    """A resumable upload finalises itself, so "commit" here is only the check that it
    did. Without it a lost final part reports success and leaves nothing in the bucket."""
    class FakeBucket:
        def __init__(self, blob):
            self._blob = blob

        def get_blob(self, key):
            return self._blob

    class FakeClient:
        def __init__(self, blob):
            self._blob = blob

        def bucket(self, name):
            return FakeBucket(self._blob)

    handle = SC._encode_handle({
        "backend": "gcs", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "https://example.invalid/session", "total": 100, "parts": 1,
        "user": "alice",
    })

    SS._gcs_client = lambda: FakeClient(None)
    try:
        _run(SC.commit_upload(handle, [{"part": 1, "ref": ""}], username="alice"))
    except StorageError as e:
        assert "did not finalise" in str(e), e
    else:
        raise AssertionError("a missing GCS object committed successfully")

    short = types.SimpleNamespace(size=42)
    SS._gcs_client = lambda: FakeClient(short)
    try:
        _run(SC.commit_upload(handle, [{"part": 1, "ref": ""}], username="alice"))
    except StorageError as e:
        assert "42" in str(e), e
    else:
        raise AssertionError("a short GCS object committed successfully")


def test_aborting_an_azure_upload_does_not_delete_the_blob():
    """There is nothing to abort on Azure: uncommitted blocks belong to no blob and
    expire on their own. Deleting the key would delete a PREVIOUS good upload of the same
    asset — a cancelled retry taking out the file it was replacing."""
    deleted = []

    class FakeBlobClient:
        def delete_blob(self):
            deleted.append(True)

    class FakeService:
        def get_blob_client(self, container, blob):
            return FakeBlobClient()

    SS._azure_blob_client = lambda: FakeService()
    handle = SC._encode_handle({
        "backend": "azure_blob", "name": "x.exe", "key": "config-mgmt/x.exe",
        "ref": "", "total": 100, "parts": 1, "user": "alice",
    })
    assert _run(SC.abort_upload(handle, username="alice"))["aborted"] is True
    assert not deleted


# ── What the page is told ────────────────────────────────────────────────────

def test_the_form_ceiling_is_the_chunked_one_for_an_object_store():
    """`max_upload_bytes` stays the INLINE ceiling — `check_inline_upload` enforces it
    when a base64 body arrives — while the form advertises what it can actually do."""
    assert SS.max_form_upload_bytes("azure_blob") == SC.MAX_CHUNKED_UPLOAD_BYTES
    assert SS.max_form_upload_bytes("azure_blob") > SS.max_upload_bytes("azure_blob")
    assert SS.max_upload_bytes("azure_blob") == SS.MAX_INLINE_UPLOAD_BYTES


def test_the_form_ceiling_is_unchanged_for_a_backend_with_no_chunked_lane():
    for backend in ("local", "agent_local"):
        assert SS.max_form_upload_bytes(backend) == SS.max_upload_bytes(backend), backend


def test_every_backend_a_pov_can_configure_has_the_chunked_lane():
    """A POV instance is the reason this lane exists — it is where a Resource Broker
    installer has to land — and it can only configure storage through the wizard's
    `povstorage` step. So the two sets are coupled: a provider offered there whose backend
    has no chunked lane would put a POV back where it started, and the symptom would be a
    size limit rather than a missing feature.

    Read out of the source rather than by importing api/setup: this file is a pure-Python
    unit test and that module pulls in the app.
    """
    import ast

    path = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    spec = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_POV_STORAGE_SPEC" for t in node.targets
        ):
            spec = ast.literal_eval(node.value)
    assert spec, "_POV_STORAGE_SPEC not found — did the POV storage wizard step move?"
    for provider, entry in spec.items():
        assert SC.supports_chunked_upload(entry["backend"]), (
            f"a POV can configure {provider} → {entry['backend']}, which has no chunked "
            f"upload lane, so an installer there is capped at "
            f"{SS.MAX_INLINE_UPLOAD_BYTES} bytes")


def test_the_lane_reads_nothing_about_the_install_profile():
    """Profile-neutral by construction, on both the demo and the POV instance.

    `/storage` and `/api/storage` are ungated on purpose (unlike the cloud consoles, which
    `_profile_page_gate` 404s on a POV). Storage is how a POV stages the installer it
    exists to demo, so a profile check appearing anywhere in this lane would be a
    regression — and one that reads to an operator as a broken upload, not as a gate.
    """
    for rel in (("web_dashboard", "api", "storage.py"),
                ("web_dashboard", "services", "storage_chunked.py")):
        with open(os.path.join(_ROOT, *rel), encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in ("install_profile", "profile_page_allowed", "feature_flags"):
            assert forbidden not in src, f"{rel[-1]} gates the upload lane on {forbidden}"


def test_the_endpoint_checks_the_declared_part_size_before_reading_the_body():
    """`await request.body()` buffers whatever arrives, so a Content-Length check after
    it is no check at all — it moves the allocation this lane exists to avoid from the
    browser to the worker."""
    path = os.path.join(_ROOT, "web_dashboard", "api", "storage.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    section = src[src.index("async def stage_chunked_upload_part"):][:3000]
    # Both matched on the CODE and not on the prose: the docstring above them says the
    # same thing in the other order, and a test that reads a comment proves nothing.
    guard = section.index('request.headers.get("content-length")')
    read = section.index("data = await request.body()")
    assert guard < read, "the size guard runs after the body has already been buffered"


def test_the_handle_travels_in_a_header_and_not_in_the_url():
    """It carries the store's upload reference — for GCS a write capability — and URLs
    end up in access logs."""
    path = os.path.join(_ROOT, "web_dashboard", "api", "storage.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "X-Upload-Handle" in src
    assert "/upload/part/{part_number}" in src
    assert "handle" not in "/upload/part/{part_number}"


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
