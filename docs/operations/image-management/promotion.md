# Promoting an image to the other clouds

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you built an image in one cloud and want it in the others, through the storage backend.

Part of [Image Management](../image-management.md).

The lifecycle this doc is mostly about, surfaced on the
**`/images`** page. Source of truth: the image artefact (VHD by default)
sitting in your **hub backend**, recorded as a `RegisteredImage` row.
Targets: AMI / Managed Image / Custom Image, one or more.

The end-to-end flow:

<figure style="margin:2rem 0">
<div style="overflow-x:auto">
<svg viewBox="0 0 860 650" role="img" aria-label="The image promotion path: a Packer build is exported to the hub backend — with no copy when the build cloud already matches the hub, or a cross-backend storage_service.copy() when it does not — then pulled by a target-cloud runner over a presigned URL, converted with qemu-img into target-cloud staging, imported by the cloud&#39;s own API into a native image, and recorded on RegisteredImage.promotions." style="width:100%;min-width:720px;height:auto;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif">
<defs>
<marker id="pmarw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="currentColor"/></marker>
</defs>
<g fill="currentColor" text-anchor="middle">
<rect x="24" y="44" width="812" height="224" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="68" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">1 · BUILD AND LAND ON THE HUB</text>
<rect x="44" y="128" width="150" height="56" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="119" y="150" font-size="12">Packer build</text>
<text x="119" y="166" font-size="10" fill="#475569">AWS · Azure · GCP · OCI</text>
<text x="253" y="147" font-size="10" fill="#475569">exports a VHD</text>
<line x1="194" y1="156" x2="312" y2="156" stroke="currentColor" marker-end="url(#pmarw)"/>
<rect x="316" y="128" width="164" height="56" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="398" y="150" font-size="12">Native export to</text>
<text x="398" y="166" font-size="12">same-cloud storage</text>
<text x="610" y="80" font-size="10.5" fill="#475569">build cloud = hub backend</text>
<text x="610" y="92" font-size="10.5" fill="#475569">the native export is the hub upload — no copy</text>
<path d="M484,144 L536,104 L732,104 L732,122" fill="none" stroke="currentColor" marker-end="url(#pmarw)"/>
<path d="M648,138 A84,10 0 0 1 816,138 V182 A84,10 0 0 1 648,182 Z" fill="#eff6ff" stroke="#1d4ed8"/>
<path d="M648,138 A84,10 0 0 0 816,138" fill="none" stroke="#1d4ed8"/>
<text x="732" y="162" font-size="11.5" font-weight="600" fill="#1d4ed8">Hub backend</text>
<text x="732" y="176" font-size="10" fill="#1d4ed8">S3 · Blob · GCS</text>
<text x="424" y="218" text-anchor="end" font-size="10.5" fill="#475569">build cloud ≠ hub backend</text>
<text x="424" y="231" text-anchor="end" font-size="10.5" fill="#475569">(AWS build, hub on Azure Blob)</text>
<path d="M440,184 L440,229 L496,229" fill="none" stroke="currentColor" marker-end="url(#pmarw)"/>
<rect x="500" y="206" width="164" height="46" rx="7" fill="#f1f5f9" stroke="#94a3b8"/>
<text x="582" y="225" font-size="11">storage_service.copy()</text>
<text x="582" y="240" font-size="9.5" fill="#475569">staging copy then deleted</text>
<path d="M668,229 L732,229 L732,194" fill="none" stroke="currentColor" marker-end="url(#pmarw)"/>
<rect x="24" y="284" width="812" height="156" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="308" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">2 · CONVERT IN THE TARGET CLOUD</text>
<text x="782" y="272" text-anchor="end" font-size="10.5" fill="#475569">presigned URL</text>
<path d="M790,191 L790,330" fill="none" stroke="currentColor" marker-end="url(#pmarw)"/>
<rect x="576" y="334" width="240" height="60" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="696" y="358" font-size="12">Target-cloud runner</text>
<text x="696" y="375" font-size="10" fill="#475569">ECS · ACI · Cloud Run</text>
<text x="410" y="344" font-size="10.5" fill="#475569">qemu-img convert</text>
<text x="410" y="356" font-size="10.5" fill="#475569">+ upload</text>
<line x1="572" y1="366" x2="248" y2="366" stroke="currentColor" marker-end="url(#pmarw)"/>
<path d="M44,344 A100,10 0 0 1 244,344 V384 A100,10 0 0 1 44,384 Z" fill="#f1f5f9" stroke="#94a3b8"/>
<path d="M44,344 A100,10 0 0 0 244,344" fill="none" stroke="#94a3b8"/>
<text x="144" y="366" font-size="11.5" font-weight="600">Target-cloud staging</text>
<text x="144" y="379" font-size="10" fill="#475569">bucket or container</text>
<text x="430" y="426" font-size="10.5" fill="#475569">The convert runs inside the target cloud — image bytes never transit the dashboard container.</text>
<rect x="24" y="456" width="812" height="174" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="480" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">3 · IMPORT AND RECORD</text>
<text x="152" y="448" text-anchor="start" font-size="10.5" fill="#475569">the import API reads the staged object</text>
<path d="M144,396 L144,504" fill="none" stroke="currentColor" marker-end="url(#pmarw)"/>
<rect x="44" y="508" width="204" height="84" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="146" y="528" font-size="11" font-weight="600">Cloud import API</text>
<text x="146" y="545" font-size="9.5" fill="#475569">ec2.ImportImage</text>
<text x="146" y="558" font-size="9.5" fill="#475569">images.create_or_update</text>
<text x="146" y="571" font-size="9.5" fill="#475569">images.insert</text>
<text x="289" y="541" font-size="10" fill="#475569">creates</text>
<line x1="252" y1="550" x2="326" y2="550" stroke="currentColor" marker-end="url(#pmarw)"/>
<rect x="330" y="508" width="204" height="84" rx="7" fill="#eff6ff" stroke="#1d4ed8"/>
<text x="432" y="534" font-size="11.5" font-weight="600" fill="#1d4ed8">Native image</text>
<text x="432" y="552" font-size="10" fill="#1d4ed8">AMI · Managed Image</text>
<text x="432" y="566" font-size="10" fill="#1d4ed8">Custom Image</text>
<text x="577" y="541" font-size="10" fill="#475569">recorded as</text>
<line x1="538" y1="550" x2="616" y2="550" stroke="currentColor" marker-end="url(#pmarw)"/>
<rect x="620" y="508" width="196" height="84" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="718" y="530" font-size="11" font-weight="600">RegisteredImage</text>
<text x="718" y="545" font-size="11" font-weight="600">.promotions</text>
<text x="718" y="564" font-size="10" fill="#475569">one row per target cloud</text>
<text x="430" y="614" font-size="10.5" fill="#475569">The hub artefact stays the source of truth; each target cloud adds its own promotion row.</text>
</g>
</svg>
</div>
<figcaption style="font-size:.85rem;color:#475569;margin-top:.5rem">The promotion path, from Packer build to <code>RegisteredImage.promotions</code>. Two edges reach the hub: when the build cloud already matches the hub backend the native export <em>is</em> the hub upload and nothing is copied; when it does not, <code>storage_service.copy()</code> moves the VHD across and deletes the staging copy.</figcaption>
</figure>

