#!/usr/bin/env bash
# Azure sandbox bootstrap for the VM Dashboard.
#
# Creates:
#   • Resource group
#   • VNet with two subnets:
#     - aci-subnet  (delegated to Microsoft.ContainerInstance) → has internet
#       egress so the BT Jumpoint ACI container can reach PRA's relay.
#     - vm-subnet   (NSG denies outbound to Internet, allows VirtualNetwork)
#       so deployed VMs can only egress within the VNet — i.e. to the ACI
#       Jumpoint, never directly to the internet.
#   • Key Vault with an SSH keypair stored as JSON {public_key, private_key}
#   • Service principal with Contributor on the RG
#
# Prints config block for /setup.

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_supported_os
require_cmd az
require_cmd jq
require_cmd ssh-keygen

LOCATION="${AZURE_LOCATION:-centralus}"
NAME="${SANDBOX_NAME_PREFIX}"
RG="${NAME}-rg"
VNET="${NAME}-vnet"
ACI_SUBNET="aci-subnet"
VM_SUBNET="vm-subnet"
K8S_SUBNET="k8s-subnet"
NSG="${NAME}-vm-nsg"

ensure_logged_in "az" "az account show" "Run: az login"

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"
section "Azure sandbox in subscription $SUBSCRIPTION_ID, location $LOCATION"

TAGS="${SANDBOX_TAG_KEY}=${SANDBOX_TAG_VALUE}"

# ── 1. Resource Group ─────────────────────────────────────────────────────────
section "Resource group"
az group create -n "$RG" -l "$LOCATION" --tags "$TAGS" >/dev/null
ok "Resource group $RG"
state_write azure rg "$RG"

# ── 2. VNet + subnets ─────────────────────────────────────────────────────────
section "VNet + subnets"
az network vnet create -g "$RG" -n "$VNET" \
  --address-prefix 10.99.0.0/16 --tags "$TAGS" >/dev/null
ok "VNet $VNET (10.99.0.0/16)"

# ACI subnet — delegated to Microsoft.ContainerInstance for ACI VNet injection.
az network vnet subnet create -g "$RG" --vnet-name "$VNET" -n "$ACI_SUBNET" \
  --address-prefix 10.99.1.0/24 \
  --delegations Microsoft.ContainerInstance/containerGroups >/dev/null
ok "ACI subnet $ACI_SUBNET (10.99.1.0/24, delegated)"

# VM subnet — outbound restricted by NSG (created next).
az network vnet subnet create -g "$RG" --vnet-name "$VNET" -n "$VM_SUBNET" \
  --address-prefix 10.99.2.0/24 >/dev/null
ok "VM subnet $VM_SUBNET (10.99.2.0/24)"

# Dedicated subnet for managed Kubernetes (AKS) — separate from the ACI and VM
# subnets above.
az network vnet subnet create -g "$RG" --vnet-name "$VNET" -n "$K8S_SUBNET" \
  --address-prefix 10.99.3.0/24 >/dev/null
ok "K8s subnet $K8S_SUBNET (10.99.3.0/24)"

ACI_SUBNET_ID="$(az network vnet subnet show -g "$RG" --vnet-name "$VNET" -n "$ACI_SUBNET" --query id -o tsv)"
VM_SUBNET_ID="$(az network vnet subnet show  -g "$RG" --vnet-name "$VNET" -n "$VM_SUBNET"  --query id -o tsv)"
K8S_SUBNET_ID="$(az network vnet subnet show -g "$RG" --vnet-name "$VNET" -n "$K8S_SUBNET" --query id -o tsv)"
state_write azure aci_subnet_id "$ACI_SUBNET_ID"
state_write azure vm_subnet_id  "$VM_SUBNET_ID"
state_write azure k8s_subnet_id "$K8S_SUBNET_ID"

# ── 3. NSG: deny VM internet egress, allow VNet ──────────────────────────────
section "NSG (block VM internet egress)"
az network nsg create -g "$RG" -n "$NSG" --tags "$TAGS" >/dev/null

