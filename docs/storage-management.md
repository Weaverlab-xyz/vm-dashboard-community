# Storage Management

This document explains how the dashboard stores playbooks, scripts, and
other large assets that don't fit in the encrypted credentials database
— and how to choose, configure, and migrate between cloud object stores.

The companion to [Secrets Management](secrets-management.md): secrets are
small, sensitive, and live in a per-key encrypted store. Storage holds
bigger, mostly-non-sensitive payloads (playbooks, shell scripts, package
files) that need to be readable by Ansible runners across hosts.

---

## Philosophy

The dashboard talks to one **active** object-storage backend at a time. You
pick from three providers, configure as many as you like, and choose one
to be active. Backends not currently active stay reachable through the
migration UI so you can copy assets between them without downtime.

| Backend | Underlying service | Best for |
|---|---|---|
| **AWS S3** | S3 bucket + key prefix | Teams already on AWS; cheapest at scale |
| **Azure Blob Storage** | Storage account + container + blob prefix | Teams on Azure; integrates with Azure SP creds |
| **Google Cloud Storage** | GCS bucket + object prefix | Teams on GCP; same SA creds as Compute Engine |
| **Local Filesystem / UNC** | Filesystem path inside the dashboard container, or a corporate `\\server\share` UNC accessed via SMB | On-prem hypervisor targets when a corporate file share is the source of truth — see [the constraint below](#constraint-local-backend-only-works-with-the-local-ansible-runner) |

All four are interchangeable from the dashboard's perspective. Switching
backends does **not** move data; the Migrate panel does that explicitly,
and only deletes from the source if you ask it to (today: never — see
"Migration semantics" below).

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

---

## Configuring storage

Open `/storage` (admin only). The page has three sections:

1. **Backend** — pick the active backend with a radio button. Each
   backend's configuration card shows below; fill in the fields for the
   one(s) you want to use. A backend appears as **configured** when its
   primary identifier (bucket / storage-account / bucket name) is set,
   regardless of whether it's the active one.
2. **Stored assets** — once a backend is active, the list shows what's
   in it. Use Config Management's upload form (`/config-mgmt`) to add
   playbooks; this page is read-mostly except for delete.
3. **Migrate** — covered below.

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

After filling in fields, click **Test connection** to probe the backend —
it lists the bucket/container as a quick reachability check. Save with
**Save configuration**; activation flips the moment the save succeeds.

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
| `POST /api/storage/upload` | logged-in user | Upload `{filename, content_b64}` to the active backend. |
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
versioning so an accidental overwrite or migration can be reverted.

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
