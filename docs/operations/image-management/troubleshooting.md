# Troubleshooting images

> **Audience:** operator · **Profile:** `demo` · **Read this when:** a build, capture or promotion failed, or an image is missing where you expected it.

Part of [Image Management](../image-management.md).

**Packer build hangs at "Waiting for SSH/WinRM."**
The base image's security group / NSG / firewall doesn't permit the
build runner's source IP. Check the cloud-side network policy on the
ephemeral build instance Packer creates. For **Windows builds behind
a TLS-inspecting corp proxy**, also verify WinRM egress: from the
app container, `timeout 5 bash -c "</dev/tcp/<build-vm-ip>/5986"` —
if 5986 is blocked while 22 works, the proxy is eating WinRM; build
from an unproxied network or switch the template to a private-VNet
build (`virtual_network_name` / `virtual_network_subnet_name`).

**Windows build fails creating the temp Key Vault.**
Packer provisions a transient Key Vault for the WinRM certificate.
Grant the service principal Key Vault create rights in the build RG
and register the `Microsoft.KeyVault` resource provider on the
subscription (`az provider register --namespace Microsoft.KeyVault`).

**Build succeeds but storage upload fails with "no active backend"
or "Export skipped: no S3/Azure/GCS configured."**
The Packer build itself succeeded; only the export-to-hub step was
skipped. The cloud-native image is registered and deployable. To get
it onto the hub (and enable cross-cloud promote), do one of:
- Configure the missing storage prerequisite on `/storage` and click
  **Export VHD** on the image in its per-cloud Images tab. Same flow
  as the auto-export, just operator-triggered.
- Or re-run the build (slower, full Packer cycle).

**Promote to Azure fails with "VHD format unsupported."**
Azure's VM import wants a fixed-size VHD, not a dynamic one (and not
RAW). The dashboard's converter produces fixed-size by default; if
you've replaced it with a custom converter, double-check the output
format with `qemu-img info`.

**Azure browse/promote fails with `AuthorizationFailed` on
`Microsoft.Compute/galleries/images/read` or
`Microsoft.Compute/images/read`.**
The Shared Image Gallery lives in a resource group *outside* the one
the dashboard service principal was granted on, so it has no rights
there. `Reader` on that RG fixes browsing; promoting *into* the
gallery also needs managed-image and gallery-image-version writes, so
grant `Contributor` or the least-privilege `Dashboard Image Promoter`
custom role (read galleries/images + write managed images and gallery
image versions). The sandbox bootstrap automates the grant — re-run
with the gallery RG exported:

```bash
AZURE_IMAGE_GALLERY_RG=myGalleryRG AZURE_IMAGE_GALLERY_NAME=corpImageGallery \
  ./scripts/sandbox/Linux/setup-azure.sh
```

```powershell
$env:AZURE_IMAGE_GALLERY_RG = 'myGalleryRG'
$env:AZURE_IMAGE_GALLERY_NAME = 'corpImageGallery'
.\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
```

It creates the custom role (if missing) and assigns it to the SP
scoped to that RG; override with `AZURE_IMAGE_GALLERY_ROLE=Contributor`,
or set `AZURE_IMAGE_GALLERY_SUBSCRIPTION_ID` for a cross-subscription
gallery. On a non-sandbox install, grant the same role by hand with
`az role assignment create … --scope /subscriptions/<sub>/resourceGroups/<rg>`.
RBAC is eventually consistent — wait a minute or two and refresh the
dashboard's credentials after granting.

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

**Azure: deleting a Gallery image fails with `AuthorizationFailed`.**
On `/azure` → **Private Images**, Delete on a **Gallery** row removes
the whole image definition: every version first, then the definition,
because Azure won't delete a definition that still has versions. Delete
on a **Managed** row removes that one managed image from the resource
group it's listed in. The gallery delete needs
`Microsoft.Compute/galleries/images/versions/delete` **and**
`Microsoft.Compute/galleries/images/delete` on the gallery RG, which is
more than the `Reader` that browsing needs. `Contributor` covers both.
The `Dashboard Image Promoter` role gets both from the setup scripts, but
a role created before the definition-delete action was added only has
the version delete. On that older role, the versions are deleted and
then the definition refuses, which leaves an empty definition in the list.
Add the action to the existing role, then retry the delete:

```bash
az role definition update --role-definition "$(az role definition list --name 'Dashboard Image Promoter' --query '[0]' -o json | jq '.permissions[0].actions += ["Microsoft.Compute/galleries/images/delete"] | {Name: .roleName, Id: .name, IsCustom: true, Description: .description, Actions: .permissions[0].actions, NotActions: .permissions[0].notActions, AssignableScopes: .assignableScopes}')"
```
