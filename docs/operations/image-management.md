# Image Management

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are about to build a custom image and need to know how it will reach the other clouds.

Part of [Cloud](../clouds.md).

This document explains how the dashboard treats VM images — the
philosophy that drives the design, the lifecycle the codebase encodes,
and how a single source image becomes an AMI, an Azure Managed Image,
a GCP Custom Image, and an OCI Custom Image.

The companion docs:

- [Infrastructure as Code](../clouds/infrastructure-as-code.md) — what consumes
  images (Terraform deploys, Packer build orchestration)
- [Storage Management](../operations/storage-management.md) — where image artefacts
  live between build and promotion
- [Config Management](../operations/config-management.md) — what runs *on* the
  resulting VMs after deployment
- [Secrets Management](../admin/secrets-management.md) — credentials feeding
  the build/promote process

---

## Philosophy

Image management is downstream of build discipline. If your image
hygiene is good, deployments are reproducible, vulnerability response
is mechanical, and rollbacks are a pointer-flip. If it isn't, every
deploy is a small adventure. The dashboard tries to make the good path
the easy path.

**1. Build once, deploy many.** The same image artefact ships to AWS,
Azure, GCP, and OCI. Re-running Packer per cloud doesn't give you "the
same image" — it gives you four independent images that drift the moment
provisioning steps depend on package mirrors, mirror timing, or
upstream release timing. Build the artefact once; promote that exact
artefact everywhere.

**2. Storage-backed portability.** The image artefact lives in your
[storage backend](../operations/storage-management.md) of record (S3 / Azure Blob /
GCS / OCI Object Storage / Local-or-UNC). It's a versioned, named, source-controlled
binary blob. The cloud-specific images (AMI / Managed Image / Custom
Image) are *consumers* of that artefact, not the source of truth. If
the AMI is accidentally deleted, the artefact in storage lets you
re-promote without rebuilding.

**3. Same source, multiple targets.** Promotion to a target cloud is a
distinct, idempotent step that pulls the artefact from storage and
calls the cloud's native VM-import API. A new target (on-prem KVM,
say) is a new promoter, not a new Packer template — a cloud only needs
its own builder when you want to *bake* images there, which is why
Oracle Cloud has both.

**4. Lifecycle hygiene by default.** Every image has a name, a
version, a build manifest (Packer template + provisioner output), and
a destroy path. The dashboard records all four against the build job;
deletes propagate through promoters when the operator wants the
artefact gone everywhere.

---

## How the dashboard implements these

| Principle | Where it shows up |
|---|---|
| Build once, deploy many | The Packer integration ([`services/packer_service.py`](../../web_dashboard/services/packer_service.py)) supports four builders today. The roadmap is to standardise on one source builder + post-build conversion to the other clouds' formats, so a single Packer run produces four deploys. |
| Storage-backed portability | `archive_to_s3()`, `archive_to_azure_blob()`, `archive_to_gcs()`, `archive_to_oci()` already export build outputs to the active storage backend. The artefact lands at `images/<name>-<version>/` keyed by the build job ID. |
| Same source, multiple targets | Each cloud's API (`api/aws.py`, `api/azure.py`, `api/gcp.py`, `api/oci.py`) has create-image-from-source endpoints that accept a storage URL. The promote flow calls them in turn. |
| Lifecycle hygiene | Build jobs land in the standard job tracker (`/jobs`) with the Packer template, provisioner stdout/stderr, and resulting image IDs in `extra_data`. Deleting a build job deletes the artefact from storage and (with confirmation) the derived images. |

---

## The image surfaces

The dashboard has several distinct image-related paths. They overlap
in concept but each has its own lifecycle.

### Pages

One page per surface:

| Page | Read it when |
|---|---|
| [Building an image with Packer](image-management/packer-build.md) | you are building a custom image from a base image and a provisioner script |
| [Capturing an image from a running instance](image-management/capture.md) | you have a VM configured the way you want and want an image of it |
| [Browsing images](image-management/browsing.md) | you want to see which images each cloud has, and which of them the dashboard built |
| [Promoting an image to the other clouds](image-management/promotion.md) | you built an image in one cloud and want it in the others, through the storage backend |
| [Troubleshooting images](image-management/troubleshooting.md) | a build, capture or promotion failed, or an image is missing where you expected it |

---

## Workflow

A typical build-and-promote cycle:

1. **Build** — operator picks a base image, a provisioner script
   (shell, Ansible playbook from the storage backend, or both), and a
   target cloud for the source build. Packer runs in a one-shot
   container; build stdout streams to the job log.
2. **Capture artefact** — Packer output is exported to a portable
   format (VHD for cross-cloud reach) and uploaded to the active
   storage backend at `images/<name>-<version>/`.
3. **Cloud-native registration** — in the source cloud, the artefact
   is also registered as that cloud's native image (AMI / Managed
   Image / Custom Image). You can deploy from it immediately even
   without promotion.
