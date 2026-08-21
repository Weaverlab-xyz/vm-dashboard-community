#!/usr/bin/env bash
# Workload Credentials — the Azure app-registration prerequisites.
#
# `terraform/workload_credentials` deliberately does not create these. The integration
# app's client secret is the root of the whole Azure path, and creating it in Terraform
# would write it into state. So it happens here with the az CLI, and the script hands you
# the four values the module needs.
#
# What it creates
# ---------------
#   1. An **integration** app registration + service principal + client secret. Workload
#      Credentials authenticates as this.
#   2. A **target** app registration + service principal. Credentials are minted *onto*
#      this, and it is what the dashboard effectively acts as.
#   3. Ownership: the integration SP becomes an owner of the target app. That is what
#      lets it add and remove passwords with **no tenant-wide Graph permission** —
#      materially less than the Application.ReadWrite.All alternative, which would grant
#      it over every app in the directory. The Portal cannot do this: its owners picker
#      accepts users but not service principals.
#   4. The target SP's Azure RBAC, **copied from a reference service principal** rather
#      than hardcoded.
#
# Why the RBAC is copied rather than listed
# -----------------------------------------
# The target app needs whatever the dashboard actually uses: Contributor on the resource
# group, Storage Blob Data Contributor on the storage account (a data-plane role that
# Contributor does NOT include), Key Vault secret access, and — depending on which
# features are enabled — subscription-scoped Reader, Cost Management Reader, AcrPull and
# User Access Administrator on the resource group.
#
# Writing that list here would make it a second definition of grants that
# `scripts/sandbox/Linux/setup-azure.sh` already applies, and the two would drift the
# first time either changed. That is the failure this repository hits most often.
#
# So point --reference-sp at the service principal the dashboard uses today and every
# role assignment it holds is replicated onto the target. Parity by construction, and it
# answers "what does the target need?" with "exactly what your working one has."
#
# Worth stating plainly because an earlier version of the module's own comments had it
# wrong: the grants do NOT come with it if you point setup-azure.sh at the target app.
# That script creates and grants its *own* service principal, so a separate app
# registration starts with nothing and every call 403s.
#
# Idempotent: re-running reuses existing apps and skips assignments already present.
# There is no PowerShell twin yet — on Windows, run this from WSL, which is where `az`
# lives for this repository anyway.
set -Eeuo pipefail

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../sandbox/Linux/lib/common.sh
source "$_HERE/../sandbox/Linux/lib/common.sh"

PREFIX="vm-dashboard-wlc"
REFERENCE_SP=""
KEY_VAULT=""
SECRET_YEARS="1"
DRY_RUN=0
COPY_RBAC=1
STATE_DIR="${WLC_STATE_DIR:-$HOME/.dashboard-wlc}"

usage() {
  cat >&2 <<'USAGE'
Usage: setup-azure-apps.sh --reference-sp <appId|objectId> [options]

  --reference-sp ID   The service principal whose role assignments to copy onto the
                      target app. Use the one the dashboard authenticates with today
                      (azure_client_id in its settings). Required unless --no-rbac.
  --prefix NAME       Name prefix for the two app registrations.
                      Default: vm-dashboard-wlc
  --key-vault NAME    Also copy the reference SP's Key Vault secret permissions to the
                      target. Only needed for access-policy vaults; RBAC vaults are
                      already covered by the role-assignment copy.
  --secret-years N    Lifetime of the integration app's client secret. Default: 1
  --no-rbac           Create the apps and ownership only. Grant the target yourself.
  --dry-run           Print what would happen and change nothing.
  -h, --help          This message.
USAGE
  exit "${1:-1}"
}

while (( $# )); do
  case "$1" in
    --reference-sp) REFERENCE_SP="${2:?}"; shift 2 ;;
    --prefix)       PREFIX="${2:?}";       shift 2 ;;
    --key-vault)    KEY_VAULT="${2:?}";    shift 2 ;;
    --secret-years) SECRET_YEARS="${2:?}"; shift 2 ;;
    --no-rbac)      COPY_RBAC=0;           shift ;;
    --dry-run)      DRY_RUN=1;             shift ;;
    -h|--help)      usage 0 ;;
    *) err "unknown argument: $1"; usage ;;
  esac
done

# ── Preflight ─────────────────────────────────────────────────────────────────

command -v az >/dev/null || die "az CLI not found. This is the one hard dependency."
command -v jq >/dev/null || die "jq not found (apt install jq)."

ACCOUNT_JSON="$(az account show -o json 2>/dev/null)" \
  || die "not signed in. Run: az login"
SUBSCRIPTION_ID="$(jq -r '.id' <<<"$ACCOUNT_JSON")"
TENANT_ID="$(jq -r '.tenantId' <<<"$ACCOUNT_JSON")"
ok "Subscription $SUBSCRIPTION_ID in tenant $TENANT_ID"

(( COPY_RBAC )) && [[ -z "$REFERENCE_SP" ]] && {
  err "--reference-sp is required unless you pass --no-rbac."
  err "It is the service principal the dashboard uses today — its azure_client_id."
  exit 1
}

