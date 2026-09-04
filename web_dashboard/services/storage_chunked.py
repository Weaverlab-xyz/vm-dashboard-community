"""Chunked (multipart) asset upload — the lane a 300 MB installer takes.

`storage_service`'s ordinary upload is base64-in-JSON, buffered whole at both ends. That
is why `MAX_INLINE_UPLOAD_BYTES` exists and why it is 64 MB: above that the browser tab
dies building the base64 before a byte is sent. The comment on that constant says the fix
is "a streaming upload, not editing the constant" — this module is that upload.

WHAT THE SHAPE IS
-----------------
The browser slices the File and sends one part at a time as a RAW body; the server stages
each part straight into the object store with the store's own multipart primitive and
never holds more than one part. Peak memory is one part on each side, whatever the file
weighs.

    begin  → stage x N → commit          (or abort)

Only object stores appear here. `local` and `agent_local` are absent for the same reason
they cannot presign: a filesystem has no part-staging protocol, and `agent_local`'s ceiling
is the signed job envelope, which no amount of chunking changes.

WHY THE SESSION HAS NO ROW IN THE DATABASE
------------------------------------------
The app runs `gunicorn -w 2`, so part 1 and part 2 of one upload land in different
processes. Any session state in a module-level dict would be a coin flip (the hazard
`EphemeralState` and `LoginAttempt` are in the DB for). But the state here is small and
fixed at `begin` — backend, key, size, and the store's own upload id — so instead of a
table it rides in an ENCRYPTED HANDLE held by the client. Every worker can read it, none
has to remember it, and an abandoned upload needs no reaper: the handle simply expires,
and each store already garbage-collects uncommitted parts.

The handle is opaque on purpose. A GCS resumable session URI is a capability to write that
object — it authenticates by possession — so it must not be legible to the page holding it.

THE SERVER OWNS THE LAYOUT, NOT THE CLIENT
------------------------------------------
`begin` fixes the part size and the part count from the total, and `stage` REQUIRES each
part to be exactly the length that layout implies. That is not defensiveness for its own
sake:

  * S3 and OCI reject a completed multipart upload whose non-final parts are under 5 MiB,
    and they reject it at COMMIT — i.e. after the whole file has been sent.
  * GCS resumable uploads are positional: the part's byte offset is computed here, from the
    part number, so a short part would silently write to the wrong offset.

A client that slices differently is refused at the first part, not at the last.
"""
import base64
import json
import logging
import math
from typing import Optional

from . import storage_service
from .storage_service import StorageError

logger = logging.getLogger(__name__)


# ── Sizing ───────────────────────────────────────────────────────────────────
# 8 MiB parts. Above S3's and OCI's 5 MiB minimum for a non-final part, a multiple of the
# 256 KiB that GCS resumable uploads require, and small enough that one part is a request
# no proxy in the path treats as a large body — the corp proxy and Container Apps ingress
# both sit between the browser and here.
PART_BYTES = 8 * 1024 * 1024

# The ceiling this lane advertises. NOT the object store's limit — 8 MiB parts reach
# hundreds of GB on every backend here — but a limit on what a browser tab should be asked
# to sit through. A 5 GiB upload over a corporate link is an hour of a page staying open,
# and past this point the answer is to put the object in the bucket out of band (console,
# CLI, azcopy) and let the dashboard list it.
MAX_CHUNKED_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024

# How long a session handle stays usable. Generous because it bounds the WHOLE upload, not
# one part: a 5 GiB file on a slow link is legitimately hours, and an expiry that fires
# mid-upload throws away everything already staged.
HANDLE_TTL_S = 12 * 3600


def part_bytes() -> int:
    return PART_BYTES


def max_chunked_upload_bytes() -> int:
    return MAX_CHUNKED_UPLOAD_BYTES


def part_count(total_bytes: int) -> int:
    return max(1, math.ceil(total_bytes / PART_BYTES))