4. **Promote** (optional) — operator picks a target cloud (+ region
   for AWS, resource group for Azure) in the image's promote panel.
   The dashboard enqueues an `image_promote_<target>` Job; the target
   cloud's runner pulls the hub artefact, converts format if needed,
   uploads to target-cloud staging, calls the cloud's VM-import API,
   and records the resulting native image ID on
   `RegisteredImage.promotions[<target>]`. Staged blobs are deleted
   after the cloud-side image reaches its terminal-ready state.
5. **Deploy** — the per-cloud deploy forms see the new images in
   their respective lists and can launch instances from them.

Destruction is the same path in reverse: pick the build job, "delete
image and all promotions", and the dashboard tears down the native
images in each cloud and removes the artefact from storage.

---

## Best practices

**Treat images like git tags.** Build immutably, name deterministically
(`hardened-ubuntu-22.04-2026-04-12`), promote from named versions
rather than `:latest`. The dashboard supports the build/promote
naming explicitly but doesn't enforce hygiene.

**Test in one cloud before promoting.** A successful Packer build
proves provisioning ran; it doesn't prove the resulting image actually
works. Deploy a single test VM in the source cloud, sanity-check the
service comes up, *then* promote.

**Pin source-image versions in your Packer template.** Don't say "the
latest Ubuntu 22.04" — say "ami-xxxxxxxxxxxxxxxxx as of 2026-04-12".
Otherwise builds become a function of when you ran them, and "rebuild
from manifest" loses meaning.

**Version your provisioner scripts.** The provisioner is part of the
image's manifest. A `.sh` script in
[storage](../operations/storage-management.md) labelled `harden-base.sh` and
re-edited in place gives you the same problem as a mutable image:
you can't reproduce older builds. Date-stamp or version it.

**Don't promote to a cloud you don't have credentials for.** The
promote flow needs cloud-native VM-import permissions in addition to
the dashboard's normal deploy permissions. AWS needs
`ec2:ImportImage`, Azure needs `Microsoft.Compute/images/write`, GCP
needs `compute.images.create` plus a service account that can read
from the source GCS object. Configure these once before your first
promotion run.

**Delete propagation is opt-in.** When you delete a build, the
dashboard offers (but doesn't enforce) "delete derived AMI / Managed
Image / Custom Image too." For production images that are still in
use somewhere, leave them alone; for one-off builds, sweep them with
the artefact.

**Promotions are independent jobs.** A failed Azure promotion doesn't
roll back the AWS one. Each lands in `/jobs` with its own success /
failure state. Mostly this is a feature — partial fan-out is a normal
state — but plan retries explicitly rather than expecting an atomic
all-or-none outcome.

---

## Where this is heading on SaaS

A few things the community edition does *not* try to do. They're
SaaS priorities — see [docs/editions/comparison.md](../editions/comparison.md)
for the hosted-edition philosophy.

> **Already shipped in community (was previously on this list):**
> one-click cross-cloud promote and pre-flight cloud-credential checks.
> The runner-driven promote flow above runs in the community edition;
> SaaS retains a stronger guarantee — durable replay-safe workflows
> (Temporal-backed) so a 45-minute import survives a dashboard restart
> mid-poll without orphan tasks. Same registry, same `/images` UI,
> same audit trail; the SaaS edition just adds replay-safety on top.

- **Local image builds via Azure Arc.** The hosted edition can
  register an Azure Arc runbook worker on your on-prem build host and
  run image builds *there*, against your VMware / Hyper-V hypervisor,
  without sending build traffic through public internet egress. The
  resulting artefact gets pushed to the SaaS-tenant storage backend
  and is available for cloud promotion exactly the same way as
  cloud-built images. Same workflow, different runner location.
- **Continuous CVE scanning per image version.** SaaS keeps every
  build's manifest and component bill-of-materials, runs scheduled
  vulnerability checks against published CVE feeds, and surfaces
  affected images in the dashboard. Community runs leave this kind
  of telemetry on the table — image hygiene is your discipline, not
  the platform's.
- **AI-assisted hardening.** Suggestions like "this image is missing
  CIS benchmark §5.2.3 — apply this provisioner snippet to your next
  rebuild." Same shape as the AI-assisted Ansible playbook generation
  the SaaS edition will offer for config management.
- **Centralised image catalog across tenants.** Multi-tenancy means
  one organisation's blessed base image is reusable by every team in
  that org without duplicating storage. Community is single-tenant by
  design; one image library per dashboard deployment.

The build-once-promote-many philosophy in this doc carries forward to
SaaS unchanged. What changes is *where the build happens* (Arc-managed
on-prem worker vs. the dashboard host's local Docker), the audit
trail (per-tenant signed build manifests vs. the community edition's
job log), and the cross-tenant catalog.

---

