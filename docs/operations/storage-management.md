# Storage Management

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are enabling a feature that needs a storage backend, which several of them do.

Part of [Operations](../operations.md).

This document explains how the dashboard stores playbooks, scripts, and
other large assets that don't fit in the encrypted credentials database
— and how to choose, configure, and migrate between cloud object stores.

The companion to [Secrets Management](../admin/secrets-management.md): secrets are
small, sensitive, and live in a per-key encrypted store. Storage holds
bigger, mostly-non-sensitive payloads (playbooks, shell scripts, package
files, image artefacts) that need to be readable by Ansible runners
across hosts and by cloud VM-import APIs. For the philosophy and
best-practice side of running playbooks against your fleet, see
[Config Management](config-management.md). For the IaC layer that
stood the targets up in the first place, see
[Infrastructure as Code](../clouds/infrastructure-as-code.md). For the image
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
| **Local Filesystem / UNC** | Filesystem path inside the dashboard container, or a corporate `\\server\share` UNC accessed via SMB | On-prem hypervisor targets when a corporate file share is the source of truth — see [the constraint below](storage-management/configuring.md#constraint-local-backend-only-works-with-the-local-ansible-runner) |
| **Remote Filesystem / UNC (via agent)** | The same kind of target, reached by a [remote agent](../admin/remote-agents.md) instead of by the dashboard container | A **cloud-hosted** dashboard — or a [POV instance](../profiles/pov/README.md) — that needs an on-prem share it has no network route to. See [below](storage-management/configuring.md#remote-filesystem--unc-via-agent) |

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
> backend. See [Cloud Functions → package stores](../cloud-services/functions.md).

> **Beyond user-uploaded assets, the active backend also holds Terraform
> remote state.** Cloud VM, cloud-database, and Kubernetes-cluster deploys
> write their state to the *same* active backend (keyed per job under
> `terraform-state/`, with backend-native locking), so the backend you pick
> here is load-bearing for infrastructure teardown, not just playbooks. The
> BeyondTrust PRA tunnel state is the one exception — it's scrubbed of
> credentials and kept in the database instead. See
> [Infrastructure as Code → State](../clouds/infrastructure-as-code.md#state-the-thing-that-makes-iac-work).

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [Uploading assets](storage-management/uploading.md) | you are uploading playbooks, scripts, packages or installers, including ones too large for a browser upload |
| [Configuring a storage backend](storage-management/configuring.md) | you are setting up S3, Azure Blob, Google Cloud Storage, a local or UNC path, or a remote share through an agent |
| [The image-registry hub](storage-management/image-registry.md) | you want container images mirrored into each cloud's registry from one place |

---

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
  (see [Infrastructure as Code → State](../clouds/infrastructure-as-code.md#state-the-thing-that-makes-iac-work)).
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

## Scoping an asset to a workstream

Every asset is **global** by default: anyone who can open the Storage page sees it, and
anyone who can run Config Management can run it. An administrator can limit an asset to
one workstream with the **Workstream** column on the asset table. From then on only that
workstream's members, and administrators, can:

- see it — on the Storage page, in the Config Management asset picker, and in the
  provisioner-script pickers on the cloud deploy forms;
- fetch, run or delete it — `/api/storage/fetch`, `/api/config-mgmt/run` and `/run-bulk`,
  and a Compose deploy from Containers all answer **404**, the same as for an asset that
  doesn't exist;
- replace it — an upload of the same name by anyone else is refused (**409**). Without
  this, the scope would stay on the file and the team's runs would execute the uploader's
  version.

Other things to know:

- **The scope follows the asset.** **Move** carries it to the new backend, **Migrate**
  copies it with each copied asset, and deleting the asset drops it.
- **Workstream post-deploy runs** can only use assets that are global or scoped to that
  workstream (see [Workstream post-deploy runs](../clouds/vms/pra.md#workstream-post-deploy-runs)).
- **A workstream can't be deleted** while assets are scoped to it. Make them global or
  scope them elsewhere first.
- **Jobs already queued still run.** The check is made when a run is requested, not
  when the job starts.

Changing a scope is **administrator-only** and audited (`storage_asset_scope_update`).
The API is `PUT /api/storage/scope` with `{"backend": "s3", "name": "baseline.yml",
"workgroup": "team-a"}`; `"workgroup": null` makes the asset global again. A blank
`backend` means the active backend.

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
| `PUT /api/storage/scope` | admin | Limit `{backend, name}` to `workgroup`, or make it global with `null`. See [Scoping an asset to a workstream](#scoping-an-asset-to-a-workstream). |

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