def expected_part_bytes(total_bytes: int, part_number: int) -> int:
    """Exactly how long part `part_number` must be. See the module docstring."""
    parts = part_count(total_bytes)
    if part_number < parts:
        return PART_BYTES
    return total_bytes - (parts - 1) * PART_BYTES


# ── Session handle ───────────────────────────────────────────────────────────

_HANDLE_MARKER = "vmdash-chunked-upload-1"


def _encode_handle(payload: dict) -> str:
    from . import config_service
    payload = dict(payload, marker=_HANDLE_MARKER)
    return config_service.encrypt_value(json.dumps(payload, separators=(",", ":")))


def _decode_handle(token: str, username: str) -> dict:
    """Decrypt and authenticate a session handle.

    Uses ``decrypt_value_strict`` and not ``decrypt_value``: the latter returns its input
    unchanged when it cannot decrypt, which is right for reading a legacy plain-text
    config row and catastrophic here — it would make a hand-written JSON string a valid
    handle, and the handle is what names the destination key.
    """
    from . import config_service
    try:
        raw = config_service.decrypt_value_strict(token or "", ttl=HANDLE_TTL_S)
        payload = json.loads(raw)
    except Exception:
        raise StorageError(
            "This upload session is no longer valid — it expired or was not issued by "
            "this dashboard. Start the upload again.")
    if not isinstance(payload, dict) or payload.get("marker") != _HANDLE_MARKER:
        raise StorageError("This upload session is not a chunked-upload handle.")
    # The handle is not a credential — every part request is authenticated normally — but
    # it does name a destination, so it stays bound to the user it was minted for. Two
    # people uploading at once cannot hand each other a session.
    if payload.get("user") and username and payload["user"] != username:
        raise StorageError("This upload session belongs to a different user.")
    return payload


# ── Per-backend part staging ─────────────────────────────────────────────────
# The four object stores in one block rather than beside their own backend sections, so
# the parity is readable in one screen. Each one is four operations with the same
# signature; a backend that grows a fifth shape has gone wrong.
#
#   begin(key, total)                      -> upload_ref (str; "" when the store needs none)
#   stage(key, ref, part, data, offset, total) -> part_ref (str; "" when unused at commit)
#   commit(key, ref, parts, total)         -> None      (parts: [(number, part_ref)])
#   abort(key, ref)                        -> None

def _s3_begin(key: str, total: int) -> str:
    client = storage_service._s3_client()
    resp = client.create_multipart_upload(
        Bucket=storage_service._cfg("storage_s3_bucket"), Key=key)
    return resp["UploadId"]


def _s3_stage(key: str, ref: str, part: int, data: bytes, offset: int, total: int) -> str:
    client = storage_service._s3_client()
    resp = client.upload_part(
        Bucket=storage_service._cfg("storage_s3_bucket"), Key=key,
        UploadId=ref, PartNumber=part, Body=data)
    return resp["ETag"]


def _s3_commit(key: str, ref: str, parts: list, total: int) -> None:
    client = storage_service._s3_client()
    client.complete_multipart_upload(
        Bucket=storage_service._cfg("storage_s3_bucket"), Key=key, UploadId=ref,
        MultipartUpload={"Parts": [
            {"PartNumber": n, "ETag": etag} for n, etag in sorted(parts)
        ]},
    )


def _s3_abort(key: str, ref: str) -> None:
    client = storage_service._s3_client()
    client.abort_multipart_upload(
        Bucket=storage_service._cfg("storage_s3_bucket"), Key=key, UploadId=ref)


def _azure_block_id(part: int) -> str:
    """Azure block ids must all be the same length and base64-encoded. Derived from the
    part number rather than round-tripped through the client, so the commit list cannot be
    steered by what the browser sends back — see `_azure_commit`."""
    return base64.b64encode(f"{part:08d}".encode()).decode()