# Priority lower number wins; explicit allow VirtualNetwork before deny Internet.
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-vnet-out \
  --priority 100 --direction Outbound \
  --access Allow --protocol "*" \
  --source-address-prefix VirtualNetwork --source-port-range "*" \
  --destination-address-prefix VirtualNetwork --destination-port-range "*" >/dev/null
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n deny-internet-out \
  --priority 200 --direction Outbound \
  --access Deny --protocol "*" \
  --source-address-prefix "*" --source-port-range "*" \
  --destination-address-prefix Internet --destination-port-range "*" >/dev/null
# Inbound: allow VNet (so ACI Jumpoint can reach), deny everything else.
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-vnet-in \
  --priority 100 --direction Inbound \
  --access Allow --protocol "*" \
  --source-address-prefix VirtualNetwork --source-port-range "*" \
  --destination-address-prefix VirtualNetwork --destination-port-range "*" >/dev/null
ok "NSG $NSG: VM subnet egress restricted to VirtualNetwork"

az network vnet subnet update -g "$RG" --vnet-name "$VNET" -n "$VM_SUBNET" \
  --network-security-group "$NSG" >/dev/null
ok "Attached NSG to $VM_SUBNET"
state_write azure vm_nsg "$NSG"

# ── 4. Storage account + file share for ACI /jpt persistence (optional) ──────
section "Storage account (ACI /jpt persistence)"
SA_NAME="$(printf '%s%s' "${NAME//-/}" "$(printf '%s' "$SUBSCRIPTION_ID" | tr -d '-' | head -c8)" \
  | tr 'A-Z' 'a-z' | head -c24)"
if ! az storage account show -g "$RG" -n "$SA_NAME" >/dev/null 2>&1; then
  az storage account create -g "$RG" -n "$SA_NAME" -l "$LOCATION" \
    --sku Standard_LRS --tags "$TAGS" >/dev/null
fi
SA_KEY="$(az storage account keys list -g "$RG" -n "$SA_NAME" --query '[0].value' -o tsv)"
az storage share-rm create -g "$RG" --storage-account "$SA_NAME" -n "jpt" --quota 1 >/dev/null 2>&1 || true
ok "Storage account $SA_NAME (file share: jpt)"
state_write azure sa_name "$SA_NAME"

# ── 5. Key Vault + SSH keypair JSON ──────────────────────────────────────────
section "Key Vault + SSH keypair"
# KV names are globally unique; postfix with subscription hash for collision-safety.
KV_NAME="$(printf '%s-kv-%s' "$NAME" "$(printf '%s' "$SUBSCRIPTION_ID" | tr -d '-' | head -c6)")"
KV_NAME="${KV_NAME:0:24}"
if az keyvault show -g "$RG" -n "$KV_NAME" >/dev/null 2>&1; then
  ok "Reusing Key Vault $KV_NAME"
elif az keyvault show-deleted -n "$KV_NAME" >/dev/null 2>&1; then
  # KV names are globally reserved during soft-delete retention; recover
  # rather than fail or wait out the 90-day window.
  az keyvault recover -n "$KV_NAME" -l "$LOCATION" >/dev/null
  ok "Recovered soft-deleted Key Vault $KV_NAME"
else
  az keyvault create -g "$RG" -n "$KV_NAME" -l "$LOCATION" \
    --enable-rbac-authorization false --tags "$TAGS" >/dev/null
  ok "Created Key Vault $KV_NAME"
fi
KV_URL="https://${KV_NAME}.vault.azure.net/"
state_write azure kv_name "$KV_NAME"

# Generate keypair if the secret doesn't exist yet.
SSH_SECRET="azureVM-ssh-keypair"
if ! az keyvault secret show --vault-name "$KV_NAME" -n "$SSH_SECRET" >/dev/null 2>&1; then
  TMPDIR="$(mktemp -d)"; trap 'rm -rf "$TMPDIR"' EXIT
  ssh-keygen -t rsa -b 4096 -N "" -C "dashboard-sandbox" -f "$TMPDIR/key" >/dev/null
  PUB="$(cat "$TMPDIR/key.pub")"
  PRIV="$(cat "$TMPDIR/key")"
  jq -n --arg pub "$PUB" --arg priv "$PRIV" \
    '{public_key:$pub, private_key:$priv}' > "$TMPDIR/keypair.json"
  az keyvault secret set --vault-name "$KV_NAME" -n "$SSH_SECRET" \
    --file "$TMPDIR/keypair.json" >/dev/null
  ok "Stored keypair as KV secret $SSH_SECRET"
