# The image-registry hub

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want container images mirrored into each cloud's registry from one place.

Part of [Storage Management](../storage-management.md).

The hub backend is the single storage backend that holds the canonical
VHD/raw artefact for every registered image, regardless of which cloud
built it. It's the source the cross-cloud promote flow reads from when
it kicks off a per-target runner — see
[Image Management](../image-management.md) for the full lifecycle and
[`runners/promote/README.md`](../../../runners/promote/README.md) for the
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
[Image Management → Manual export](../image-management/promotion.md#manual-export-recovery-path).

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
[runner README](../../../runners/promote/README.md) has the full table.
