#!/usr/bin/env bash
# OCI (Oracle Cloud Infrastructure) sandbox bootstrap for the VM Dashboard.
#
# OCI equivalent of the AWS / Azure / GCP sandbox isolation pattern:
#
#   • A dedicated compartment (dashboard-sandbox) under the tenancy root, so
#     every resource is grouped and easy to find/tear down.
#   • A VCN (10.98.0.0/16 — distinct from AWS 10.99/16) with:
#     - public-subnet  (10.98.1.0/24): Internet-Gateway route. The BT Jumpoint
#       lands here so it can phone home to PRA's relay.
#     - vm-subnet      (10.98.2.0/24): NAT-Gateway egress only, no public IPs —
#       user VMs land here, reachable only via the Jumpoint (sibling subnet).
#     - db-subnet      (10.98.3.0/24): private, for managed databases (Phase 4).
#   • Security list allowing intra-VCN traffic + SSH from the public subnet.
#   • A Vault + AES key + SSH-keypair secret (JSON {public_key, private_key})
#     the dashboard reads for every deploy (best-effort — see OCI_SKIP_VAULT).
#
# Operator auth: run this under whichever OCI CLI login you already use — an
# API-key profile (`oci setup config`) OR a browser/SSO session token
# (`oci session authenticate`). The script auto-detects the profile type and
# adds `--auth security_token` for session-token logins, so you never need a
# dedicated API user just to run it (parity with the AWS/Azure/GCP scripts).
#
# The dashboard does NOT reuse your operator identity. This script mints a
# dedicated IAM user (dashboard-sandbox-app) in a group with a compartment-
# scoped policy, generates an API key for it, and emits THAT key so the
# dashboard signs API calls as its own service identity. Set
# OCI_SKIP_DASHBOARD_USER=1 to instead reuse your operator API key (API-key
# operator login only — a session token has no long-lived key to hand off).
#
# Env overrides: OCI_PROFILE (default DEFAULT), OCI_COMPARTMENT_OCID (use an
# existing compartment instead of creating one), OCI_REGION, OCI_SKIP_VAULT=1,
# OCI_SKIP_DASHBOARD_USER=1.

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_supported_os
require_cmd oci
require_cmd jq
require_cmd ssh-keygen
require_cmd openssl

NAME="${SANDBOX_NAME_PREFIX}"
OCI_PROFILE="${OCI_PROFILE:-DEFAULT}"
OCI_CONFIG_FILE="${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}"
FREEFORM='{"managed-by":"dashboard-sandbox"}'

[[ -f "$OCI_CONFIG_FILE" ]] || \
  die "No OCI CLI config at $OCI_CONFIG_FILE. Run: oci session authenticate  (browser/SSO) or oci setup config  (API key)."

