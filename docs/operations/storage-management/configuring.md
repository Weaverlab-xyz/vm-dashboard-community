# Configuring a storage backend

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are setting up S3, Azure Blob, Google Cloud Storage, a local or UNC path, or a remote share through an agent.

Part of [Storage Management](../storage-management.md).

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
   uploads. See [Image-registry hub](image-registry.md) below.
3. **Stored assets** — once a backend is active, the list shows what's
   in it. Use Config Management's upload form (`/config-mgmt`) to add
   playbooks; this page is read-mostly except for delete.
4. **Migrate** — covered below.

## Required cloud credentials

Each backend reuses cloud credentials configured elsewhere in the
dashboard:

| Backend | Reads creds from |
|---|---|
| **S3** | `aws_access_key_id` / `aws_secret_access_key` (Setup → AWS) |
| **Azure Blob** | Azure service principal (Setup → Azure) |
| **GCS** | GCP service account JSON (Setup → GCP) |

If the cloud you want to use for storage isn't already configured for VM
deploys, set those creds first in `/setup` or the matching Settings panel.

## S3

| Field | Notes |
|---|---|
| **Bucket** | Required. The bucket must already exist (the dashboard does not auto-create). |
| **Region** | Optional. Defaults to your AWS region from Setup → AWS. |
| **Key prefix** | Optional. Defaults to `config-mgmt`. Useful for sharing one bucket across deployments. |

## Azure Blob Storage

| Field | Notes |
|---|---|
| **Storage account** | Required. The account must already exist. |
| **Container** | Defaults to `playbooks`. Created on first upload if missing. |
| **Blob prefix** | Defaults to `config-mgmt`. |

## Google Cloud Storage

| Field | Notes |
|---|---|
| **Bucket** | Required. The bucket must already exist. |
| **Object prefix** | Defaults to `config-mgmt`. |

## Local Filesystem / UNC

| Field | Notes |
|---|---|
| **Path** | Required. Either a path inside the dashboard container (typically a bind-mounted host directory like `/srv/playbooks`) or a UNC `\\server\share[\subpath]`. UNC paths use the SMB protocol via the `smbprotocol` Python library — no host-side mount or `cifs-utils` required. |
| **Username / Password / Domain** | Optional, used only for UNC. Username may be `bare` or `DOMAIN\user`; the Domain field is convenience for the latter. Password is encrypted at rest in the dashboard's config DB. |

### Constraint: local backend only works with the local Ansible runner

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

## Remote Filesystem / UNC (via agent)

The same kind of target as the Local backend, with the constraint removed.
The dashboard does not open the SMB socket; a [remote agent](../../admin/remote-agents.md)
already inside your network does, and reports back over the same
outbound-polling channel it uses for hypervisor and Config-Management work.

That inverts who has to be where. A dashboard on Azure Container Apps, or a
[POV instance](../../profiles/pov/README.md) with no cloud provider at all, can use a
corporate share as its storage backend — and because the dashboard fetches
the bytes and hands them to whichever Ansible runner is selected, a **cloud
runner is fine**. There is no `ansible_runner=local` requirement.

| Field | Notes |
|---|---|
| **Agent** | Required. Any enrolled agent, granted the `agent_storage` job type on the Agents page, running **2.5.0 or later**. |
| **Share name** | Required. Must match a `name:` in that agent's `shares.yaml` **and** a `name:` under `storage.shares:` in its `policy.yaml`. |
| **Subpath** | Optional relative directory inside the share. Relative only — an absolute path is refused, not quietly made relative. |
| **SMB password** | Optional, and only for a UNC share whose `shares.yaml` entry says `dashboard_secret: true`. The dashboard holds it encrypted and hands it to the agent sealed, once per job; the agent needs **2.7.0 or later**. Never shown again once saved. See [below](#the-smb-password-can-live-in-the-dashboard). |

### What the dashboard does not hold

**No path and no username,** and by default **no password.** They live in the
agent's own `shares.yaml`, next to its `connections.yaml`, and the dashboard has
no API that can read or write that file. What it stores is one half of a join — the
same arrangement hypervisor connections already use, where the dashboard
holds a connection *name* and the credential never leaves the customer's
host.

That is also why a compromised dashboard cannot reach an arbitrary share.
There is no path field anywhere in the job protocol, so `\\some-other-server\c$`
is not refused, it is unsayable. A job names a share the operator already
wrote down, plus a bare filename; both the dashboard and the agent
independently refuse anything containing a separator or a leading dot.

### The SMB password can live in the dashboard

The one exception, and the one this doc recommends for an agent attested
through SPIRE: set **SMB password** above, put `dashboard_secret: true` on the
share's entry in `shares.yaml`, and delete its `password:`. The agent then
fetches the password for each job through the same sealed per-job fetch a
[hypervisor connection](../../admin/remote-agents/credentials.md#central-storage-with-spire-the-recommended-model)
uses, and nothing durable sits on the host.

- **Only for the configured share and agent.** A storage job for any other share
  or any other agent gets nothing.
- **Under the same gate.** With **Release dashboard-held credentials only to
  SPIRE-attested agents** ticked, an Ed25519 agent is refused.
- **Every release is audited** as `agent.share_secret`, naming the job and the share.
- **The username, domain and path stay in `shares.yaml`.** The dashboard can hand
  over a password, but it cannot aim it at another server.
- **An agent older than 2.7.0** would ignore the key and send whatever password is
  left in `shares.yaml`, so while a password is set the dashboard refuses to queue
  for one. Clear the field to go back to the host-side password.

The agent side of this, `shares.yaml` and the policy, is in
[Agent-brokered file shares](../../admin/remote-agents/file-shares.md); moving the agent to SPIRE is
[Attesting an agent through SPIRE](../../admin/remote-agents/spire-attestation.md).

### Three grants, all required

| Where | What |
|---|---|
| Agents page | the `agent_storage` job type granted to this agent |
| `policy.yaml` → `job_types:` | `agent_storage` |
| `policy.yaml` → `storage:` | `enabled: true` and the share listed by name, plus `write: true` if the dashboard should be able to upload or delete |

Read is implied by naming the share. **Write is off by default** — the
common case is a share somebody else populates and the dashboard only
consumes. Without `write: true`, uploads and deletes are refused by name.
See [`examples/remote-agent/policy.example.yaml`](../../../examples/remote-agent/policy.example.yaml)
and [`shares.example.yaml`](../../../examples/remote-agent/shares.example.yaml).

### Two things that behave differently from a cloud backend

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
[chunked upload lane](uploading.md#two-lanes-and-why-a-311-mb-installer-needs-the-second)
exists only for the object stores.

Like the Local backend, it cannot be the [image-registry hub](image-registry.md)
— promote runners need an HTTPS URL, and a multi-GB VHD does not fit an
envelope — and it holds no Terraform state (see below).

After filling in fields, click **Test connection** to probe the backend —
it lists the bucket/container as a quick reachability check. Save with
**Save configuration**; activation flips the moment the save succeeds.
