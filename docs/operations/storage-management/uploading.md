# Uploading assets

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are uploading playbooks, scripts, packages or installers, including ones too large for a browser upload.

Part of [Storage Management](../storage-management.md).

Two equivalent paths to put an asset in storage:

1. **`/storage` page** — drag-and-drop or pick a file in the Upload card,
   click Upload. Goes straight to the active backend. Available to any
   logged-in user.
2. **`/config-mgmt` page** — same upload form, plus inline run controls
   for executing the asset against your hypervisor inventory or cloud
   instances. Available to any logged-in user.

Either path uses the same `POST /api/storage/upload` endpoint behind
the scenes. The `/config-mgmt`-side `POST /api/config-mgmt/upload`
endpoint also still works (delegates to the same service).

## Two lanes, and why a 311 MB installer needs the second

`POST /api/storage/upload` carries the file **inline** — base64 inside the
JSON body, held whole by the browser and again by the server. That is right
for the playbooks and scripts this started as, and it caps at **64 MB**: the
cost of the encoding is several times the file's size in browser memory, and
past that the tab dies building the request before a byte is sent. (That is
why the size is checked when you *pick* the file, not when the upload runs —
a server-side limit cannot save a tab that never got to make the call.)

Above that ceiling the `/storage` upload form switches to a **chunked
upload**, on any object-store backend (S3, Azure Blob, GCS, OCI):

| | Inline | Chunked |
|---|---|---|
| Used when | file ≤ 64 MB | file > 64 MB |
| Backends | all six | the four object stores |
| Transport | base64 in one JSON body | 8 MiB raw parts, one request each |
| Peak memory | whole file, several times over | one part, each side |
| Ceiling | 64 MB | 5 GB |
| Progress | none | per-part bar, with Cancel |

**There is nothing to enable.** The form reads the active backend's
capability and ceiling from `GET /api/storage/backends` and picks the lane
itself; a chunked upload is not a mode, a preference, or a setting on the
Storage page. If the active backend is a filesystem one (`local`,
`agent_local`) there is no chunked lane to pick, and a large file is refused
at pick time with the reason — see
[the agent-brokered backend's ~190 KB ceiling](configuring.md#remote-filesystem--unc-via-agent).

How it works, in the order it happens:

1. `POST /api/storage/upload/begin` with the filename and size. The **server**
   fixes the part size and the part count and returns an encrypted session
   handle. The extension and the 5 GB ceiling are checked here — before any
   bytes move.
2. `PUT /api/storage/upload/part/{n}` for each part, the raw slice as the body
   and the handle in an `X-Upload-Handle` header. Each part is staged straight
   into the object store with the store's own primitive (S3/OCI multipart,
   Azure `stage_block`, a GCS resumable range). Parts go **in order**, one at a
   time: the byte offset is derived from the part number, and a part of the
   wrong length is refused at that part rather than at the end.
3. `POST /api/storage/upload/commit` stitches them into one object. Until this
   returns, nothing is listable — a failed upload leaves no half-written asset.
4. `POST /api/storage/upload/abort` on Cancel or on any part failing, so the
   staged parts are not left accruing storage cost.

There is **no upload-session row in the database and no server-side temp
file.** The session state rides in the encrypted handle the browser holds, so
either `gunicorn` worker can serve any part, and an abandoned upload needs no
reaper — the handle expires and each store garbage-collects its own
uncommitted parts. The handle is opaque on purpose: for GCS it contains a
resumable session URI, which is a write capability.

**Two things this does not change.** The advisory secret scan runs on the
first part only (it gives up at the first NUL byte anyway, which is in part 1
of any installer). And `POST /api/config-mgmt/upload` is still inline-only —
Config Management's own form has the 64 MB ceiling, so upload a big installer
on `/storage` and it appears in the Config Management asset picker like
anything else.

Past 5 GB, put the object in the bucket **out of band** (cloud console, CLI,
`azcopy`) and the dashboard will list it: the ceiling is about how long a
browser tab should be asked to stay open, not about what the object store
will take.