else
  ok "Reusing existing keypair secret $SSH_SECRET"
fi

# ── 6. Service principal with Contributor on the RG ──────────────────────────
section "Service principal"
SP_NAME="${NAME}-sp"
SP_JSON_PATH="$(state_dir azure)/sp.json"
if [[ -s "$SP_JSON_PATH" ]] && jq -e '.appId' "$SP_JSON_PATH" >/dev/null 2>&1; then
  ok "Reusing service principal from $SP_JSON_PATH"
else
  RG_SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG"
  az ad sp create-for-rbac -n "$SP_NAME" \
    --role Contributor --scopes "$RG_SCOPE" \
    --years 1 -o json > "$SP_JSON_PATH"
  chmod 600 "$SP_JSON_PATH"
  ok "Created SP $SP_NAME (creds at $SP_JSON_PATH, mode 600)"

  # Grant the SP read access to the Key Vault for runtime SSH key fetches.
  # A just-created SP lags in AAD: `az ad sp show` 404s until it replicates, so
  # retry the lookup (by appId from the create output — `az ad sp list` would
  # instead return an empty string with exit 0, which no retry could catch).
  SP_OBJECT_ID="$(retry 8 5 az ad sp show --id "$(jq -r '.appId' "$SP_JSON_PATH")" --query id -o tsv)"
  retry 8 5 az keyvault set-policy -n "$KV_NAME" --object-id "$SP_OBJECT_ID" \
    --secret-permissions get list >/dev/null
  ok "Granted SP read on Key Vault $KV_NAME"
fi
SP_APP_ID="$(jq -r '.appId'    "$SP_JSON_PATH")"
SP_PASSWORD="$(jq -r '.password' "$SP_JSON_PATH")"

# ── 6b. Image-hub container + promote-runner Azure plumbing ──────────────────
# Provisions the prerequisites the dashboard's automated cross-cloud image
# promote runner needs (see docs/image-management.md, runners/promote/README.md):
#
#   • A `hub` blob container on the storage account that doubles as both the
#     image-registry hub and the staging container the promote-runner ACI
#     writes converted VHDs to (under promote-staging/).
#   • Storage Blob Data Contributor on the storage account for the SP — the
#     SP already has Contributor on the RG (control plane), but the runner
#     does AAD-authenticated *data plane* blob writes which need this
#     dedicated role.
#   • Microsoft.ContainerInstance resource provider registered so ACI works
#     in this subscription without a first-use 5-minute provisioning wait.
section "Image-hub container + promote-runner Azure plumbing"

az storage container-rm create -g "$RG" --storage-account "$SA_NAME" -n "hub" \
  >/dev/null 2>&1 || true
ok "Blob container 'hub' on storage account $SA_NAME"

SP_OBJECT_ID="$(retry 8 5 az ad sp show --id "$SP_APP_ID" --query id -o tsv)"
SA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$SA_NAME"
if az role assignment list --assignee "$SP_OBJECT_ID" --scope "$SA_SCOPE" \
     --role "Storage Blob Data Contributor" --query '[0].id' -o tsv 2>/dev/null | grep -q .; then
  ok "SP already has Storage Blob Data Contributor on $SA_NAME"
else
  # ARM RBAC is eventually consistent w.r.t. AAD: a freshly created principal
  # isn't visible yet, so role assignment create fails "PrincipalNotFound".
  # Retry until ARM sees it — the assignment is idempotent.
  retry 8 5 az role assignment create --assignee-object-id "$SP_OBJECT_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Contributor" --scope "$SA_SCOPE" >/dev/null
  ok "Granted SP Storage Blob Data Contributor on $SA_NAME"
fi

# Register the ACI provider if not already (no-op if registered). The
# promote runner launches as an ACI container group.
ACI_STATE="$(az provider show --namespace Microsoft.ContainerInstance \
  --query registrationState -o tsv 2>/dev/null || echo NotRegistered)"
if [[ "$ACI_STATE" != "Registered" ]]; then
  az provider register --namespace Microsoft.ContainerInstance --wait >/dev/null
  ok "Registered Microsoft.ContainerInstance provider"
else
  ok "Microsoft.ContainerInstance already registered"
fi