def _azure_begin(key: str, total: int) -> str:
    # Azure needs no upload id: a staged block is addressed by (blob, block id), and an
    # uncommitted block list is garbage-collected after a week. So there is nothing to
    # open — and nothing to leak if the browser walks away.
    return ""


def _azure_stage(key: str, ref: str, part: int, data: bytes, offset: int, total: int) -> str:
    svc = storage_service._azure_blob_client()
    blob_client = svc.get_blob_client(
        container=storage_service._azure_container(), blob=key)
    blob_client.stage_block(block_id=_azure_block_id(part), data=data, length=len(data))
    return _azure_block_id(part)


def _azure_commit(key: str, ref: str, parts: list, total: int) -> None:
    from azure.storage.blob import BlobBlock
    svc = storage_service._azure_blob_client()
    blob_client = svc.get_blob_client(
        container=storage_service._azure_container(), blob=key)
    # Recomputed from the part NUMBERS, ignoring the ids the client echoed back. The
    # client's list decides the length and the order; it does not get to name a block.
    ordered = [_azure_block_id(n) for n, _ in sorted(parts)]
    blob_client.commit_block_list([BlobBlock(bid) for bid in ordered])


def _azure_abort(key: str, ref: str) -> None:
    # Nothing to call. Uncommitted blocks belong to no blob and expire on their own; there
    # is no blob to delete, and deleting the key would delete a PREVIOUS good upload of
    # the same asset.
    return None


def _gcs_begin(key: str, total: int) -> str:
    client = storage_service._gcs_client()
    bucket = client.bucket(storage_service._cfg("storage_gcs_bucket"))
    blob = bucket.blob(key)
    return blob.create_resumable_upload_session(
        content_type="application/octet-stream", size=total)


def _gcs_stage(key: str, ref: str, part: int, data: bytes, offset: int, total: int) -> str:
    import requests
    end = offset + len(data) - 1
    resp = requests.put(
        ref, data=data,
        headers={
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {offset}-{end}/{total}",
        },
        # 308 Resume Incomplete is the SUCCESS answer for every part but the last, and
        # `requests` treats 308 as a redirect to follow. Following it re-PUTs the part to
        # the Location header with no Content-Range.
        allow_redirects=False,
        timeout=300,
    )
    if resp.status_code not in (200, 201, 308):
        raise StorageError(
            f"GCS refused part {part} ({resp.status_code}): {resp.text[:200]}")
    return ""


def _gcs_commit(key: str, ref: str, parts: list, total: int) -> None:
    # A resumable upload finalises itself when the last range lands, so there is no commit
    # call to make — only the check that it did. Without this an upload that lost its
    # final part would report success and leave nothing in the bucket.
    client = storage_service._gcs_client()
    bucket = client.bucket(storage_service._cfg("storage_gcs_bucket"))
    blob = bucket.get_blob(key)
    if blob is None:
        raise StorageError(
            "GCS did not finalise the object — the last part never arrived. "
            "Start the upload again.")
    if blob.size is not None and int(blob.size) != total:
        raise StorageError(
            f"GCS finalised {blob.size} bytes of an expected {total}. Start the upload "
            f"again.")


def _gcs_abort(key: str, ref: str) -> None:
    import requests
    try:
        requests.delete(ref, timeout=60)
    except Exception as e:                                # pragma: no cover - best effort
        logger.warning("Failed to cancel GCS resumable session for %s: %s", key, e)


def _oci_begin(key: str, total: int) -> str:
    import oci
    client = storage_service._oci_os_client()
    resp = client.create_multipart_upload(
        storage_service._oci_namespace(), storage_service._oci_bucket(),
        oci.object_storage.models.CreateMultipartUploadDetails(object=key),
    )
    return resp.data.upload_id


def _oci_stage(key: str, ref: str, part: int, data: bytes, offset: int, total: int) -> str:
    client = storage_service._oci_os_client()
    resp = client.upload_part(
        storage_service._oci_namespace(), storage_service._oci_bucket(), key, ref,
        part, data)
    return resp.headers.get("etag", "")