## Build → hub

After a successful Packer build the dashboard exports the image to a
portable VHD via the cloud's native API and lands it on the **hub**
backend, recording the resulting URL on `RegisteredImage.artefact_url`.
You set the hub on `/storage` via `storage_hub_backend`; if unset it
falls back to the active backend (so single-backend installs Just
Work without configuration). Per build cloud:

- **AWS** — `ec2:ExportImage` writes a VHD to S3. Requires the
  `vmimport` IAM service role; override the role name with
  `aws_vmimport_role_name` if you've named yours differently.
- **Azure** — Snapshot the managed image's OS disk, grant a read-only
  SAS URL, server-side blob-copy into the configured Azure Blob
  container as a VHD, then revoke and clean up the snapshot. No
  egress through the dashboard.
- **GCP** — Submit a Cloud Build job running the upstream
  `gcr.io/compute-image-tools/gce_vm_image_export` Daisy workflow
  with `-format=vpc`, writing the VHD to GCS.

When the build cloud matches the hub backend, the native export *is*
the hub upload — no extra copy. When they differ (e.g. AWS build with
hub = Azure Blob), the dashboard runs `storage_service.copy()` to move
the VHD from same-cloud staging into the hub, then deletes the
staging copy so you don't pay for two. With the hub on Azure Blob the
copy is fully server-side: the staging object gets a presigned URL and
the Azure storage service pulls each block itself, so no image bytes
transit the dashboard container. With the hub on S3/GCS/OCI and a
*different* build cloud, the copy stages through the container's
ephemeral disk — which multi-GB VHDs can overflow — so for
cross-cloud building either put the hub on Azure Blob or match it to
the build cloud. See `_land_on_hub()` in
[`services/packer_build_service.py`](../../../web_dashboard/services/packer_build_service.py).

## Manual export (recovery path)

The auto-export above is part of every successful Packer build, but
sometimes it gets skipped — no S3 bucket configured, no Azure storage
account on the `/storage` page, the export task timed out mid-build,
etc. The Packer build still completes and the cloud-native image (AMI
/ Managed Image / Custom Image) is fine; it just isn't on the hub and
isn't registered for cross-cloud promotion.