# ── 6c. Optional: grant the SP access to an EXTERNAL Shared Image Gallery RG ──
# The dashboard reads private images from a Compute Gallery and, for promote,
# writes managed images / gallery image versions. That gallery usually lives in
# a corp-owned RG *outside* this sandbox, where the SP has no rights by default
# (see web_dashboard/services/azure_service.py: list_private_images +
# create_image_from_blob). Opt in by exporting AZURE_IMAGE_GALLERY_RG; leave it
# unset to skip this block entirely.
#
#   AZURE_IMAGE_GALLERY_RG               external RG holding the gallery (triggers this step)
#   AZURE_IMAGE_GALLERY_NAME             Compute Gallery name (optional; emits config key)
#   AZURE_IMAGE_GALLERY_ROLE             role to grant (default: custom "Dashboard Image Promoter")
#   AZURE_IMAGE_GALLERY_SUBSCRIPTION_ID  gallery's subscription (default: current)
#
# Defaults to a least-privilege custom role scoped to read galleries/images +
# write managed images and gallery image versions — created here if missing.
# Set AZURE_IMAGE_GALLERY_ROLE=Contributor (or any existing role) to use that
# instead and skip custom-role creation. Whoever runs this needs role-assignment
# rights on the gallery RG (Owner / User Access Administrator) — a plain
# Contributor cannot grant roles.
GALLERY_RG="${AZURE_IMAGE_GALLERY_RG:-}"
if [[ -n "$GALLERY_RG" ]]; then
  section "Optional: external image-gallery RG access ($GALLERY_RG)"
  GALLERY_PROMOTER_ROLE="Dashboard Image Promoter"
  GALLERY_SUB="${AZURE_IMAGE_GALLERY_SUBSCRIPTION_ID:-$SUBSCRIPTION_ID}"
  GALLERY_ROLE="${AZURE_IMAGE_GALLERY_ROLE:-$GALLERY_PROMOTER_ROLE}"
  GALLERY_SCOPE="/subscriptions/$GALLERY_SUB/resourceGroups/$GALLERY_RG"

  # Create the custom role definition if we're using the default and it's absent.
  # AssignableScopes is the whole subscription so re-runs targeting other RGs in
  # the same sub reuse it; the *assignment* below is still scoped to just the
  # gallery RG, so effective access stays RG-local.
  if [[ "$GALLERY_ROLE" == "$GALLERY_PROMOTER_ROLE" ]] \
     && ! az role definition list --name "$GALLERY_ROLE" --query '[0].roleName' -o tsv 2>/dev/null | grep -q .; then
    az role definition create --role-definition "$(cat <<JSON
{
  "Name": "$GALLERY_PROMOTER_ROLE",
  "Description": "Read galleries/images and publish managed images + gallery image versions for the VM Dashboard promote flow.",
  "Actions": [
    "Microsoft.Compute/galleries/read",
    "Microsoft.Compute/galleries/images/read",
    "Microsoft.Compute/galleries/images/write",
    "Microsoft.Compute/galleries/images/versions/read",
    "Microsoft.Compute/galleries/images/versions/write",
    "Microsoft.Compute/galleries/images/versions/delete",
    "Microsoft.Compute/images/read",
    "Microsoft.Compute/images/write",
    "Microsoft.Compute/images/delete",
    "Microsoft.Storage/storageAccounts/read"
  ],
  "AssignableScopes": ["/subscriptions/$GALLERY_SUB"]
}
JSON
)" >/dev/null
    ok "Created custom role '$GALLERY_PROMOTER_ROLE' (assignable in subscription $GALLERY_SUB)"
  fi

  # Grant the SP the role on the gallery RG. retry absorbs both the custom-role
  # definition's propagation delay and ARM's PrincipalNotFound race; the
  # assignment is idempotent, so the existence check + retry are both safe.
  if az role assignment list --assignee "$SP_OBJECT_ID" --scope "$GALLERY_SCOPE" \
       --role "$GALLERY_ROLE" --query '[0].id' -o tsv 2>/dev/null | grep -q .; then
    ok "SP already has '$GALLERY_ROLE' on $GALLERY_RG"
  else
    retry 8 5 az role assignment create --assignee-object-id "$SP_OBJECT_ID" \
      --assignee-principal-type ServicePrincipal \
      --role "$GALLERY_ROLE" --scope "$GALLERY_SCOPE" >/dev/null
    ok "Granted SP '$GALLERY_ROLE' on $GALLERY_RG (subscription $GALLERY_SUB)"
  fi
  # Recorded so rollback can drop this assignment (the corp gallery RG itself is
  # never deleted by rollback — only the assignment we added here).
  state_write azure image_gallery_rg   "$GALLERY_RG"
  state_write azure image_gallery_sub  "$GALLERY_SUB"
  state_write azure image_gallery_role "$GALLERY_ROLE"