def _oci_commit(key: str, ref: str, parts: list, total: int) -> None:
    import oci
    client = storage_service._oci_os_client()
    client.commit_multipart_upload(
        storage_service._oci_namespace(), storage_service._oci_bucket(), key, ref,
        oci.object_storage.models.CommitMultipartUploadDetails(
            parts_to_commit=[
                oci.object_storage.models.CommitMultipartUploadPartDetails(
                    part_num=n, etag=etag)
                for n, etag in sorted(parts)
            ],
        ),
    )


def _oci_abort(key: str, ref: str) -> None:
    client = storage_service._oci_os_client()
    client.abort_multipart_upload(
        storage_service._oci_namespace(), storage_service._oci_bucket(), key, ref)


_CHUNKED_OPS = {
    "s3":         {"begin": _s3_begin,    "stage": _s3_stage,    "commit": _s3_commit,    "abort": _s3_abort},
    "azure_blob": {"begin": _azure_begin, "stage": _azure_stage, "commit": _azure_commit, "abort": _azure_abort},
    "gcs":        {"begin": _gcs_begin,   "stage": _gcs_stage,   "commit": _gcs_commit,   "abort": _gcs_abort},
    "oci_object_storage": {"begin": _oci_begin, "stage": _oci_stage, "commit": _oci_commit, "abort": _oci_abort},
}


def supports_chunked_upload(backend: str) -> bool:
    """Whether `backend` can accept an asset one part at a time.

    Answered from `_CHUNKED_OPS`, the same table the operations dispatch through, so a
    caller that asks first and a caller that just tries cannot disagree — the rule
    `storage_service.can_presign` follows for the same reason.
    """
    return backend in _CHUNKED_OPS


def chunked_upload_reason(backend: str) -> str:
    """Why this backend does or does not have the chunked lane, phrased for an operator."""
    if backend == "agent_local":
        return ("a share reached through an agent moves files inside a signed job "
                "envelope, and chunking does not change that ceiling — use a cloud "
                "backend for anything larger")
    if backend == "local":
        return ("a filesystem path has no part-staging protocol, and this backend is "
                "already limited to the container's ephemeral disk — use a cloud backend "
                "for anything larger")
    return ("parts are staged straight into the object store, so neither the browser nor "
            "the dashboard holds more than one part")


# ── Public API ───────────────────────────────────────────────────────────────

async def begin_upload(backend: str, name: str, total_bytes: int,
                       username: str = "") -> dict:
    """Open a chunked upload and return the handle plus the layout the client must use."""
    storage_service._validate_backend(backend)
    if not supports_chunked_upload(backend):
        raise StorageError(
            f"Backend '{backend}' cannot accept a chunked upload: "
            f"{chunked_upload_reason(backend)}.")
    # The extension check happens HERE and not only at commit. Discovering that a file
    # type is unsupported after sending 311 MB of it is the same mistake as checking a
    # size limit after building the base64.
    storage_service._check_upload(name)
    if total_bytes <= 0:
        raise StorageError("A chunked upload needs the file's total size up front.")
    if total_bytes > MAX_CHUNKED_UPLOAD_BYTES:
        raise storage_service.UploadTooLarge(
            f"'{name}' is {storage_service._human_bytes(total_bytes)}, over the "
            f"{storage_service._human_bytes(MAX_CHUNKED_UPLOAD_BYTES)} limit for a "
            f"browser upload — put an object this large in the bucket out of band (cloud "
            f"console, CLI or azcopy) and the dashboard will list it.")
    key = storage_service.asset_key(backend, name)
    ref = await storage_service._to_thread(_CHUNKED_OPS[backend]["begin"], key, total_bytes)
    parts = part_count(total_bytes)
    handle = _encode_handle({
        "backend": backend,
        "name":    name,
        "key":     key,
        "ref":     ref or "",
        "total":   total_bytes,
        "parts":   parts,
        "user":    username or "",
    })
    logger.info("Chunked upload opened: %s -> %s/%s in %d part(s)",
                name, backend, key, parts)
    return {
        "handle":      handle,
        "backend":     backend,
        "filename":    name,
        "total_bytes": total_bytes,
        "part_bytes":  PART_BYTES,
        "parts":       parts,
    }