INTEGRATION_APP_NAME="${PREFIX}-integration"
TARGET_APP_NAME="${PREFIX}-target"

# ── App registrations ─────────────────────────────────────────────────────────
#
# `az ad app create` returns both ids and they are easy to confuse: `.appId` is the
# Application (client) ID, `.id` is the **Object ID**. Workload Credentials wants the
# Object ID for the target app, and the provider calls that out explicitly because it is
# the most common mistake in Azure setup — the resulting failure does not name the field.

app_object_id() {
  az ad app list --display-name "$1" --query "[0].id" -o tsv 2>/dev/null | head -1
}

ensure_app() {
  local name="$1" existing
  existing="$(app_object_id "$name")"
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    ok "Reusing app registration $name ($existing)" >&2
    printf '%s' "$existing"
    return 0
  fi
  if (( DRY_RUN )); then
    info "would create app registration $name" >&2
    printf '%s' "00000000-0000-0000-0000-000000000000"
    return 0
  fi
  az ad app create --display-name "$name" --query id -o tsv
}

INTEGRATION_OBJECT_ID="$(ensure_app "$INTEGRATION_APP_NAME")"
TARGET_OBJECT_ID="$(ensure_app "$TARGET_APP_NAME")"
ok "Integration app object id: $INTEGRATION_OBJECT_ID"
ok "Target app object id:      $TARGET_OBJECT_ID"

app_client_id() {
  az ad app show --id "$1" --query appId -o tsv 2>/dev/null
}

if (( DRY_RUN )); then
  INTEGRATION_CLIENT_ID="<integration-client-id>"
else
  INTEGRATION_CLIENT_ID="$(app_client_id "$INTEGRATION_OBJECT_ID")"
fi

# ── Service principals ────────────────────────────────────────────────────────
#
# An app registration is not a principal. Both need an SP: the integration's to be made
# an owner, the target's to hold role assignments.
#
# A freshly created SP lags in Entra — `az ad sp show` 404s until it replicates — so
# every lookup is retried. `az ad sp list` would instead return an empty string with exit
# 0, which no retry could catch.

ensure_sp() {
  local app_client_id="$1" existing
  existing="$(az ad sp show --id "$app_client_id" --query id -o tsv 2>/dev/null || true)"
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    printf '%s' "$existing"
    return 0
  fi
  if (( DRY_RUN )); then
    printf '%s' "00000000-0000-0000-0000-000000000000"
    return 0
  fi
  az ad sp create --id "$app_client_id" >/dev/null 2>&1 || true
  retry 8 5 az ad sp show --id "$app_client_id" --query id -o tsv
}

if (( DRY_RUN )); then
  INTEGRATION_SP_OBJECT_ID="<integration-sp-object-id>"
  TARGET_SP_OBJECT_ID="<target-sp-object-id>"
  info "would create service principals for both apps"
else
  TARGET_CLIENT_ID="$(app_client_id "$TARGET_OBJECT_ID")"
  INTEGRATION_SP_OBJECT_ID="$(ensure_sp "$INTEGRATION_CLIENT_ID")"
  TARGET_SP_OBJECT_ID="$(ensure_sp "$TARGET_CLIENT_ID")"
  ok "Integration SP object id: $INTEGRATION_SP_OBJECT_ID"
  ok "Target SP object id:      $TARGET_SP_OBJECT_ID"
fi

# ── Ownership ─────────────────────────────────────────────────────────────────
#
# The least-privilege path. An owner can add and remove credentials on the app it owns
# with no directory-wide permission at all.

if (( DRY_RUN )); then
  info "would add the integration SP as an owner of $TARGET_APP_NAME"
elif az ad app owner list --id "$TARGET_OBJECT_ID" --query "[].id" -o tsv 2>/dev/null \
     | grep -qx "$INTEGRATION_SP_OBJECT_ID"; then
  ok "Integration SP already owns the target app"
else
  retry 6 5 az ad app owner add --id "$TARGET_OBJECT_ID" \
    --owner-object-id "$INTEGRATION_SP_OBJECT_ID"
  ok "Integration SP is now an owner of the target app"
fi

# ── The client secret ─────────────────────────────────────────────────────────
#
# Written to a 0600 file rather than stdout: it belongs in TF_VAR_… or the dashboard's
# config, not in a terminal scrollback or a CI log. Same treatment setup-azure.sh gives
# the sandbox SP's credentials.

SECRET_FILE="$STATE_DIR/azure-integration.json"
if (( DRY_RUN )); then
  info "would reset the integration app's client secret into $SECRET_FILE"
elif [[ -s "$SECRET_FILE" ]] && jq -e '.password' "$SECRET_FILE" >/dev/null 2>&1; then
  ok "Reusing the integration client secret at $SECRET_FILE"
