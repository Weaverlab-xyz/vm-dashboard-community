# Building an image with Packer

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are building a custom image from a base image and a provisioner script.

Part of [Image Management](../image-management.md).

The "I want a custom image baked from scratch" path. Four builders
ship today, picked by the cloud the build runs in:

| Builder | Cloud | Output |
|---|---|---|
| `amazon-ebs` | AWS | EBS-backed AMI |
| `azure-arm` | Azure | Managed Image in your subscription |
| `googlecompute` | GCP | Custom Image in your project |
| `oracle-oci` | OCI | Custom Image in your compartment |

The Packer template is generated in-process from the deploy form
(source AMI / image, instance type, provisioner script, output
naming). Templates aren't pre-staged in the repo because build inputs
are too varied to template statically. The build job streams Packer
stdout/stderr to the live job log so you can watch the provision
steps.

## GCP source image: a family or an exact name

The GCP form's **Source Image** control has two modes, and the
difference matters for anything you did not get from a public
catalogue:

- **Public family** (the default) emits `source_image_family` —
  `debian-12`, `rocky-linux-9`, `ubuntu-2204-lts-amd64` — which
  resolves to the family's newest image at build time.
- **Custom image** emits a literal `source_image`, the exact name of an
  image in your project. This is the mode an image you **imported
  yourself** requires: an imported image carries no family, so no value
  in the family field can ever name it. It is the GCE counterpart of
  the AWS form's literal source AMI.

The optional **Image project** under Custom image emits
`source_image_project_id`. Leave it blank unless the image lives in a
separate shared image project — unset, the builder searches the build
project first and the standard public image projects after it.

Picking the wrong mode is a build that runs, and then fails a minute in
against the wrong OS. The provisioners that check what they were given
(`provisioners/net/vyos-cell.sh` refuses a non-VyOS image) report it
clearly; one that does not will happily bake the wrong thing. The job
log's template step names the source it used either way.

## OCI build prerequisites

The OCI builder has two requirements the other three don't, because
Packer runs inside the dashboard's worker container — outside your
tenancy's VCN — and reaches the throwaway build instance over the
public internet:

* **A public subnet.** The build form only offers subnets whose
  `prohibit_public_ip` is false, and the generated template sets
  `assign_public_ip = true`. The default `oci_default_subnet_ocid`
  points at the *private* VM subnet, which cannot work here.
* **Inbound TCP 22 from the dashboard's egress address.** The sandbox
  script adds this rule scoped to a single `/32` (§6c of
  `scripts/sandbox/Linux/setup-oci.sh`; override with
  `OCI_BUILD_SSH_CIDR`, or `skip` to opt out). Without it the build
  hangs at *Waiting for SSH* until it times out.

A third, softer one: the base image's architecture must match the
shape. Oracle encodes `aarch64` in platform image display names, so
the form warns when an Arm image is paired with an x86 shape or vice
versa — advisory, because a custom image may be named anything.

### The shape has to exist in *your* region

`LaunchInstance` answers three different mistakes with the same
unattributed `404 NotAuthorizedOrNotFound` — no field named, one second
into the build:

1. the shape isn't offered in that availability domain,
2. the base image doesn't support the shape,
3. a genuine IAM policy denial.

Case 2 can instead come back as `400 InvalidParameter` naming both
("Shape VM.Standard.A1.Flex is not valid for image ocid1.image…"), which
is the one legible failure of the three — but only after Packer has
already created a key pair and started the build.

The first is the easy trap, because the default build shape is
`VM.Standard.E2.1.Micro`, the Always-Free AMD micro — and **E2 shapes
exist only in the older OCI regions.** A newer region such as
`us-chicago-1` offers no E2 shape at all, so the out-of-the-box default
is unlaunchable there. Worse, the free tier has no x86 substitute: the
other Always-Free shape, `VM.Standard.A1.Flex`, is Ampere, so it pairs
only with an `aarch64` base image.

There is a harder version of this trap, because **`ListShapes` is scoped
by your service limits, not just by what hardware the region has.** A
trial tenancy sees only the shapes it holds quota for — measured live in
`us-chicago-1` on 2026-08-19: all three availability domains returned
exactly three shapes, every one of them Ampere (`BM.Standard.A1.160`,
`VM.Standard.A1.Flex`, `VM.Standard.A2.Flex`), while the x86
`Oracle-Autonomous-Linux-10.1` image listed 69 compatible shapes. The
two lists are **disjoint**, so *no* x86 image can be built anywhere in
that region under that tenancy, at any price. Switching to an `aarch64`
base image is the only way forward, not a matter of paying for a bigger
shape.

The build form fills its shape list live and drops a default the region
can't launch, and both the API route and the job runner precheck the
placement before Packer starts
(`oci_service.check_launch_placement`) — so cases 1 and 2 now fail
with a message naming the shape and listing what would work. The two
OCI *deploy* endpoints run the same check for the same reason — see the
`shape_not_launchable` entry in [Cloud VMs](../../clouds/vms.md).