async def stage_part(handle: str, part_number: int, data: bytes,
                     username: str = "") -> dict:
    """Stage one part. `data` must be exactly the length the layout implies."""
    session = _decode_handle(handle, username)
    total = int(session["total"])
    parts = int(session["parts"])
    if not (1 <= part_number <= parts):
        raise StorageError(
            f"Part {part_number} is outside this upload's 1..{parts}.")
    expected = expected_part_bytes(total, part_number)
    if len(data) != expected:
        raise StorageError(
            f"Part {part_number} is {len(data)} bytes, expected {expected}. The part size "
            f"is fixed by the server at {PART_BYTES} bytes; slice the file to match.")
    offset = (part_number - 1) * PART_BYTES
    backend = session["backend"]
    ref = await storage_service._to_thread(
        _CHUNKED_OPS[backend]["stage"], session["key"], session["ref"],
        part_number, data, offset, total)
    return {"part": part_number, "ref": ref or "", "bytes": len(data)}


async def commit_upload(handle: str, parts: list, username: str = "") -> dict:
    """Commit the staged parts into one object.

    `parts` is the list of ``{"part": n, "ref": s}`` the client collected from
    :func:`stage_part`. It must name every part exactly once — a commit that silently
    dropped one would produce a truncated installer that runs and fails on the target.
    """
    session = _decode_handle(handle, username)
    expected = int(session["parts"])
    pairs: list = []
    seen: set = set()
    for item in parts or []:
        try:
            n = int(item["part"])
        except (KeyError, TypeError, ValueError):
            raise StorageError("Each part must carry its part number.")
        if n in seen:
            raise StorageError(f"Part {n} was listed twice.")
        seen.add(n)
        pairs.append((n, str(item.get("ref") or "")))
    missing = sorted(set(range(1, expected + 1)) - seen)
    if missing:
        raise StorageError(
            f"{len(missing)} of {expected} parts never arrived (first missing: "
            f"{missing[0]}). Nothing was written — start the upload again.")
    if len(pairs) != expected:
        raise StorageError(
            f"Got {len(pairs)} parts for an upload of {expected}.")
    backend = session["backend"]
    await storage_service._to_thread(
        _CHUNKED_OPS[backend]["commit"], session["key"], session["ref"],
        pairs, int(session["total"]))
    logger.info("Chunked upload committed: %s -> %s/%s (%d parts, %d bytes)",
                session["name"], backend, session["key"], expected,
                int(session["total"]))
    return {
        "filename": session["name"],
        "backend":  backend,
        "size":     int(session["total"]),
        "parts":    expected,
    }


async def abort_upload(handle: str, username: str = "") -> dict:
    """Discard the staged parts. Best-effort: a store that cannot be told still expires
    them on its own, which is why nothing here treats a failure as fatal."""
    session = _decode_handle(handle, username)
    backend = session["backend"]
    try:
        await storage_service._to_thread(
            _CHUNKED_OPS[backend]["abort"], session["key"], session["ref"])
    except Exception as e:
        logger.warning("Failed to abort chunked upload of %s on %s: %s",
                       session.get("name"), backend, e)
        return {"aborted": False, "detail": str(e)}
    return {"aborted": True}


def session_filename(handle: str, username: str = "") -> Optional[str]:
    """The asset name a handle names, for a caller that needs to log or scan it without
    re-deriving the whole session."""
    try:
        return _decode_handle(handle, username).get("name")
    except StorageError:
        return None