To recover without rebuilding, every cloud Images tab has an
**Export VHD** action on each existing image:

- **AWS Private AMIs** — Export VHD button alongside Deploy / Delete.
- **Azure Managed Images** — Export VHD action.
- **GCP Custom Images** — Export VHD on each image card.

Clicking it prompts for a registry name (defaults to a sanitized
version of the image's native name), then runs the same export →
land-on-hub → register flow that the Packer post-build path runs.
You're redirected to `/jobs/<id>` for live progress. End-to-end this
takes 15–60 minutes depending on image size and whether the build
cloud is the hub cloud (no copy) or a different cloud (cross-backend
copy).

Endpoints behind the buttons:
- `POST /api/aws/amis/{ami_id}/export`
- `POST /api/azure/images/{image_name}/export` (optional
  `resource_group` in body; defaults to `azure_resource_group`)
- `POST /api/gcp/images/{image_name}/export`

Each takes `{image_name: <registry name>}` and returns
`{job_id, status, message}`. Same cloud-storage prerequisites apply
as for the auto-export — AWS needs an S3 bucket, Azure needs a
storage account, GCP needs a GCS bucket. The job log surfaces a
clear "Export skipped: no S3 bucket configured" (or equivalent) if
the prerequisite is missing.

## Pre-flight checks

The **Promote** modal runs an advisory pre-flight check the moment
you pick a target cloud. The checks are pure-Python (no cloud-side
API calls, returns in <100ms) and cover the local-state blockers
visible without leaving the dashboard:

- Artefact recorded — `artefact_url` and `artefact_format` are set
- Format compatibility — VHD/VMDK/RAW/OVA matrix per target cloud
- Cross-storage copy required — informational, since the runner
  handles it automatically
- Target credentials configured — the dashboard's config store has
  the credentials the target cloud's import API will use

Failing checks don't block — the **Promote** button still works —
but they're surfaced visually so the operator doesn't kick off a
30-minute import only to discover credentials were missing. The
endpoint is `POST /api/images/{id}/preflight` with `{target_cloud}`
body; response shape is `{checks: [{name, status: pass|warn|fail, detail}]}`.
Runner-time errors (IAM mid-flight, quota, format quirks) surface
on `/jobs/<id>` once the actual promote runs.

## Automated cross-cloud promote

When you click **Promote** on an image, the dashboard enqueues an
`image_promote_<target>` Job and runs the conversion + import in a
transient container in the *target* cloud:

| Target | Runner | Cloud SDK call | Conversion |
|---|---|---|---|
| AWS | ECS Fargate task | `ec2.ImportImage` | None (VHD passthrough) |
| Azure | ACI container group | `compute.images.begin_create_or_update` | None (VHD passthrough) |
| GCP | Cloud Run Job | `compute.images.insert` | `qemu-img vhd → raw` + `tar.gz`-wrap with `disk.raw` entry |
| OCI | Container Instance | `compute.create_image` (Object Storage tuple) | `qemu-img vhd → qcow2` (OCI import reads QCOW2) |

The runner pulls the hub artefact via a short-lived presigned URL
minted at task-launch time, so it never holds hub-side credentials.
On exit the dashboard calls the cloud's image-import API against the
staged blob, polls until the resulting image is `Available` /
`Succeeded` / `READY`, then deletes the staged copy and records the
final identifier on `RegisteredImage.promotions[<target>]`.

The runner image is `chrweav/dashboard-promote-runner:latest`
by default; override via `promote_runner_image` if you maintain a
hardened private build. See
[`runners/promote/README.md`](../../../runners/promote/README.md) for the
operator prerequisites (IAM, quotas, networking) per target cloud,
the full list of `promote_runner_*` config keys, and local build
instructions for maintaining a custom or hardened build.

If your dashboard credentials can't reach the target — e.g. cross-
account promotes or air-gapped tenants — pass `?manual=1` on the
promote endpoint (or click "Show manual steps instead" in the modal)
and the dashboard returns the operator-runnable CLI walkthrough as
before. The promotion is recorded as `manual` in the registry; run
the commands yourself, then re-promote (or `record_promotion` via the
API) to fill in the resulting native image ID.

Format expectations per target:

| Target | Native import format | Path |
|---|---|---|
| AWS | VMDK / OVA / RAW / VHD via `ec2.ImportImage` | hub → staging S3 → `import-image` task → AMI |
| Azure | VHD via `Microsoft.Compute/images` | hub → staging Blob → `images.create_or_update` → Managed Image |
| GCP | tar.gz containing `disk.raw` via `images.insert` | hub → staging GCS → `images.insert` → Custom Image |