else
  mkdir -p "$STATE_DIR"
  # `credential reset` rather than `credential add`: a re-run should leave exactly one
  # usable secret rather than accumulating them, and the app registration caps how many
  # it will hold.
  az ad app credential reset --id "$INTEGRATION_CLIENT_ID" \
    --display-name "workload-credentials" --years "$SECRET_YEARS" -o json > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
  ok "Integration client secret written to $SECRET_FILE (mode 600)"
fi

# ── Copy the reference SP's role assignments ───────────────────────────────────

if (( ! COPY_RBAC )); then
  warn "Skipping the RBAC copy (--no-rbac). The target app has NO permissions yet;"
  warn "grant it before enabling the Azure dynamic tier or every call will 403."
else
  REF_OBJECT_ID="$(retry 6 5 az ad sp show --id "$REFERENCE_SP" --query id -o tsv)" \
    || die "could not resolve --reference-sp '$REFERENCE_SP' to a service principal"
  info "Copying role assignments from reference SP $REF_OBJECT_ID"

  ASSIGNMENTS="$(az role assignment list --assignee "$REF_OBJECT_ID" --all -o json)"
  COUNT="$(jq 'length' <<<"$ASSIGNMENTS")"
  (( COUNT )) || warn "the reference SP holds NO role assignments — is it the right one?"

  # roleDefinitionId rather than roleDefinitionName: a custom role's name is not
  # guaranteed to resolve, and this deployment uses at least one ("Dashboard Image
  # Promoter" for an external image gallery).
  while IFS=$'\t' read -r role_id role_name scope; do
    [[ -z "$role_id" ]] && continue
    if (( DRY_RUN )); then
      info "would grant '$role_name' at $scope"
      continue
    fi
    if az role assignment list --assignee "$TARGET_SP_OBJECT_ID" \
         --scope "$scope" --query "[?roleDefinitionId=='$role_id'] | length(@)" \
         -o tsv 2>/dev/null | grep -qx "1"; then
      ok "already granted: $role_name at $scope"
      continue
    fi
    # PrincipalNotFound here is Entra replication, not a permissions problem.
    if retry 8 5 az role assignment create \
        --assignee-object-id "$TARGET_SP_OBJECT_ID" \
        --assignee-principal-type ServicePrincipal \
        --role "$role_id" --scope "$scope" >/dev/null; then
      ok "granted: $role_name at $scope"
    else
      warn "could NOT grant '$role_name' at $scope"
      warn "  Creating an assignment needs Microsoft.Authorization/roleAssignments/write"
      warn "  at that scope — Owner or User Access Administrator. Grant it by hand:"
      warn "    az role assignment create --assignee-object-id $TARGET_SP_OBJECT_ID \\"
      warn "      --assignee-principal-type ServicePrincipal \\"
      warn "      --role '$role_name' --scope '$scope'"
    fi
  done < <(jq -r '.[] | [.roleDefinitionId, .roleDefinitionName, .scope] | @tsv' \
             <<<"$ASSIGNMENTS")
fi

# ── Key Vault access policy ───────────────────────────────────────────────────
#
# Only for access-policy vaults. An RBAC vault's permissions are role assignments and
# were already handled above — which is the distinction that catches people out, since
# Contributor grants neither.

if [[ -n "$KEY_VAULT" ]]; then
  if (( DRY_RUN )); then
    info "would grant the target SP get/list/set/delete on Key Vault $KEY_VAULT"
  else
    KV_RBAC="$(az keyvault show -n "$KEY_VAULT" \
      --query "properties.enableRbacAuthorization" -o tsv 2>/dev/null || echo "unknown")"
    if [[ "$KV_RBAC" == "true" ]]; then
      ok "Key Vault $KEY_VAULT is RBAC-mode; its access came with the role copy above"
    else
      retry 8 5 az keyvault set-policy -n "$KEY_VAULT" \
        --object-id "$TARGET_SP_OBJECT_ID" \
        --secret-permissions get list set delete >/dev/null
      ok "Granted the target SP get/list/set/delete on Key Vault $KEY_VAULT"
    fi
  fi
fi

# ── What to do with it ────────────────────────────────────────────────────────

ok "Azure prerequisites ready"
cat <<EOF

Feed these to terraform/workload_credentials:

  export TF_VAR_azure_integration_client_secret="\$(jq -r .password $SECRET_FILE)"

  terraform -chdir=terraform/workload_credentials apply \\
    -var enable_azure=true \\
    -var azure_tenant_id='$TENANT_ID' \\
    -var azure_integration_client_id='$INTEGRATION_CLIENT_ID' \\
    -var azure_integration_sp_object_id='$INTEGRATION_SP_OBJECT_ID' \\
    -var azure_target_application_object_id='$TARGET_OBJECT_ID'

The module will also set the app ownership itself unless you pass
-var azure_manage_target_ownership=false. This script has already done it, so either is
fine — the operation is idempotent.

Then in the dashboard: Settings -> Preview features -> Workload Credentials -> Configure,
and set azure_subscription_id to $SUBSCRIPTION_ID if it is not already. The lease does
NOT carry a subscription, so a minted credential with none configured is refused rather
than used.
EOF