That precheck **fails open when a lookup is silent** — it can't reach
OCI, or one of the two lists (`ListShapes`, or the image's compatibility
entries) comes back empty and so tells us nothing. It does **not** fail
open when both lists answer and share nothing: an empty intersection
between two non-empty lists is OCI saying plainly that nothing offered
here boots this image, and reading that as "unknown" is exactly what let
an Ampere shape under an x86 image on 2026-08-19. So a bare 404 that
survives all of the above is most likely case 3 — check the policies on
the compartment holding the subnet and the image.

To build an x86 image in a region without E2, pick a paid shape
(`VM.Standard.E5.Flex` at 1 OCPU costs cents for a build that lasts
minutes) and acknowledge the free-tier warning — but check the shape
picker first: if the only shapes offered are `A1`/`A2`, your tenancy has
no x86 quota there and no amount of paying will change it. To stay
inside the free tier, or to build at all in an Ampere-only tenancy, use
an `aarch64` base image with `VM.Standard.A1.Flex`.

Set `storage_oci_bucket` on the Storage page to have builds export to
VHD and register in the image hub; without it the build still produces
the custom image and reports `export_skipped`.

After the build succeeds, the resulting image's ID is captured in
`Job.extra_data` and the artefact (when exported) is uploaded to your
storage backend.

## Loading the provisioner script from storage

Every Packer form (AWS / Azure / GCP / OCI) has a **Load from storage**
dropdown next to the Provisioner Script field. It lists every
`.sh` / `.bash` asset across every configured storage backend
(local, S3, Azure Blob, GCS) tagged with the backend it lives on —
`setup.sh (S3)`, `setup.sh (Local)` — so identical filenames on
different backends are unambiguous.

Selecting an entry fetches the script via
[`GET /api/storage/fetch/{backend}/{name}`](../../../web_dashboard/api/storage.py)
and drops the text into the textarea. The operator can still tweak
it before submitting; a blue subtitle echoes which backend + name
the script came from so an edited version doesn't quietly drift
from its stored copy.

This means you can keep your hardening scripts version-controlled
on disk or in object storage, upload them once via
[Storage Management](../storage-management.md), and pick them from
the dropdown for every build instead of copy-pasting. Useful when
the same script is reused across cloud providers — store it once on
a cloud backend and load it for all three builds.

## Passing environment variables to the provisioner

Once a provisioner script is loaded, every Packer form (AWS / Azure /
GCP / OCI) shows an **Environment variables** panel — a repeatable
name/value table whose entries are handed to the shell provisioner as
`environment_vars`. Use it to parameterise a reused script (package
versions, feature flags, registration endpoints) instead of forking
the script per build.

Each row has a **secret ref** toggle:

- **Off (literal).** The value is inlined into the generated Packer
  template verbatim.
- **On (secret reference).** The value is a reference into your
  configured [secrets backend](../../admin/secrets-management.md) —
  `aws_sm://dashboard/db_password`, `azure_kv://db-password`,
  `gcp_sm://db-password`, or `bt_safe://…`. It's resolved at
  build-launch (close to when the provisioner runs) via
  `config_service`, the same resolver the rest of the dashboard uses.

