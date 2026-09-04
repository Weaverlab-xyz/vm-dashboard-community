# Storage Management

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are enabling a feature that needs a storage backend, which several of them do.

This document explains how the dashboard stores playbooks, scripts, and
other large assets that don't fit in the encrypted credentials database
— and how to choose, configure, and migrate between cloud object stores.

The companion to [Secrets Management](secrets-management.md): secrets are
small, sensitive, and live in a per-key encrypted store. Storage holds
bigger, mostly-non-sensitive payloads (playbooks, shell scripts, package
files, image artefacts) that need to be readable by Ansible runners
across hosts and by cloud VM-import APIs. For the philosophy and
best-practice side of running playbooks against your fleet, see
[Config Management](config-management.md). For the IaC layer that
stood the targets up in the first place, see
[Infrastructure as Code](infrastructure-as-code.md). For the image
build → promote lifecycle that produces the binaries the IaC layer
deploys, see [Image Management](image-management.md).

---

## Philosophy

The dashboard talks to one **active** storage backend at a time. You pick
from the list below, configure as many as you like, and choose one to be
active. Backends not currently active stay reachable through the migration
UI so you can copy assets between them without downtime.

| Backend | Underlying service | Best for |
|---|---|---|
| **AWS S3** | S3 bucket + key prefix | Teams already on AWS; cheapest at scale |
| **Azure Blob Storage** | Storage account + container + blob prefix | Teams on Azure; integrates with Azure SP creds |
| **Google Cloud Storage** | GCS bucket + object prefix | Teams on GCP; same SA creds as Compute Engine |
| **Local Filesystem / UNC** | Filesystem path inside the dashboard container, or a corporate `\\server\share` UNC accessed via SMB | On-prem hypervisor targets when a corporate file share is the source of truth — see [the constraint below](#constraint-local-backend-only-works-with-the-local-ansible-runner) |
| **Remote Filesystem / UNC (via agent)** | The same kind of target, reached by a [remote agent](remote-agents.md) instead of by the dashboard container | A **cloud-hosted** dashboard — or a [POV instance](profiles/pov/README.md) — that needs an on-prem share it has no network route to. See [below](#remote-filesystem--unc-via-agent) |

They are interchangeable from the dashboard's perspective. Switching
backends does **not** move data; the Migrate panel does that explicitly,
and only deletes from the source if you ask it to (today: never — see
"Migration semantics" below).

The active backend holds two kinds of data: **user assets** (playbooks,
packages, Packer manifests, the image-registry hub) and **Terraform
remote state** for cloud deploys (see [What counts as an asset](#what-counts-as-an-asset)).
That second kind is why switching backends is guarded — if live deployments
have state on the current backend, the dashboard won't let you flip the
active backend out from under it without migrating that state too (see
"Migration semantics").

---

## Why storage is its own page

Storage was originally configured inside the Ansible feature panel. It
lives on its own page because:

- **Future features may use it.** Image manifests, log archives, and
  capture artifacts all fit the same backend abstraction. Pinning the
  config to "Ansible" would force every new feature to either re-implement
  storage or pretend to be Ansible.
- **It's a deployment-level concern.** Picking S3 vs. GCS is an
  organisation-policy decision, not a per-feature one. Surfacing it
  alongside `/secrets` (which is the same kind of decision for
  credentials) keeps the mental model clean.
- **The Ansible feature flag depends on it.** The Settings → Integrations
  toggle for Ansible is greyed out until storage is configured and active,
  with a link to `/storage` in the tooltip.

---

## What counts as an asset

| Type | Extensions | Used by |
|---|---|---|
| Ansible playbook | `.yml`, `.yaml` | Ansible runner — executed as-is |
| Shell script | `.sh` | Ansible runner — auto-wrapped: `ansible.builtin.script` |
| PowerShell script | `.ps1` | Ansible runner — auto-wrapped: `ansible.windows.win_script` (Windows targets only; the host's inventory must set `ansible_connection=winrm`) |
| RPM package | `.rpm` | Ansible runner — auto-wrapped: copy + dnf install |
| DEB package | `.deb` | Ansible runner — auto-wrapped: copy + apt install |

Files outside this set are rejected at upload. Assets are stored under a
configurable key prefix (default `config-mgmt/`), so multiple deployments
can share a bucket if the prefix differs.

> **The table above governs the ASSET UPLOAD path only** — files a user uploads
> for the Ansible runner to execute. It is not a restriction on what the dashboard
> itself can store. In particular, **Cloud Functions are unaffected by it**: their
> Python handlers are not uploaded at all. They ship inside the dashboard image,
> the packager builds the deployable zip in memory, and it goes to a dedicated
> per-cloud bucket (`function_package_s3_bucket` / `function_package_gcs_bucket` /
> a container on `storage_azure_account`) rather than to the active storage
> backend. See [Cloud Functions → package stores](integrations/cloud-functions.md).

> **Beyond user-uploaded assets, the active backend also holds Terraform
> remote state.** Cloud VM, cloud-database, and Kubernetes-cluster deploys
> write their state to the *same* active backend (keyed per job under
> `terraform-state/`, with backend-native locking), so the backend you pick
> here is load-bearing for infrastructure teardown, not just playbooks. The
> BeyondTrust PRA tunnel state is the one exception — it's scrubbed of
> credentials and kept in the database instead. See
> [Infrastructure as Code → State](infrastructure-as-code.md#state-the-thing-that-makes-iac-work).

## Uploading

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

### Two lanes, and why a 311 MB installer needs the second

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
[the agent-brokered backend's ~190 KB ceiling](#remote-filesystem--unc-via-agent).

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

---

## Configuring storage

Open `/storage` (admin only). The page has four sections:

1. **Backend** — pick the active backend with a radio button. Each
   backend's configuration card shows below; fill in the fields for the
   one(s) you want to use. A backend appears as **configured** when its
   primary identifier (bucket / storage-account / bucket name) is set,
   regardless of whether it's the active one.
2. **Image-registry hub** — pick the backend that holds the canonical
   VHD/raw artefact for every registered image. Leave on "Same as
   active backend" for single-backend installs; only change it if you
   want the image hub on a different cloud than your day-to-day asset
   uploads. See [Image-registry hub](#image-registry-hub) below.
3. **Stored assets** — once a backend is active, the list shows what's
   in it. Use Config Management's upload form (`/config-mgmt`) to add
   playbooks; this page is read-mostly except for delete.
4. **Migrate** — covered below.

### Required cloud credentials

Each backend reuses cloud credentials configured elsewhere in the
dashboard:

| Backend | Reads creds from |
|---|---|
| **S3** | `aws_access_key_id` / `aws_secret_access_key` (Setup → AWS) |
| **Azure Blob** | Azure service principal (Setup → Azure) |
| **GCS** | GCP service account JSON (Setup → GCP) |

If the cloud you want to use for storage isn't already configured for VM
deploys, set those creds first in `/setup` or the matching Settings panel.

### S3

| Field | Notes |
|---|---|
| **Bucket** | Required. The bucket must already exist (the dashboard does not auto-create). |
| **Region** | Optional. Defaults to your AWS region from Setup → AWS. |
| **Key prefix** | Optional. Defaults to `config-mgmt`. Useful for sharing one bucket across deployments. |

### Azure Blob Storage

| Field | Notes |
|---|---|
| **Storage account** | Required. The account must already exist. |
| **Container** | Defaults to `playbooks`. Created on first upload if missing. |
| **Blob prefix** | Defaults to `config-mgmt`. |

### Google Cloud Storage

| Field | Notes |
|---|---|
| **Bucket** | Required. The bucket must already exist. |
| **Object prefix** | Defaults to `config-mgmt`. |

### Local Filesystem / UNC

| Field | Notes |
|---|---|
| **Path** | Required. Either a path inside the dashboard container (typically a bind-mounted host directory like `/srv/playbooks`) or a UNC `\\server\share[\subpath]`. UNC paths use the SMB protocol via the `smbprotocol` Python library — no host-side mount or `cifs-utils` required. |
| **Username / Password / Domain** | Optional, used only for UNC. Username may be `bare` or `DOMAIN\user`; the Domain field is convenience for the latter. Password is encrypted at rest in the dashboard's config DB. |

#### Constraint: local backend only works with the local Ansible runner

The Local backend is only selectable when **Settings → Ansible → Runner**
is set to **Local Docker (default)**. Cloud Ansible runners (AWS ECS,
Azure ACI, GCP Cloud Run) live in cloud-only VPCs/VNets and have no
network path back to a corporate file server. If you tried to use a UNC
path from a Fargate task, the SMB connection would fail at TCP 445 and
the run would error before the playbook ran.

The dashboard enforces this in two places:

- **Frontend**: the radio button for the Local backend is disabled with
  an inline note when the runner isn't `local`.
- **Backend**: `PATCH /api/storage/config` returns a 400 if you try to
  set `storage_active_backend=local` while `ansible_runner != local`.

Concrete fit: if your contributors test on-prem hypervisor targets
(Proxmox VE, vSphere/ESXi, Nutanix AHV, XCP-ng, Hyper-V) with playbooks
hosted on a corporate share, the Local backend is the right choice.

**If the dashboard itself is in a cloud, this backend is the wrong one** —
that container has no route to your file server either. Use the
agent-brokered backend below instead, which has no runner constraint at all.

### Remote Filesystem / UNC (via agent)

The same kind of target as the Local backend, with the constraint removed.
The dashboard does not open the SMB socket; a [remote agent](remote-agents.md)
already inside your network does, and reports back over the same
outbound-polling channel it uses for hypervisor and Config-Management work.

That inverts who has to be where. A dashboard on Azure Container Apps, or a
[POV instance](profiles/pov/README.md) with no cloud provider at all, can use a
corporate share as its storage backend — and because the dashboard fetches
the bytes and hands them to whichever Ansible runner is selected, a **cloud
runner is fine**. There is no `ansible_runner=local` requirement.

| Field | Notes |
|---|---|
| **Agent** | Required. Any enrolled agent, granted the `agent_storage` job type on the Agents page, running **2.5.0 or later**. |
| **Share name** | Required. Must match a `name:` in that agent's `shares.yaml` **and** a `name:` under `storage.shares:` in its `policy.yaml`. |
| **Subpath** | Optional relative directory inside the share. Relative only — an absolute path is refused, not quietly made relative. |

#### What the dashboard does not hold

**No path, no username, no password.** All three live in the agent's own
`shares.yaml`, next to its `connections.yaml`, and the dashboard has no API
that can read or write that file. What it stores is one half of a join — the
same arrangement hypervisor connections already use, where the dashboard
holds a connection *name* and the credential never leaves the customer's
host.

That is also why a compromised dashboard cannot reach an arbitrary share.
There is no path field anywhere in the job protocol, so `\\some-other-server\c$`
is not refused, it is unsayable. A job names a share the operator already
wrote down, plus a bare filename; both the dashboard and the agent
independently refuse anything containing a separator or a leading dot.

#### Three grants, all required

| Where | What |
|---|---|
| Agents page | the `agent_storage` job type granted to this agent |
| `policy.yaml` → `job_types:` | `agent_storage` |
| `policy.yaml` → `storage:` | `enabled: true` and the share listed by name, plus `write: true` if the dashboard should be able to upload or delete |

Read is implied by naming the share. **Write is off by default** — the
common case is a share somebody else populates and the dashboard only
consumes. Without `write: true`, uploads and deletes are refused by name.
See [`examples/remote-agent/policy.example.yaml`](../examples/remote-agent/policy.example.yaml)
and [`shares.example.yaml`](../examples/remote-agent/shares.example.yaml).

#### Two things that behave differently from a cloud backend

**It is slower, and listings are cached.** Every operation is a real agent
job, and agents poll every five seconds, so a round trip is 5–15 seconds.
Asset *listings* are therefore cached for two minutes, keyed on agent +
share + subpath — without that, the asset pickers on six other pages would
each sit on a round trip. Uploads and deletes invalidate the cache
immediately, and **Test connection** deliberately bypasses it, so it is also
how you force a refresh after somebody changes the share outside the
dashboard.

**There is a ~190 KB ceiling per file.** Files travel inside the signed job
envelope, which is capped at 256 KB before base64 takes its third.
Playbooks, `.sh`, `.ps1` and inventories are far below it. Packages
(`.rpm`, `.deb`, `.exe`, `.msi`) are not, and an oversize upload is refused
at the dashboard with a message naming the limit rather than failing halfway
through a transfer. Put those on a cloud backend.

**Chunking does not help here, and that is why it is not offered.** The
ceiling is the envelope, not the request — an agent moves a file inside one
signed job, so splitting the browser's upload into parts would still leave
the agent hop unable to carry the result. The
[chunked upload lane](#two-lanes-and-why-a-311-mb-installer-needs-the-second)
exists only for the object stores.

Like the Local backend, it cannot be the [image-registry hub](#image-registry-hub)
— promote runners need an HTTPS URL, and a multi-GB VHD does not fit an
envelope — and it holds no Terraform state (see below).

After filling in fields, click **Test connection** to probe the backend —
it lists the bucket/container as a quick reachability check. Save with
**Save configuration**; activation flips the moment the save succeeds.

---

## Image-registry hub

The hub backend is the single storage backend that holds the canonical
VHD/raw artefact for every registered image, regardless of which cloud
built it. It's the source the cross-cloud promote flow reads from when
it kicks off a per-target runner — see
[Image Management](image-management.md) for the full lifecycle and
[`runners/promote/README.md`](../runners/promote/README.md) for the
runner internals.

**Configuration.** On `/storage`, the **Image-registry hub** picker
has four options:

- **Same as active backend** (default) — `storage_hub_backend` is
  unset; `storage_service.hub_backend()` resolves to whatever
  `active_backend()` returns. Single-backend installs need nothing
  more.
- **S3 / Azure Blob / GCS** — pin the hub to that backend explicitly.
  Useful when your day-to-day asset uploads live in one cloud but you
  want the image hub in another (e.g. day-to-day in S3, image hub in
  GCS for cost reasons).

Neither filesystem backend can be the hub — `local` or `agent_local`.
The promote runners need an HTTPS-reachable URL for the source
artefact, which a filesystem doesn't offer; and a multi-GB VHD would
not fit an agent job envelope in any case. The page's picker lists
neither; posting either via the API returns a 400 with a pointer to
this section.

**What the hub does.** After every successful Packer build the
dashboard exports a portable VHD via the cloud's native export API
into same-cloud storage. If that same-cloud storage *is* the hub (e.g.
build cloud = AWS, hub = S3), no extra hop. If it isn't (build cloud
= AWS, hub = Azure Blob), the dashboard runs
`storage_service.copy(build_backend, build_key, hub, hub_key)` to move
the VHD into the hub and deletes the build-side staging copy. Into an
Azure Blob hub this copy is fully server-side (the staging object is
presigned and Azure pulls each block itself — no bytes through the
dashboard container); into an S3/GCS/OCI hub from a *different* build
cloud it stages through the container's ephemeral disk, which
multi-GB VHDs can overflow — pick an Azure Blob hub or the build
cloud's own backend if you build cross-cloud.

The same export-and-land-on-hub path runs when an operator clicks
**Export VHD** on a cloud-native image in the per-cloud Images tab
(AWS Private AMIs / Azure Managed Images / GCP Custom Images). This
is the recovery path for builds whose auto-export was skipped (e.g.
the storage prerequisite was missing at build time). See
[Image Management → Manual export](image-management.md#manual-export-recovery-path).

**What it doesn't do.** The hub is not where the promote runner
*uploads* to. Each target cloud has its own staging container the
runner writes into (`promote_runner_aws_staging_bucket`,
`promote_runner_azure_staging_container`,
`promote_runner_gcp_staging_bucket`) so the cloud's import API reads
from local storage. The hub stays the read-only source-of-truth.

**Promote-runner config.** The `promote_runner_*` keys (image override,
ECS/ACI/Cloud Run/Container Instances plumbing, target-side staging, IAM
role ARNs) are edited in **Settings → Remote Worker → Image-promote
runner**, one sub-card per target cloud. `PATCH /api/storage/config`
also accepts them — the promote runner shares the hub backend's
lifecycle, so the storage API round-trips them for scripted setups. The
[runner README](../runners/promote/README.md) has the full table.

---

## Migration semantics

The Migrate panel copies every asset from a source backend to a target
backend. Operating principles:

- **Source is preserved.** Today the dashboard never deletes from source
  during migration. Verify the target is healthy, then delete from the
  source manually (use each backend's native console). This is intentional
  — first cutovers are when things go wrong, and rolling back is much
  cheaper if the data is still where it started.
- **Skip-by-default for collisions.** Files already present in the target
  are skipped. Tick **Overwrite existing** to replace them.
- **Active backend doesn't change automatically.** After the migration
  completes successfully, switch the active backend in the Backend
  section above and click **Save configuration** — that's a separate,
  explicit step. The dashboard reads from whichever backend is active at
  the moment a request lands; there's no warmup or cache.
- **Per-asset errors don't abort the run.** A file that fails to copy
  appears in the Failed list with its error; the rest of the migration
  continues. You can re-run with the same source/target to retry just the
  failed ones (already-copied files are skipped automatically).
- **Terraform state migrates separately, and switching is guarded.** The
  asset Migrate panel copies *assets*. **Terraform remote state** (under
  the `terraform-state/` prefix) is handled on the active-backend switch
  itself: if live deployments still have state on the current backend, the
  dashboard **blocks the switch** rather than stranding it, and asks you to
  migrate the state. Confirming copies every `terraform-state/*` object to
  the new backend (`storage_service.migrate_terraform_state`), then flips
  the active backend. So the safe cutover is: migrate assets → switch the
  active backend and confirm the state migration → verify → delete from the
  old backend by hand. Losing that state orphans the resources it tracks
  (see [Infrastructure as Code → State](infrastructure-as-code.md#state-the-thing-that-makes-iac-work)).
- **Neither filesystem backend holds Terraform state at all.** Terraform ships
  no state backend for a filesystem, so with `local` *or* `agent_local` active,
  state stays in the container's deploy directory — which on Azure Container
  Apps has no volume and does not survive a recreate. A state file never crosses
  to an agent: it is not an asset, it is not extension-filtered, and it is
  regularly larger than a job envelope can carry. The `/storage` page shows an
  amber warning whenever one of these is active. If you provision cloud
  resources, put the active backend on S3, Azure Blob or GCS and use a
  filesystem backend for assets only.

The migrate result block on the page summarises three lists: Copied,
Skipped, Failed. Save the page or screenshot before navigating away if
you need a record.

---

## API reference

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /api/storage/backends` | logged-in user | Backend metadata + active state. Used by /storage and the Ansible flag prereq gate. |
| `GET /api/storage/config` | admin | All per-backend config values. |
| `PATCH /api/storage/config` | admin | Update fields + active selection. Validates active is configured before flipping. |
| `POST /api/storage/test` | admin | Reachability probe (lists assets in the named backend). |
| `GET /api/storage/list` | logged-in user | Assets in the active backend. |
| `GET /api/storage/list/{backend}` | admin | Assets in a specific backend (used by the migrate UI's source picker). |
| `POST /api/storage/upload` | logged-in user | Upload `{filename, content_b64}` to the active backend. Inline lane; 413 above 64 MB. |
| `POST /api/storage/upload/begin` | logged-in user | Open a chunked upload: `{filename, size}` → `{handle, part_bytes, parts}`. Object stores only. |
| `PUT /api/storage/upload/part/{n}` | logged-in user | Stage one raw part. Body is the bytes; handle in `X-Upload-Handle`. |
| `POST /api/storage/upload/commit` | logged-in user | Commit `{handle, parts}` into one object. |
| `POST /api/storage/upload/abort` | logged-in user | Discard `{handle}`'s staged parts. Never fails loudly. |
| `POST /api/storage/migrate` | admin | Copy `{source, target, overwrite}` → returns `{copied, skipped, failed}`. |
| `DELETE /api/storage/asset/{name}` | admin | Remove a single asset from the active backend. |

Storage credentials live in the encrypted DB exactly the same way as
other config values; nothing on this page reads or writes
`.jwt_secret_key`.

---

## Cost

Idle cost is roughly the cost of the underlying object store, which is
near-zero for the playbook/script asset profile (KB-MB files, low PUT/GET
volume).

| Backend | Storage class | Typical monthly cost for ~100 MB of assets |
|---|---|---|
| S3 | Standard | ~$0.0023 |
| Azure Blob | Hot LRS | ~$0.0018 |
| GCS | Standard | ~$0.0020 |

Network egress during runs (the runner downloads the playbook bytes once
per job) is the same per-byte rate as any cross-AZ traffic in the
respective cloud.

---

## Backup and lifecycle

The dashboard does not manage backup or lifecycle for storage backends.
Use each provider's native primitives:

- **S3** — versioning, lifecycle policies (e.g. Glacier transition).
- **Azure Blob** — soft delete, snapshot, lifecycle management.
- **GCS** — object versioning, lifecycle rules.

Recommended baseline for any production-style deployment: enable
versioning so an accidental overwrite or migration can be reverted. This
matters doubly for the `terraform-state/` prefix — versioning there is your
recovery path if a state object is corrupted or deleted, since losing it
orphans the resources that state tracks.

---

## Troubleshooting

**"No active storage backend" error on the Config Management page.**
You have a backend configured but didn't activate it. Open `/storage`,
pick the radio button for the backend you intended, and Save.

**Test connection fails with a 403 / AccessDenied.**
The cloud credentials configured in Setup → AWS/Azure/GCP don't include
read+write access to the bucket. For S3 this typically means
`s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on
the bucket and `arn:aws:s3:::bucket/prefix/*`. For Azure Blob, the
service principal needs **Storage Blob Data Contributor** on the
storage account or the specific container.

**Migrate finishes, but the new uploads still go to the old backend.**
Switching the active backend is a separate step. After migration, change
the radio button in **Backend** and click **Save configuration**.

**An asset shows up in `GET /api/storage/list/{backend}` but not in the
active list view.**
Different backends maintain different prefixes. The active backend's
prefix may be excluding it. Either reuse the same prefix everywhere, or
include the prefix when uploading.

**The Ansible feature flag toggle is greyed out.**
Working as intended — the dashboard requires storage to be configured
and active before Ansible can be enabled. Open `/storage`, pick a
backend, save, then come back to Settings → Integrations.