# ── Read a single key from the [$OCI_PROFILE] INI section ─────────────────────
# Everything from '[PROFILE]' to the next '[' header. Works for both an API-key
# profile (tenancy/user/fingerprint/region/key_file) and a session-token profile
# (tenancy/region/security_token_file/key_file, no user/fingerprint).
_profile_val() {
  awk -v prof="[$OCI_PROFILE]" -v key="$1" '
    $0==prof {inp=1; next}
    /^\[/    {inp=0}
    inp && $0 ~ "^"key"[[:space:]]*=" {
      sub("^"key"[[:space:]]*=[[:space:]]*", ""); print; exit
    }' "$OCI_CONFIG_FILE"
}

# ── Detect operator auth mode ─────────────────────────────────────────────────
# A browser/SSO login (`oci session authenticate`) writes a `security_token_file`
# and needs `--auth security_token` on every call; an API-key profile
# (`oci setup config`) has user/fingerprint/key_file and needs no auth flag.
if [[ -n "$(_profile_val security_token_file)" ]]; then
  AUTH_MODE="session"
else
  AUTH_MODE="apikey"
fi
AUTH_ARG=""
[[ "$AUTH_MODE" == "session" ]] && AUTH_ARG="--auth security_token"

ensure_logged_in "oci" "oci $AUTH_ARG iam region list --profile $OCI_PROFILE" \
  "Run: oci session authenticate  (browser/SSO) or oci setup config  (API key)."

# ── Read the operator credentials from the CLI config ─────────────────────────
TENANCY="${OCI_TENANCY_OCID:-$(_profile_val tenancy)}"
USER_OCID="$(_profile_val user)"
FINGERPRINT="$(_profile_val fingerprint)"
REGION="${OCI_REGION:-$(_profile_val region)}"
KEY_FILE="$(_profile_val key_file)"
PASSPHRASE="$(_profile_val pass_phrase || true)"
# Expand a leading ~ in key_file (oci setup writes an absolute path, but be safe).
KEY_FILE="${KEY_FILE/#\~/$HOME}"

# Tenancy + region are required in both modes.
[[ -n "$TENANCY" ]] || die "Could not read 'tenancy' from $OCI_CONFIG_FILE [$OCI_PROFILE]."
[[ -n "$REGION" ]]  || die "Could not read 'region' from $OCI_CONFIG_FILE [$OCI_PROFILE]."
# user/fingerprint/key_file exist only in an API-key profile — a session-token
# profile omits them (the dashboard user minted below supplies the real signer).
if [[ "$AUTH_MODE" == "apikey" ]]; then
  [[ -n "$USER_OCID" ]]   || die "Could not read 'user' from $OCI_CONFIG_FILE [$OCI_PROFILE]."
  [[ -n "$FINGERPRINT" ]] || die "Could not read 'fingerprint' from $OCI_CONFIG_FILE [$OCI_PROFILE]."
  [[ -f "$KEY_FILE" ]]    || die "API signing key file '$KEY_FILE' (key_file in [$OCI_PROFILE]) not found."
fi

OCI=(oci --profile "$OCI_PROFILE" --region "$REGION")
[[ "$AUTH_MODE" == "session" ]] && OCI+=(--auth security_token)

section "OCI sandbox in tenancy ${TENANCY:0:20}…, region $REGION"

# ── 1. Compartment ────────────────────────────────────────────────────────────
section "Compartment"
COMPARTMENT_NAME="$NAME"
if [[ -n "${OCI_COMPARTMENT_OCID:-}" ]]; then
  COMPARTMENT="$OCI_COMPARTMENT_OCID"
  ok "Using existing compartment $COMPARTMENT"
else
  COMPARTMENT="$("${OCI[@]}" iam compartment list --compartment-id "$TENANCY" --all \
    --query "data[?name=='$COMPARTMENT_NAME'].id | [0]" --raw-output 2>/dev/null || true)"
  if [[ -z "$COMPARTMENT" || "$COMPARTMENT" == "null" ]]; then
    COMPARTMENT="$("${OCI[@]}" iam compartment create \
      --compartment-id "$TENANCY" --name "$COMPARTMENT_NAME" \
      --description "VM Dashboard sandbox" --freeform-tags "$FREEFORM" \
      --wait-for-state ACTIVE --query 'data.id' --raw-output)"
    ok "Created compartment $COMPARTMENT_NAME"
  else
    ok "Reusing compartment $COMPARTMENT_NAME"
  fi
fi
state_write oci compartment "$COMPARTMENT"

# ── 1b. Dedicated dashboard IAM user + group + policy + API key ────────────────
# Mint a service identity for the dashboard instead of reusing the operator's
# credentials (parity with the AWS IAM user / Azure SP / GCP SA). The operator's
# login (API key OR session token) is used only to create it here; the dashboard
# then signs its own API calls with the API key we generate for this user.
# Skippable with OCI_SKIP_DASHBOARD_USER=1 (API-key operator login only).
DASHBOARD_USER_NAME=""
DASHBOARD_USER_OCID=""
DASHBOARD_FINGERPRINT=""
DASHBOARD_PRIVATE_KEY_PEM=""
if [[ "${OCI_SKIP_DASHBOARD_USER:-0}" != "1" ]]; then
  section "Dashboard IAM user"

  # IAM control-plane writes must target the tenancy home region.
  HOME_REGION="$("${OCI[@]}" iam region-subscription list \
    --query "data[?\"is-home-region\"]|[0].\"region-name\"" --raw-output 2>/dev/null || true)"
  [[ -n "$HOME_REGION" && "$HOME_REGION" != "null" ]] || HOME_REGION="$REGION"
  OCI_IAM=(oci --profile "$OCI_PROFILE" --region "$HOME_REGION")
  [[ "$AUTH_MODE" == "session" ]] && OCI_IAM+=(--auth security_token)

  # User (tenancy-root; reuse by name).
  DASHBOARD_USER_NAME="${NAME}-app"
  DASHBOARD_USER_OCID="$("${OCI_IAM[@]}" iam user list --compartment-id "$TENANCY" --all \
    --query "data[?name=='$DASHBOARD_USER_NAME']|[0].id" --raw-output 2>/dev/null || true)"
  if [[ -z "$DASHBOARD_USER_OCID" || "$DASHBOARD_USER_OCID" == "null" ]]; then
    DASHBOARD_USER_OCID="$("${OCI_IAM[@]}" iam user create --compartment-id "$TENANCY" \
      --name "$DASHBOARD_USER_NAME" --description "VM Dashboard service identity" \
      --freeform-tags "$FREEFORM" --query 'data.id' --raw-output)"
    ok "Created IAM user $DASHBOARD_USER_NAME"
  else
    ok "Reusing IAM user $DASHBOARD_USER_NAME"
  fi

  # Group (tenancy-root; reuse by name) + idempotent membership.
  DASHBOARD_GROUP_NAME="${NAME}-app-group"
  DASHBOARD_GROUP_OCID="$("${OCI_IAM[@]}" iam group list --compartment-id "$TENANCY" --all \
    --query "data[?name=='$DASHBOARD_GROUP_NAME']|[0].id" --raw-output 2>/dev/null || true)"
  if [[ -z "$DASHBOARD_GROUP_OCID" || "$DASHBOARD_GROUP_OCID" == "null" ]]; then
    DASHBOARD_GROUP_OCID="$("${OCI_IAM[@]}" iam group create --compartment-id "$TENANCY" \
      --name "$DASHBOARD_GROUP_NAME" --description "VM Dashboard service group" \
      --freeform-tags "$FREEFORM" --query 'data.id' --raw-output)"
    ok "Created IAM group $DASHBOARD_GROUP_NAME"
  else
    ok "Reusing IAM group $DASHBOARD_GROUP_NAME"
  fi
  _member="$("${OCI_IAM[@]}" iam group list-users --group-id "$DASHBOARD_GROUP_OCID" --all \
    --query "data[?id=='$DASHBOARD_USER_OCID']|[0].id" --raw-output 2>/dev/null || true)"
  if [[ -z "$_member" || "$_member" == "null" ]]; then
    retry 6 5 "${OCI_IAM[@]}" iam group add-user \
      --user-id "$DASHBOARD_USER_OCID" --group-id "$DASHBOARD_GROUP_OCID" >/dev/null
    ok "Added $DASHBOARD_USER_NAME to $DASHBOARD_GROUP_NAME"
  else
    ok "$DASHBOARD_USER_NAME already in $DASHBOARD_GROUP_NAME"
  fi

  # Policy at the tenancy root so it can reference the sandbox compartment by
  # name. Compartment-admin scope, confined to that one compartment. (The name
  # is a direct child of root; a nested compartment would need a parent:child
  # path here.)
  DASHBOARD_POLICY_NAME="${NAME}-app-policy"
  _comp_name="$("${OCI_IAM[@]}" iam compartment get --compartment-id "$COMPARTMENT" \
    --query 'data.name' --raw-output 2>/dev/null || true)"
  [[ -n "$_comp_name" && "$_comp_name" != "null" ]] || _comp_name="$COMPARTMENT_NAME"
  DASHBOARD_POLICY_OCID="$("${OCI_IAM[@]}" iam policy list --compartment-id "$TENANCY" --all \
    --query "data[?name=='$DASHBOARD_POLICY_NAME']|[0].id" --raw-output 2>/dev/null || true)"
  if [[ -z "$DASHBOARD_POLICY_OCID" || "$DASHBOARD_POLICY_OCID" == "null" ]]; then
    _stmt="Allow group $DASHBOARD_GROUP_NAME to manage all-resources in compartment $_comp_name"
    DASHBOARD_POLICY_OCID="$("${OCI_IAM[@]}" iam policy create --compartment-id "$TENANCY" \
      --name "$DASHBOARD_POLICY_NAME" --description "VM Dashboard sandbox access" \
      --statements "[\"$_stmt\"]" --freeform-tags "$FREEFORM" --query 'data.id' --raw-output)"
    ok "Created IAM policy $DASHBOARD_POLICY_NAME (manage all-resources in compartment $_comp_name)"
  else
    ok "Reusing IAM policy $DASHBOARD_POLICY_NAME"
  fi

  # API key: we generate the keypair locally, so we always hold the private half
  # (unlike AWS's server-minted secret). Reuse the cached key if it still matches
  # a live fingerprint; otherwise mint a fresh one (pruning the oldest if the
  # per-user 3-key cap is hit).
  DASHBOARD_FINGERPRINT="$(state_read oci dashboard_fingerprint)"
  DASHBOARD_PRIVATE_KEY_PEM="$(state_read oci dashboard_private_key)"
  # Parse the full JSON with jq (OCI's --raw-output formatting of an array
  # projection is ambiguous across CLI versions).
  _keys_json="$("${OCI_IAM[@]}" iam user api-key list --user-id "$DASHBOARD_USER_OCID" 2>/dev/null || echo '{"data":[]}')"
  _have_fp="$(echo "$_keys_json" | jq -r --arg fp "$DASHBOARD_FINGERPRINT" '[.data[].fingerprint] | index($fp) // empty' 2>/dev/null || true)"
  if [[ -n "$DASHBOARD_FINGERPRINT" && -n "$DASHBOARD_PRIVATE_KEY_PEM" && -n "$_have_fp" ]]; then
    ok "Reusing cached API key for $DASHBOARD_USER_NAME (fingerprint ${DASHBOARD_FINGERPRINT:0:11}…)"
  else
    if [[ "$(echo "$_keys_json" | jq '.data | length' 2>/dev/null || echo 0)" -ge 3 ]]; then
      _oldest_fp="$(echo "$_keys_json" | jq -r '.data | sort_by(."time-created") | .[0].fingerprint // empty' 2>/dev/null || true)"
      [[ -n "$_oldest_fp" ]] && \
        "${OCI_IAM[@]}" iam user api-key delete --user-id "$DASHBOARD_USER_OCID" \
          --fingerprint "$_oldest_fp" --force >/dev/null 2>&1 || true
    fi
    _keydir="$(mktemp -d)"
    openssl genrsa -out "$_keydir/api_key.pem" 2048 >/dev/null 2>&1
    openssl rsa -pubout -in "$_keydir/api_key.pem" -out "$_keydir/api_key_public.pem" >/dev/null 2>&1
    DASHBOARD_PRIVATE_KEY_PEM="$(cat "$_keydir/api_key.pem")"
    DASHBOARD_FINGERPRINT="$(retry 6 5 "${OCI_IAM[@]}" iam user api-key upload \
      --user-id "$DASHBOARD_USER_OCID" --key-file "$_keydir/api_key_public.pem" \
      --query 'data.fingerprint' --raw-output)"
    rm -rf "$_keydir"
    state_write oci dashboard_private_key "$DASHBOARD_PRIVATE_KEY_PEM"
    state_write oci dashboard_fingerprint "$DASHBOARD_FINGERPRINT"
    chmod 0600 "$(state_dir oci)/dashboard_private_key" 2>/dev/null || true
    chmod 0700 "$(state_dir oci)" 2>/dev/null || true
    ok "Minted API key for $DASHBOARD_USER_NAME (fingerprint ${DASHBOARD_FINGERPRINT:0:11}…)"
  fi
  state_write oci dashboard_user   "$DASHBOARD_USER_OCID"
  state_write oci dashboard_group  "$DASHBOARD_GROUP_OCID"
  state_write oci dashboard_policy "$DASHBOARD_POLICY_OCID"
elif [[ "$AUTH_MODE" == "session" ]]; then
  die "OCI_SKIP_DASHBOARD_USER=1 needs an API-key operator login — a session token has no long-lived key to hand the dashboard. Re-run without the flag, or use 'oci setup config'."
else
  warn "OCI_SKIP_DASHBOARD_USER=1 — the dashboard will reuse your operator API key ($USER_OCID)."
fi

# Helper: find a resource id by display-name in the compartment (AVAILABLE only).
_find() {  # $1=oci-subcommand (space-sep), $2=display-name
  local sub="$1" name="$2"
  # shellcheck disable=SC2086
  "${OCI[@]}" $sub list --compartment-id "$COMPARTMENT" --all \
    --query "data[?\"display-name\"=='$name' && \"lifecycle-state\"!='TERMINATED'].id | [0]" \
    --raw-output 2>/dev/null || true
}

# ── 2. VCN ────────────────────────────────────────────────────────────────────
section "VCN + gateways"
VCN_NAME="${NAME}-vcn"
VCN="$(_find "network vcn" "$VCN_NAME")"
if [[ -z "$VCN" || "$VCN" == "null" ]]; then
  VCN="$("${OCI[@]}" network vcn create --compartment-id "$COMPARTMENT" \
    --cidr-blocks '["10.98.0.0/16"]' --display-name "$VCN_NAME" \
    --dns-label dashsandbox --freeform-tags "$FREEFORM" \
    --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
  ok "Created VCN $VCN_NAME (10.98.0.0/16)"
else
  ok "Reusing VCN $VCN_NAME"
fi
state_write oci vcn "$VCN"

# Internet Gateway (public subnet egress).
IGW_NAME="${NAME}-igw"
IGW="$(_find "network internet-gateway" "$IGW_NAME")"
if [[ -z "$IGW" || "$IGW" == "null" ]]; then
  IGW="$("${OCI[@]}" network internet-gateway create --compartment-id "$COMPARTMENT" \
    --vcn-id "$VCN" --is-enabled true --display-name "$IGW_NAME" \
    --freeform-tags "$FREEFORM" --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
  ok "Created Internet Gateway $IGW_NAME"
else
  ok "Reusing Internet Gateway $IGW_NAME"
fi

# NAT Gateway (private VM-subnet egress).
NAT_NAME="${NAME}-nat"
NAT="$(_find "network nat-gateway" "$NAT_NAME")"
if [[ -z "$NAT" || "$NAT" == "null" ]]; then
  NAT="$("${OCI[@]}" network nat-gateway create --compartment-id "$COMPARTMENT" \
    --vcn-id "$VCN" --display-name "$NAT_NAME" \
    --freeform-tags "$FREEFORM" --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
  ok "Created NAT Gateway $NAT_NAME"
else
  ok "Reusing NAT Gateway $NAT_NAME"
fi

# ── 3. Route tables ───────────────────────────────────────────────────────────
section "Route tables"
PUB_RT_NAME="${NAME}-public-rt"
PUB_RT="$(_find "network route-table" "$PUB_RT_NAME")"
if [[ -z "$PUB_RT" || "$PUB_RT" == "null" ]]; then
  PUB_RT="$("${OCI[@]}" network route-table create --compartment-id "$COMPARTMENT" \
    --vcn-id "$VCN" --display-name "$PUB_RT_NAME" --freeform-tags "$FREEFORM" \
    --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" \
    --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
  ok "Created public route table (→ IGW)"
else
  ok "Reusing public route table"
fi

PRIV_RT_NAME="${NAME}-private-rt"
PRIV_RT="$(_find "network route-table" "$PRIV_RT_NAME")"
if [[ -z "$PRIV_RT" || "$PRIV_RT" == "null" ]]; then
  PRIV_RT="$("${OCI[@]}" network route-table create --compartment-id "$COMPARTMENT" \
    --vcn-id "$VCN" --display-name "$PRIV_RT_NAME" --freeform-tags "$FREEFORM" \
    --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$NAT\"}]" \
    --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
  ok "Created private route table (→ NAT)"
else
  ok "Reusing private route table"
fi

# ── 4. Security list (intra-VCN + SSH from the public subnet) ─────────────────
section "Security list"
SL_NAME="${NAME}-sl"
SL="$(_find "network security-list" "$SL_NAME")"
if [[ -z "$SL" || "$SL" == "null" ]]; then
  INGRESS='[
    {"source":"10.98.0.0/16","protocol":"all","isStateless":false},
    {"source":"10.98.1.0/24","protocol":"6","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":22,"max":22}}}
  ]'
  EGRESS='[{"destination":"0.0.0.0/0","protocol":"all","isStateless":false}]'
  SL="$("${OCI[@]}" network security-list create --compartment-id "$COMPARTMENT" \
    --vcn-id "$VCN" --display-name "$SL_NAME" --freeform-tags "$FREEFORM" \
    --ingress-security-rules "$INGRESS" --egress-security-rules "$EGRESS" \
    --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
  ok "Created security list (intra-VCN + SSH from public subnet)"