Secret values are deliberately kept **out of the template**: a
resolved secret is passed through a Packer **sensitive variable**
(`PKR_VAR_*` injected into the build subprocess), and the template
only ever contains the declaration plus a `${var.…}` reference. So a
secret never lands in the **archived** template (when "Archive
template" is on, the `.pkr.hcl` is uploaded to your storage backend)
and Packer redacts it from the build log. Literal values, having no
such protection, should not be used for secrets.

Environment-variable names are validated (`[A-Za-z_][A-Za-z0-9_]*`)
so a name can't break out of the `environment_vars` array.

> **BeyondTrust provisioner options.** Above the generic table sits a
> dedicated **BeyondTrust provisioner options** block (admin user,
> Install EPM-L deb/rpm, Entitle SSH public key) — a convenience layer
> over the same mechanism that drives the
> [`bt-ready-*` scripts](../../../provisioners/beyondtrust/README.md). It
> sets `BT_ADMIN_USER` / `BT_ENTITLE_PUBKEY` and, for EPM-L, resolves a
> fresh BeyondTrust presigned package URL into `BT_EPML_URL` via the
> [EPM-L integration](../../beyondtrust/epml.md) at build-launch
> (those links expire ~30 min). On Azure the panel shows only for
> Linux builds — the Windows path uses a PowerShell provisioner, not
> the shell `environment_vars` mechanism.

## Windows builds (Azure)

The Azure Packer form builds **Windows** managed images too — pick a
Windows preset (Windows Server 2022 / 2022 Core) or set
`os_type: "Windows"` on `POST /api/packer/azure/build`. Differences
from the Linux path:

- **WinRM, not SSH.** Packer connects over WinRM HTTPS (5986) to the
  temp build VM and provisions a *transient Azure Key Vault* to hold
  the WinRM certificate — the dashboard's service principal needs Key
  Vault create rights in the build resource group and the
  `Microsoft.KeyVault` provider registered on the subscription.
- **PowerShell provisioner.** The provisioner script is a `.ps1`
  (the storage dropdown filters to `.ps1` assets when Windows is
  selected) and runs before a `windows-restart`, so feature installs
  settle before generalization.
- **Sysprep, not waagent.** The build always ends with the canonical
  Azure finisher: wait for `RdAgent` + `WindowsAzureGuestAgent`,
  `Sysprep /oobe /generalize`, then poll `ImageState` until the
  reseal completes. Without it the captured image is specialized and
  undeployable.
- **Sizing.** Windows builds crawl on 4 GB burstable sizes; the form
  nudges Windows presets to `Standard_D2s_v3`. Expect 30–60+ min.

A ready-made starter provisioner ships at
[`provisioners/beyondtrust/bt-ready-windows.ps1`](../../../provisioners/beyondtrust/bt-ready-windows.ps1):
it installs **OpenSSH Server**, enables **RDP + NLA**, sets the SSH
default shell to PowerShell, and (optionally) authorizes an SSH public
key — turning a Windows Server Core image into one you can reach by
`ssh` like a Linux VM, plus agentless RDP through the Gateway. Upload
it to `/storage` (the layer tags `.ps1` as `powershell`) and Load it,
or paste it in. See
[provisioners/beyondtrust/README.md](../../../provisioners/beyondtrust/README.md#windows-bt-ready-windowsps1).

Deploying a Windows image (deploy form, bulk deploy, or a Desktops
pool) generates a strong local-admin password per VM, stores it in
the configured [secrets backend](../../admin/secrets-management.md) (the
`database` backend works out of the box), and records only the
`(backend, ref)` pair in job metadata. Retrieve it per VM via
**Azure → VMs → Password** (`GET /api/azure/vms/{name}/admin-password`).
Azure can't inject an SSH key into a Windows VM at deploy time (that's
Linux-only), so the BeyondTrust Shell Jump (SSH) step is skipped at
deploy — but if the image baked OpenSSH + your key (above), you can SSH
in directly, and otherwise broker access with an RDP jump item.
The image registry records `os_type` per image so cross-cloud
promotes import Windows VHDs as Windows (registry rows predating the
column default to Linux).

## Windows 11 (Trusted Launch + Compute Gallery)

Windows 11 desktop images differ from the Windows **Server** path above.
Win 11 requires **Trusted Launch** (Secure Boot + vTPM), and Azure
**cannot create a managed image from a Trusted Launch VM** — so the
builder publishes a **Compute Gallery image version** instead of a
managed image. Pick the **Windows 11 multi-session (24H2 AVD)** preset
(`microsoftwindowsdesktop / windows-11 / win11-24h2-avd`), which sets
`trusted_launch`. What the build does differently:

- **Auto-creates the gallery + image definition.** Before the build,
  the dashboard ensures a Compute Gallery (`azure_shared_image_gallery`,
  or `vmDashboardGallery` by default, in `azure_gallery_resource_group`)
  and a **Gen2 / TrustedLaunch / Generalized** Windows image definition
  exist (idempotent). Packer then publishes a version `1.<YYMMDD>.<HHMMSS>`
  into it.
- **No managed image, no VHD export.** The gallery version *is* the
  artefact; the registry records its full resource id with
  `os_type=Windows` (no hub VHD copy).
- **Trusted Launch on deploy.** Deploying a gallery Windows image (deploy
  form, bulk, or a Desktops pool) sets a `SecurityProfile`
  (`TrustedLaunch` + Secure Boot + vTPM) and `license_type=Windows_Client`.
  The pickers auto-detect this (`source=gallery` + `os_type=Windows`);
  pasted ARM ids get a manual **Trusted Launch** checkbox.

**Multi-session ≠ AVD here.** The 24H2 AVD SKU is a multi-session OS, but
the dashboard does **not** install the AVD agent or register an AVD host
pool — VMs stay PRA-RDP-brokered, one per seat; the multi-session
capability is latent.

**Prerequisites.** Windows 11 client images are only visible to **eligible
offers** — Visual Studio / dev-test subscriptions, or AVD / Microsoft 365
E3+ (multi-tenant hosting rights). On an ineligible subscription the build
VM creation fails with an image-not-found error. The build **and** deploy
sizes must be **Gen2 / Trusted-Launch capable** (e.g. `Standard_D2s_v3`) —
**B-series do not support Trusted Launch.**