fi

# ── 7. Print config to paste into /setup ─────────────────────────────────────
_cfg=(
  "azure_subscription_id=$SUBSCRIPTION_ID"
  "azure_tenant_id=$TENANT_ID"
  "azure_client_id=$SP_APP_ID"
  "azure_client_secret=$SP_PASSWORD"
  "azure_resource_group=$RG"
  "azure_location=$LOCATION"
  "azure_vnet_resource_group=$RG"
  "azure_aci_resource_group=$RG"
  "azure_aci_subnet_id=$ACI_SUBNET_ID                      # ACI lands here, has internet egress"
  "azure_default_subnet_id=$VM_SUBNET_ID                   # VMs land here, NSG-restricted to VNet"
  "azure_aci_storage_account=$SA_NAME                      # /jpt persistent volume"
  "azure_aci_storage_account_rg=$RG"
  "azure_aci_file_share=jpt"
  "azure_key_vault_url=$KV_URL"
  "azure_ssh_keypair_secret_name=$SSH_SECRET               # JSON {public_key, private_key}"
  ""
  "# Image-registry hub + automated cross-cloud promote:"
  "storage_azure_account=$SA_NAME                          # Image hub + promote staging"
  "storage_azure_container=hub                              # Container for hub artefacts"
  "storage_active_backend=azure_blob                        # Active asset backend"
  "storage_hub_backend=azure_blob                           # Image hub (defaults to active if unset)"
  "promote_runner_image=chrweav/dashboard-promote-runner:latest   # Public multi-arch image; override to your ACR for a private/air-gapped registry"
  "promote_runner_azure_resource_group=$RG                  # ACI lands here"
  "promote_runner_azure_location=$LOCATION"
  "promote_runner_azure_subnet_id=$ACI_SUBNET_ID            # Reuses the Jumpoint ACI subnet"
  "promote_runner_azure_staging_account=$SA_NAME            # Same account as hub by default"
  "promote_runner_azure_staging_container=hub"
  "promote_runner_azure_target_resource_group=$RG           # Resulting managed image lands here"
  ""
  "# BeyondTrust deploy key — set in /setup or /secrets:"
  "azure_aci_docker_deploy_key=…"
)

# Surface the external gallery in the pasteable config when opted in. (Comment
# lines must not contain '=' — write_config_json would mis-parse them as keys.)
if [[ -n "${AZURE_IMAGE_GALLERY_RG:-}" ]]; then
  _cfg+=(
    ""
    "# External Shared Image Gallery (SP granted access above):"
    "azure_gallery_resource_group=$AZURE_IMAGE_GALLERY_RG"
  )
  if [[ -n "${AZURE_IMAGE_GALLERY_NAME:-}" ]]; then
    _cfg+=("azure_shared_image_gallery=$AZURE_IMAGE_GALLERY_NAME              # Compute Gallery name")
  fi
  _cfg+=("# Tip: point promote_runner_azure_target_resource_group at $AZURE_IMAGE_GALLERY_RG to land promoted images in the gallery RG")
fi

print_dashboard_config "Azure sandbox configuration" "${_cfg[@]}"
write_config_json azure "${_cfg[@]}"   # machine-readable twin for onboard-sandbox.sh

cat <<EOF
Sandbox topology summary

  VNet $VNET (10.99.0.0/16)
    ├─ aci-subnet (10.99.1.0/24, delegated to ACI) → internet egress  [Jumpoint]
    └─ vm-subnet  (10.99.2.0/24, NSG-restricted)   → VirtualNetwork only  [user VMs]

Service principal credentials cached at:
  $SP_JSON_PATH  (mode 600)

To tear it down:
  ./scripts/sandbox/Linux/rollback.sh --cloud azure

EOF
