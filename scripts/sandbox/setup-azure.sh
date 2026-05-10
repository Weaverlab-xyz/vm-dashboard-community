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

require_wsl
require_cmd az
require_cmd jq
require_cmd ssh-keygen

LOCATION="${AZURE_LOCATION:-centralus}"
NAME="${SANDBOX_NAME_PREFIX}"
RG="${NAME}-rg"
VNET="${NAME}-vnet"
ACI_SUBNET="aci-subnet"
VM_SUBNET="vm-subnet"
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

ACI_SUBNET_ID="$(az network vnet subnet show -g "$RG" --vnet-name "$VNET" -n "$ACI_SUBNET" --query id -o tsv)"
VM_SUBNET_ID="$(az network vnet subnet show  -g "$RG" --vnet-name "$VNET" -n "$VM_SUBNET"  --query id -o tsv)"
state_write azure aci_subnet_id "$ACI_SUBNET_ID"
state_write azure vm_subnet_id  "$VM_SUBNET_ID"

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
if ! az keyvault show -g "$RG" -n "$KV_NAME" >/dev/null 2>&1; then
  az keyvault create -g "$RG" -n "$KV_NAME" -l "$LOCATION" \
    --enable-rbac-authorization false --tags "$TAGS" >/dev/null
  ok "Created Key Vault $KV_NAME"
else
  ok "Reusing Key Vault $KV_NAME"
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
  SP_OBJECT_ID="$(az ad sp list --display-name "$SP_NAME" --query '[0].id' -o tsv)"
  az keyvault set-policy -n "$KV_NAME" --object-id "$SP_OBJECT_ID" \
    --secret-permissions get list >/dev/null
  ok "Granted SP read on Key Vault $KV_NAME"
fi
SP_APP_ID="$(jq -r '.appId'    "$SP_JSON_PATH")"
SP_PASSWORD="$(jq -r '.password' "$SP_JSON_PATH")"

# ── 7. Print config to paste into /setup ─────────────────────────────────────
print_dashboard_config "Azure sandbox configuration" \
  "azure_subscription_id=$SUBSCRIPTION_ID" \
  "azure_tenant_id=$TENANT_ID" \
  "azure_client_id=$SP_APP_ID" \
  "azure_client_secret=$SP_PASSWORD" \
  "azure_resource_group=$RG" \
  "azure_location=$LOCATION" \
  "azure_vnet_resource_group=$RG" \
  "azure_aci_resource_group=$RG" \
  "azure_aci_subnet_id=$ACI_SUBNET_ID                      # ACI lands here, has internet egress" \
  "azure_default_subnet_id=$VM_SUBNET_ID                   # VMs land here, NSG-restricted to VNet" \
  "azure_aci_storage_account=$SA_NAME                      # /jpt persistent volume" \
  "azure_aci_storage_account_rg=$RG" \
  "azure_aci_file_share=jpt" \
  "azure_key_vault_url=$KV_URL" \
  "azure_ssh_keypair_secret_name=$SSH_SECRET               # JSON {public_key, private_key}" \
  "" \
  "# BeyondTrust deploy key — set in /setup or /secrets:" \
  "azure_aci_docker_deploy_key=…"

cat <<EOF
Sandbox topology summary

  VNet $VNET (10.99.0.0/16)
    ├─ aci-subnet (10.99.1.0/24, delegated to ACI) → internet egress  [Jumpoint]
    └─ vm-subnet  (10.99.2.0/24, NSG-restricted)   → VirtualNetwork only  [user VMs]

Service principal credentials cached at:
  $SP_JSON_PATH  (mode 600)

To tear it down:
  ./scripts/sandbox/rollback.sh --cloud azure

EOF