else
  ok "Reusing security list"
fi

# ── 5. Subnets ────────────────────────────────────────────────────────────────
section "Subnets"
_create_subnet() {  # $1=name $2=cidr $3=route-table $4=prohibit-public-ip $5=dns-label
  local name="$1" cidr="$2" rt="$3" prohibit="$4" dns="$5" id
  id="$(_find "network subnet" "$name")"
  if [[ -z "$id" || "$id" == "null" ]]; then
    id="$("${OCI[@]}" network subnet create --compartment-id "$COMPARTMENT" \
      --vcn-id "$VCN" --cidr-block "$cidr" --display-name "$name" --dns-label "$dns" \
      --route-table-id "$rt" --security-list-ids "[\"$SL\"]" \
      --prohibit-public-ip-on-vnic "$prohibit" --freeform-tags "$FREEFORM" \
      --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
    ok "Created subnet $name ($cidr)"
  else
    ok "Reusing subnet $name"
  fi
  printf '%s' "$id"
}
PUB_SUBNET="$(_create_subnet "${NAME}-public-subnet"  10.98.1.0/24 "$PUB_RT"  false pub)"
VM_SUBNET="$(_create_subnet  "${NAME}-vm-subnet"      10.98.2.0/24 "$PRIV_RT" true  vm)"
DB_SUBNET="$(_create_subnet  "${NAME}-db-subnet"      10.98.3.0/24 "$PRIV_RT" true  db)"
state_write oci vm_subnet "$VM_SUBNET"

# ── 6. Vault + key + SSH-keypair secret (best-effort) ─────────────────────────
# OCI has no lightweight Secrets Manager; the SSH key lives in a KMS Vault
# secret. Vault creation is slow (~1-2 min) and a vault can't be hard-deleted
# (scheduled deletion only), so this step is guarded: set OCI_SKIP_VAULT=1 to
# skip it (the keypair is then cached locally and you create the secret by hand).
SSH_SECRET_OCID=""
VAULT_OCID=""
if [[ "${OCI_SKIP_VAULT:-0}" != "1" ]]; then
  section "Vault + SSH keypair secret"
  VAULT_NAME="${NAME}-vault"
  VAULT_OCID="$("${OCI[@]}" kms management vault list --compartment-id "$COMPARTMENT" --all \
    --query "data[?\"display-name\"=='$VAULT_NAME' && \"lifecycle-state\"=='ACTIVE'].id | [0]" --raw-output 2>/dev/null || true)"
  if [[ -z "$VAULT_OCID" || "$VAULT_OCID" == "null" ]]; then
    info "Creating Vault $VAULT_NAME (this can take a minute or two)…"
    VAULT_OCID="$("${OCI[@]}" kms management vault create --compartment-id "$COMPARTMENT" \
      --display-name "$VAULT_NAME" --vault-type DEFAULT --freeform-tags "$FREEFORM" \
      --wait-for-state ACTIVE --query 'data.id' --raw-output 2>/dev/null || true)"
  fi
  if [[ -n "$VAULT_OCID" && "$VAULT_OCID" != "null" ]]; then
    ok "Vault $VAULT_NAME ready"
    MGMT_EP="$("${OCI[@]}" kms management vault get --vault-id "$VAULT_OCID" \
      --query 'data."management-endpoint"' --raw-output)"
    KEY_OCID="$("${OCI[@]}" kms management key list --compartment-id "$COMPARTMENT" \
      --endpoint "$MGMT_EP" --all \
      --query "data[?\"display-name\"=='${NAME}-key' && \"lifecycle-state\"=='ENABLED'].id | [0]" --raw-output 2>/dev/null || true)"
    if [[ -z "$KEY_OCID" || "$KEY_OCID" == "null" ]]; then
      KEY_OCID="$("${OCI[@]}" kms management key create --compartment-id "$COMPARTMENT" \
        --endpoint "$MGMT_EP" --display-name "${NAME}-key" \
        --key-shape '{"algorithm":"AES","length":32}' --freeform-tags "$FREEFORM" \
        --wait-for-state ENABLED --query 'data.id' --raw-output)"
    fi
    ok "KMS key ready"

    SSH_SECRET_NAME="dashboard-sandbox-ssh-keypair"
    SSH_SECRET_OCID="$("${OCI[@]}" vault secret list --compartment-id "$COMPARTMENT" --all \
      --query "data[?\"secret-name\"=='$SSH_SECRET_NAME' && \"lifecycle-state\"=='ACTIVE'].id | [0]" --raw-output 2>/dev/null || true)"
    if [[ -z "$SSH_SECRET_OCID" || "$SSH_SECRET_OCID" == "null" ]]; then
      TMPDIR="$(mktemp -d)"; trap 'rm -rf "$TMPDIR"' EXIT
      ssh-keygen -t rsa -b 4096 -N "" -C "dashboard-sandbox" -f "$TMPDIR/key" >/dev/null
      B64="$(jq -n --arg pub "$(cat "$TMPDIR/key.pub")" --arg priv "$(cat "$TMPDIR/key")" \
        '{public_key:$pub, private_key:$priv}' | base64 | tr -d '\n')"
      SSH_SECRET_OCID="$("${OCI[@]}" vault secret create-base64 --compartment-id "$COMPARTMENT" \
        --secret-name "$SSH_SECRET_NAME" --vault-id "$VAULT_OCID" --key-id "$KEY_OCID" \
        --secret-content-content "$B64" --freeform-tags "$FREEFORM" \
        --query 'data.id' --raw-output)"
      ok "Created SSH keypair secret $SSH_SECRET_NAME"
    else
      ok "Reusing SSH keypair secret $SSH_SECRET_NAME"
    fi
    state_write oci vault "$VAULT_OCID"
    state_write oci ssh_secret "$SSH_SECRET_OCID"
  else
    warn "Vault creation failed/unavailable — skipping the SSH secret (set oci_ssh_key_secret manually, or re-run)."
  fi
else
  warn "OCI_SKIP_VAULT=1 — no Vault/secret created. Deployed VMs will be keyless unless you set oci_ssh_key_secret."
fi

# ── 7. Print config to paste into /setup + write config.json twin ─────────────
# By default the dashboard uses the dedicated IAM user minted above; with
# OCI_SKIP_DASHBOARD_USER=1 it falls back to the operator's own API key.
if [[ "${OCI_SKIP_DASHBOARD_USER:-0}" != "1" ]]; then
  CFG_USER_OCID="$DASHBOARD_USER_OCID"
  CFG_FINGERPRINT="$DASHBOARD_FINGERPRINT"
  CFG_PRIVATE_KEY_PEM="$DASHBOARD_PRIVATE_KEY_PEM"
  CFG_PASSPHRASE=""                       # generated key has no passphrase
  CFG_IDENTITY="dedicated IAM user $DASHBOARD_USER_NAME (its own API key)"
else
  CFG_USER_OCID="$USER_OCID"
  CFG_FINGERPRINT="$FINGERPRINT"
  CFG_PRIVATE_KEY_PEM="$(cat "$KEY_FILE")"
  CFG_PASSPHRASE="$PASSPHRASE"
  CFG_IDENTITY="your operator API key ($USER_OCID)"
fi
_cfg=(
  "oci_tenancy_ocid=$TENANCY"
  "oci_user_ocid=$CFG_USER_OCID"
  "oci_fingerprint=$CFG_FINGERPRINT"
  "oci_region=$REGION"
  "oci_compartment_ocid=$COMPARTMENT"
  "oci_vcn_ocid=$VCN"
  "oci_default_subnet_ocid=$VM_SUBNET                       # User VMs land here (NAT egress, no public IP)"
  "oci_private_key=…                                         # PEM injected into config.json below"
)
[[ -n "$CFG_PASSPHRASE" ]] && _cfg+=("oci_private_key_passphrase=$CFG_PASSPHRASE")
if [[ -n "$SSH_SECRET_OCID" ]]; then
  _cfg+=("oci_ssh_key_secret=$SSH_SECRET_OCID                # Vault secret: JSON {public_key, private_key}")
  _cfg+=("oci_vault_ocid=$VAULT_OCID")
else
  _cfg+=("oci_ssh_key_secret=…   # Create a Vault secret (JSON {public_key,private_key}) and paste its OCID")
fi
print_dashboard_config "OCI sandbox configuration" "${_cfg[@]}"
write_config_json oci "${_cfg[@]}"   # machine-readable twin for onboard-sandbox.sh

# Inject the real private-key PEM into config.json (kept off the printed block).
if command -v jq >/dev/null 2>&1; then
  _oci_cfg="$(state_dir oci)/config.json"
  jq -c --arg pk "$CFG_PRIVATE_KEY_PEM" '.oci_private_key = $pk' "$_oci_cfg" > "$_oci_cfg.tmp" \
    && mv "$_oci_cfg.tmp" "$_oci_cfg"
fi

cat <<EOF
Sandbox topology summary

  Compartment $COMPARTMENT_NAME
  VCN ${NAME}-vcn (10.98.0.0/16)
    ├─ ${NAME}-public-subnet (10.98.1.0/24) → Internet Gateway  [Jumpoint]
    ├─ ${NAME}-vm-subnet      (10.98.2.0/24) → NAT Gateway       [user VMs]
    └─ ${NAME}-db-subnet      (10.98.3.0/24) → NAT (private)     [managed DBs]

The dashboard signs API calls as $CFG_IDENTITY.
Deploy VMs into the vm-subnet; the free tier defaults to VM.Standard.E2.1.Micro.

To tear it down:
  ./scripts/sandbox/Linux/rollback.sh --cloud oci

EOF
