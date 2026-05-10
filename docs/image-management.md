# Image Management

This document explains how the dashboard treats VM images — the
philosophy that drives the design, the lifecycle the codebase encodes,
and how a single source image becomes an AMI, an Azure Managed Image,
and a GCP Custom Image.

The companion docs:

- [Infrastructure as Code](infrastructure-as-code.md) — what consumes
  images (Terraform deploys, Packer build orchestration)
- [Storage Management](storage-management.md) — where image artefacts
  live between build and promotion
- [Config Management](config-management.md) — what runs *on* the
  resulting VMs after deployment
- [Secrets Management](secrets-management.md) — credentials feeding
  the build/promote process

---

## Philosophy

Image management is downstream of build discipline. If your image
hygiene is good, deployments are reproducible, vulnerability response
is mechanical, and rollbacks are a pointer-flip. If it isn't, every
deploy is a small adventure. The dashboard tries to make the good path
the easy path.

**1. Build once, deploy many.** The same image artefact ships to AWS,
Azure, and GCP. Re-running Packer per cloud doesn't give you "the same
image" — it gives you three independent images that drift the moment
provisioning steps depend on package mirrors, mirror timing, or
upstream release timing. Build the artefact once; promote that exact
artefact everywhere.

**2. Storage-backed portability.** The image artefact lives in your
[storage backend](storage-management.md) of record (S3 / Azure Blob /
GCS / Local-or-UNC). It's a versioned, named, source-controlled
binary blob. The cloud-specific images (AMI / Managed Image / Custom
Image) are *consumers* of that artefact, not the source of truth. If
the AMI is accidentally deleted, the artefact in storage lets you
re-promote without rebuilding.

**3. Same source, multiple targets.** Promotion to a target cloud is a
distinct, idempotent step that pulls the artefact from storage and
calls the cloud's native VM-import API. Adding a fourth target
(Oracle Cloud, on-prem KVM) in the future is a fourth promoter, not a
fourth Packer template.

**4. Lifecycle hygiene by default.** Every image has a name, a
version, a build manifest (Packer template + provisioner output), and
a destroy path. The dashboard records all four against the build job;
deletes propagate through promoters when the operator wants the
artefact gone everywhere.

---

## How the dashboard implements these

| Principle | Where it shows up |
|---|---|
| Build once, deploy many | The Packer integration ([`services/packer_service.py`](../web_dashboard/services/packer_service.py)) supports three builders today. The roadmap is to standardise on one source builder + post-build conversion to the other clouds' formats, so a single Packer run produces three deploys. |
| Storage-backed portability | `archive_to_s3()`, `archive_to_azure_blob()`, `archive_to_gcs()` already export build outputs to the active storage backend. The artefact lands at `images/<name>-<version>/` keyed by the build job ID. |
| Same source, multiple targets | Each cloud's API (`api/aws.py`, `api/azure.py`, `api/gcp.py`) has create-image-from-source endpoints that accept a storage URL. The promote flow calls them in turn. |
| Lifecycle hygiene | Build jobs land in the standard job tracker (`/jobs`) with the Packer template, provisioner stdout/stderr, and resulting image IDs in `extra_data`. Deleting a build job deletes the artefact from storage and (with confirmation) the derived images. |

---

## The image surfaces

The dashboard has several distinct image-related paths. They overlap
in concept but each has its own lifecycle.

### Packer-driven build

The "I want a custom image baked from scratch" path. Three builders
ship today, picked by the cloud the build runs in:

| Builder | Cloud | Output |
|---|---|---|
| `amazon-ebs` | AWS | EBS-backed AMI |
| `azure-arm` | Azure | Managed Image in your subscription |
| `googlecompute` | GCP | Custom Image in your project |

The Packer template is generated in-process from the deploy form
(source AMI / image, instance type, provisioner script, output
naming). Templates aren't pre-staged in the repo because build inputs
are too varied to template statically. The build job streams Packer
stdout/stderr to the live job log so you can watch the provision
steps.

After the build succeeds, the resulting image's ID is captured in
`Job.extra_data` and the artefact (when exported) is uploaded to your
storage backend.

### Capture from a running instance

The "I have a VM I've been hand-tuning, snapshot it as an image" path.
Every deploy form has a "Create image" action; the dashboard:

1. Stops the instance (or doesn't, depending on the cloud's snapshot
   semantics — AWS allows live snapshot, Azure requires deallocate +
   generalize).
2. Calls the cloud's native image-creation API.
3. Records the resulting image ID against the source-instance job.

Useful for one-offs but not the recommended steady-state path —
captured images are harder to reproduce than Packer-built ones, and
the build manifest is "whatever was on this VM at this moment", which
ages poorly.

### Image browsing

The per-cloud pages list both your private images (account-scoped)
and curated public catalogues:

| Cloud | Private | Public |
|---|---|---|
| AWS | Your account's AMIs (region-scoped) | A curated allow-list of well-known AMI publishers (Amazon Linux, Ubuntu, Debian) |
| Azure | Managed Images + Shared Image Gallery versions | Azure Marketplace (Ubuntu / RHEL / Debian, with provider-publisher whitelist) |
| GCP | Custom Images in your project | Public OS family catalogue (Debian, Ubuntu, Rocky, Windows Server) |

The deploy forms also accept a free-text "Deploy from AMI ID / URN /
Image URI" so you can launch from anything your account can see, not
only the curated lists.

### Storage-backed promotion (the new bit)

The lifecycle this doc is mostly about. Source of truth: the image
artefact (RAW / VHD / VMDK) sitting in your active storage backend.
Targets: AMI / Managed Image / Custom Image, one or more. Promotion
is *not* automatic — the operator picks which clouds to promote to
and the dashboard runs the cloud-native VM-import flow against each.

Format expectations on each target:

| Target | Native import format | Path |
|---|---|---|
| AWS | VMDK / OVA / RAW / VHD via `aws ec2 import-image` | S3 → `import-image` task → AMI |
| Azure | VHD via `az image create --source` | Azure Blob → `Microsoft.Compute/images` resource → Managed Image |
| GCP | RAW (tar.gz wrapped) via `gcloud compute images create --source-uri` | GCS → `images.insert` → Custom Image |

The "matching backend per cloud" design means the artefact you upload
to S3 is reachable by AWS's import API without cross-cloud egress; the
same logical artefact gets a copy in Azure Blob for Azure imports and
in GCS for GCP imports. Migration is two-step: build once, archive to
*your active backend*, then a "fan-out" step copies to the matching
storage in any cloud you're promoting to.

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
4. **Promote** (optional) — operator picks one or more target clouds
   in the image's promote panel. For each:
   - The artefact is copied to the matching storage in that cloud
     (S3 / Azure Blob / GCS).
   - The cloud's VM-import API is called.
   - The resulting native image ID is recorded against the build job.
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
[storage](storage-management.md) labelled `harden-base.sh` and
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
SaaS priorities — see [docs/saas-comparison.md](saas-comparison.md)
for the hosted-edition philosophy.

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

## Troubleshooting

**Packer build hangs at "Waiting for SSH/WinRM."**
The base image's security group / NSG / firewall doesn't permit the
build runner's source IP. Check the cloud-side network policy on the
ephemeral build instance Packer creates.

**Build succeeds but storage upload fails with "no active backend."**
You haven't activated a backend on `/storage`. The artefact is still
registered as a cloud-native image; you can deploy from it. Activate a
backend and re-run the upload step from the build job's actions menu
to enable promotion later.

**Promote to Azure fails with "VHD format unsupported."**
Azure's VM import wants a fixed-size VHD, not a dynamic one (and not
RAW). The dashboard's converter produces fixed-size by default; if
you've replaced it with a custom converter, double-check the output
format with `qemu-img info`.

**Promote to GCP fails with "image source URI access denied."**
The GCP service account configured in Setup → GCP doesn't have
`storage.objects.get` on the GCS bucket you're promoting from. Grant
`roles/storage.objectViewer` on the bucket.

**Image disappears from the per-cloud Private list after a successful
build.**
The image was created in a different region than the per-cloud page
is currently viewing. Use the region selector to find it, or add the
region to the dashboard's `aws_region` / `azure_location` /
`gcp_region` config so the warmer caches it.

**"Build artifact not found" when promoting.**
The build job's storage upload didn't complete (network blip during
upload, backend swap mid-build). Re-run the build, or manually upload
the artefact under the expected `images/<name>-<version>/` key prefix.
