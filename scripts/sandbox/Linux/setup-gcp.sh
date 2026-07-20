#!/usr/bin/env bash
# GCP sandbox bootstrap for the VM Dashboard.
#
# GCP equivalent of the AWS / Azure sandbox isolation pattern:
#
#   • Custom VPC with two subnets:
#     - jumpoint-subnet: VMs land here with --no-address (no public IP) but
#       can still egress to the internet via a Cloud NAT gateway attached
#       to a Cloud Router. This is where the BT Jumpoint COS-on-GCE VM
#       lives so it can phone home to PRA's relay.
#     - vm-subnet:       NO Cloud NAT mapping. VMs deployed here have no
#       public IP and no NAT path → they cannot reach the internet at all.
#       Only routable via the VPC's internal IP space, so the Jumpoint
#       (sibling subnet) is the only reachable outbound proxy for SSH.
#
#   • Firewall rules:
#     - allow-internal: any-protocol within VPC
#     - allow-ssh-from-jumpoint: TCP 22 from jumpoint-subnet → vm-subnet
#     - block-egress-vm: explicit egress deny on vm-subnet (belt-and-suspenders;
#       Cloud NAT absence already prevents internet, but the rule makes it
#       observable and audit-friendly).
#
#   • Service account with the minimum roles needed for the dashboard's
#     deploy/destroy/image flows.
#
#   • Secret Manager: SSH keypair JSON.

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_supported_os
require_cmd gcloud
require_cmd jq
require_cmd ssh-keygen

NAME="${SANDBOX_NAME_PREFIX}"
PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${GCP_REGION:-us-central1}"
ZONE="${GCP_ZONE:-${REGION}-a}"

[[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || \
  die "No GCP project set. Run: gcloud config set project YOUR-PROJECT  (or export GCP_PROJECT_ID=…)"

ensure_logged_in "gcloud" "gcloud auth print-access-token --quiet" \
  "Run: gcloud auth login && gcloud auth application-default login"

section "GCP sandbox in project $PROJECT_ID, region $REGION ($ZONE)"

# Apply the sandbox label everywhere we can (GCP uses labels, not tags).
LABELS="${SANDBOX_TAG_KEY//-/_}=${SANDBOX_TAG_VALUE//-/_}"

VPC="${NAME}-vpc"
JP_SUBNET="${NAME}-jumpoint-subnet"
VM_SUBNET="${NAME}-vm-subnet"
K8S_SUBNET="${NAME}-k8s-subnet"
ROUTER="${NAME}-router"
NAT="${NAME}-nat"

# ── 1. Enable required APIs ───────────────────────────────────────────────────
section "Enable APIs"
# run.googleapis.com is needed for the dashboard's automated image promote
# (the runner launches as a Cloud Run Job in the target project).
# cloudbuild.googleapis.com powers image-export-to-VHD: registering a built
# image as a promotable hub artefact runs the Daisy gce_vm_image_export
# workflow as a Cloud Build job.
# container.googleapis.com is the Kubernetes Engine API — GKE provisioning
# (google_container_cluster / node pools) fails SERVICE_DISABLED without it.
# gkehub/connectgateway/gkeconnect power GKE Entra federation (Workforce Identity
# + Connect Gateway; see docs/integrations/entra-k8s-federation.md) — pre-enabling
# them here makes the dashboard's Enable-federation step a fast no-op instead of a
# cold API enable.
# cloudresourcemanager.googleapis.com backs the project-level get/setIamPolicy the
# federation's gateway-IAM grant uses (and the project-number lookup for the Connect
# Gateway URL). It is NOT enabled by default on every project — without it those calls
# fail with a "403 Forbidden … :getIamPolicy" that is really a SERVICE_DISABLED.
# bigquery.googleapis.com powers the Cloud Costs page: GCP has no cost API, so the
# dashboard queries the Cloud Billing export table in BigQuery (see cost_service.py).
for api in compute.googleapis.com secretmanager.googleapis.com iam.googleapis.com run.googleapis.com cloudbuild.googleapis.com container.googleapis.com \
           gkehub.googleapis.com connectgateway.googleapis.com gkeconnect.googleapis.com cloudresourcemanager.googleapis.com bigquery.googleapis.com; do
  gcloud services enable "$api" --project "$PROJECT_ID" --quiet
done
ok "Enabled compute, secretmanager, iam, run, cloudbuild, container, gkehub, connectgateway, gkeconnect, cloudresourcemanager, bigquery"

# ── 2. VPC + subnets ─────────────────────────────────────────────────────────
section "VPC + subnets"
if ! gcloud compute networks describe "$VPC" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud compute networks create "$VPC" --project "$PROJECT_ID" \
    --subnet-mode=custom --bgp-routing-mode=regional --quiet >/dev/null
  ok "Created VPC $VPC (custom mode)"
else
  ok "Reusing VPC $VPC"
fi

if ! gcloud compute networks subnets describe "$JP_SUBNET" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
  gcloud compute networks subnets create "$JP_SUBNET" \
    --project "$PROJECT_ID" --network "$VPC" --region "$REGION" \
    --range 10.99.1.0/24 --quiet >/dev/null
  ok "Created jumpoint subnet $JP_SUBNET (10.99.1.0/24)"
else
  ok "Reusing jumpoint subnet $JP_SUBNET"
fi

if ! gcloud compute networks subnets describe "$VM_SUBNET" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
  gcloud compute networks subnets create "$VM_SUBNET" \
    --project "$PROJECT_ID" --network "$VPC" --region "$REGION" \
    --range 10.99.2.0/24 --quiet >/dev/null
  ok "Created VM subnet $VM_SUBNET (10.99.2.0/24)"
else
  ok "Reusing VM subnet $VM_SUBNET"
fi

# Dedicated subnet for managed Kubernetes (GKE) — separate from the jumpoint and
# VM subnets above. Gets Cloud NAT egress (below) so a CO-LOCATED GKE cluster's
# nodes can pull images / reach the Entitle SaaS. The gke-pods / gke-services
# secondary ranges are the VPC-native pod & service ranges a co-located cluster
# uses (carved from the free 10.99.128.0/17 block so they don't collide with the
# /24 subnets or the PSA range). See docs — GKE co-location for Cloud SQL reach.
if ! gcloud compute networks subnets describe "$K8S_SUBNET" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
  gcloud compute networks subnets create "$K8S_SUBNET" \
    --project "$PROJECT_ID" --network "$VPC" --region "$REGION" \
    --range 10.99.3.0/24 \
    --secondary-range gke-pods=10.99.128.0/18,gke-services=10.99.192.0/20 \
    --quiet >/dev/null
  ok "Created K8s subnet $K8S_SUBNET (10.99.3.0/24; pods 10.99.128.0/18, services 10.99.192.0/20)"
elif ! gcloud compute networks subnets describe "$K8S_SUBNET" --project "$PROJECT_ID" --region "$REGION" \
       --format 'value(secondaryIpRanges[].rangeName)' 2>/dev/null | grep -q 'gke-pods'; then
  # Existing sandbox created before co-location — add the secondary ranges idempotently.
  gcloud compute networks subnets update "$K8S_SUBNET" \
    --project "$PROJECT_ID" --region "$REGION" \
    --add-secondary-ranges gke-pods=10.99.128.0/18,gke-services=10.99.192.0/20 \
    --quiet >/dev/null 2>&1 \
    && ok "Added GKE secondary ranges to $K8S_SUBNET (pods 10.99.128.0/18, services 10.99.192.0/20)" \
    || ok "Could not add GKE secondary ranges to $K8S_SUBNET (check perms)"
else
  ok "Reusing K8s subnet $K8S_SUBNET (GKE secondary ranges present)"
fi

state_write gcp vpc "$VPC"
state_write gcp jp_subnet "$JP_SUBNET"
state_write gcp vm_subnet "$VM_SUBNET"
state_write gcp k8s_subnet "$K8S_SUBNET"

# ── 3. Cloud Router + Cloud NAT (jumpoint + k8s subnets) ─────────────────────
section "Cloud Router + Cloud NAT (jumpoint + k8s subnets)"
if ! gcloud compute routers describe "$ROUTER" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
  gcloud compute routers create "$ROUTER" \
    --project "$PROJECT_ID" --network "$VPC" --region "$REGION" --quiet >/dev/null
  ok "Created router $ROUTER"
else
  ok "Reusing router $ROUTER"
fi

# NAT gateway with explicit subnet listing. jumpoint-subnet gets internet, and so
# does the k8s-subnet (node PRIMARY range) so a CO-LOCATED GKE cluster can pull
# images / reach the Entitle SaaS. Pods egress via SNAT to the node IP (ip-masq),
# so only the node primary range needs NAT. vm-subnet stays OFF NAT (isolation).
NAT_RANGES="$JP_SUBNET,$K8S_SUBNET"
if ! gcloud compute routers nats describe "$NAT" \
      --project "$PROJECT_ID" --router "$ROUTER" --router-region "$REGION" >/dev/null 2>&1; then
  gcloud compute routers nats create "$NAT" \
    --project "$PROJECT_ID" --router "$ROUTER" --router-region "$REGION" \
    --nat-custom-subnet-ip-ranges "$NAT_RANGES" \
    --auto-allocate-nat-external-ips --quiet >/dev/null
  ok "Created NAT $NAT (NAT'd subnets: jumpoint + k8s)"
else
  # Ensure the k8s-subnet is NAT'd on an existing sandbox (update replaces the list).
  gcloud compute routers nats update "$NAT" \
    --project "$PROJECT_ID" --router "$ROUTER" --router-region "$REGION" \
    --nat-custom-subnet-ip-ranges "$NAT_RANGES" --quiet >/dev/null 2>&1 || true
  ok "Reusing NAT $NAT (ensured jumpoint + k8s ranges)"
fi
state_write gcp router "$ROUTER"
state_write gcp nat    "$NAT"

# ── 4. Firewall rules ────────────────────────────────────────────────────────
section "Firewall rules"
NETWORK_TAG_JP="bt-jumpoint"        # tag the dashboard's COS Jumpoint VM
NETWORK_TAG_VM="${NAME}-vm"         # tag deployed user VMs (advisory; dashboard
                                    # doesn't auto-tag, but firewall scopes by tag).
NETWORK_TAG_K8S="${NAME}-k8s"       # tag CO-LOCATED GKE nodes (drives the k8s→DB
                                    # egress rule below; nodes reach VMs via allow-internal).

# Allow internal communication anywhere in the VPC.
gcloud compute firewall-rules create "${NAME}-allow-internal" \
  --project "$PROJECT_ID" --network "$VPC" \
  --direction INGRESS --priority 65534 \
  --allow all --source-ranges 10.99.0.0/16 \
  --quiet >/dev/null 2>&1 || true

# Allow SSH from Jumpoint → user VMs.
gcloud compute firewall-rules create "${NAME}-allow-ssh-from-jumpoint" \
  --project "$PROJECT_ID" --network "$VPC" \
  --direction INGRESS --priority 1000 \
  --action ALLOW --rules tcp:22 \
  --source-tags "$NETWORK_TAG_JP" --target-tags "$NETWORK_TAG_VM" \
  --quiet >/dev/null 2>&1 || true

# Belt-and-suspenders: explicit deny on egress from VM-tagged hosts to anything
# outside the VPC. (Cloud NAT absence already prevents internet, but this
# makes the intent obvious to auditors and survives someone mis-attaching a
# NAT mapping later.)
gcloud compute firewall-rules create "${NAME}-deny-vm-egress" \
  --project "$PROJECT_ID" --network "$VPC" \
  --direction EGRESS --priority 1000 \
  --action DENY --rules all \
  --target-tags "$NETWORK_TAG_VM" \
  --destination-ranges 0.0.0.0/0 \
  --quiet >/dev/null 2>&1 || true
# …but allow them to reach back into the VPC (so SSH replies work).
gcloud compute firewall-rules create "${NAME}-allow-vm-egress-vpc" \
  --project "$PROJECT_ID" --network "$VPC" \
  --direction EGRESS --priority 999 \
  --action ALLOW --rules all \
  --target-tags "$NETWORK_TAG_VM" \
  --destination-ranges 10.99.0.0/16 \
  --quiet >/dev/null 2>&1 || true

ok "Firewall rules: allow-internal, allow-ssh-from-jumpoint, deny-vm-egress, allow-vm-egress-vpc"

# ── 4b. Private Services Access + Cloud SQL reachability (managed databases) ──
# The cloud-database feature provisions a PRIVATE Cloud SQL Postgres instance
# (ipv4_enabled=false) reached ONLY through the BT PRA protocol tunnel on the
# jumpoint. Private-IP Cloud SQL needs a reserved IP range + a VPC peering with
# the servicenetworking producer (the GCP analog of the AWS private DB subnet
# group). The instance's private IP lands in this peered range — OUTSIDE
# 10.99.0.0/16 — so the existing deny-vm-egress rule already prevents user VMs
# from reaching it; only the jumpoint (no egress deny) can. We add an explicit
# egress ALLOW for the jumpoint to make that intent auditable.
section "Private Services Access + Cloud SQL (managed databases)"
gcloud services enable servicenetworking.googleapis.com sqladmin.googleapis.com \
  --project "$PROJECT_ID" --quiet >/dev/null 2>&1 || true

PSA_RANGE="${NAME}-psa-range"
if ! gcloud compute addresses describe "$PSA_RANGE" --global --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud compute addresses create "$PSA_RANGE" \
    --global --purpose VPC_PEERING --prefix-length 20 \
    --network "$VPC" --project "$PROJECT_ID" --quiet >/dev/null \
    && ok "Allocated private-services-access range $PSA_RANGE (/20)" \
    || ok "PSA range $PSA_RANGE not allocated (check perms)"
else
  ok "Reusing private-services-access range $PSA_RANGE"
fi

# Connect (or update) the servicenetworking peering on the VPC. Idempotent:
# 'connect' fails if it already exists, so fall back to 'update --force'.
gcloud services vpc-peerings connect \
  --service servicenetworking.googleapis.com \
  --ranges "$PSA_RANGE" --network "$VPC" --project "$PROJECT_ID" --quiet >/dev/null 2>&1 \
  || gcloud services vpc-peerings update \
       --service servicenetworking.googleapis.com \
       --ranges "$PSA_RANGE" --network "$VPC" --project "$PROJECT_ID" --force --quiet >/dev/null 2>&1 \
  || true
ok "servicenetworking peering on $VPC (Cloud SQL private IP path)"

# Explicit, auditable egress ALLOW: jumpoint → the peered PSA range on 5432
# (postgres), 3306 (mysql), 1433 (sqlserver) — every managed-DB engine reaches via the tunnel.
PSA_CIDR="$(gcloud compute addresses describe "$PSA_RANGE" --global --project "$PROJECT_ID" \
  --format 'value(address,prefixLength)' 2>/dev/null | awk 'NF==2{print $1"/"$2}')"
if [[ -n "$PSA_CIDR" ]]; then
  gcloud compute firewall-rules create "${NAME}-allow-db-from-jumpoint" \
    --project "$PROJECT_ID" --network "$VPC" \
    --direction EGRESS --priority 998 \
    --action ALLOW --rules tcp:5432,tcp:3306,tcp:1433 \
    --target-tags "$NETWORK_TAG_JP" --destination-ranges "$PSA_CIDR" \
    --quiet >/dev/null 2>&1 || true
  ok "Firewall: allow-db-from-jumpoint (tcp:5432,tcp:3306,tcp:1433 → $PSA_CIDR)"
  # Parity for a CO-LOCATED GKE cluster: the Entitle agent's nodes (tagged
  # ${NAME}-k8s) reach Cloud SQL directly (nodes live in the PSA-owning VPC; pod
  # traffic is SNAT'd to the node IP via ip-masq). Explicit/auditable egress ALLOW.
  gcloud compute firewall-rules create "${NAME}-allow-db-from-k8s" \
    --project "$PROJECT_ID" --network "$VPC" \
    --direction EGRESS --priority 998 \
    --action ALLOW --rules tcp:5432,tcp:3306,tcp:1433 \
    --target-tags "$NETWORK_TAG_K8S" --destination-ranges "$PSA_CIDR" \
    --quiet >/dev/null 2>&1 || true
  ok "Firewall: allow-db-from-k8s (tcp:5432,tcp:3306,tcp:1433 → $PSA_CIDR)"
else
  ok "PSA CIDR not resolvable yet — skipping explicit DB egress rules (default egress still reaches the DB)"
fi
state_write gcp psa_range "$PSA_RANGE"

# ── 5. Service account ──────────────────────────────────────────────────────
section "Service account"
SA_ID="${NAME}-sa"
SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_ID" \
    --project "$PROJECT_ID" \
    --display-name "Dashboard sandbox SA" --quiet >/dev/null
  ok "Created SA $SA_EMAIL"
else
  ok "Reusing SA $SA_EMAIL"
fi

# A just-created service account is eventually consistent: for a few seconds IAM
# can't resolve it as a policy member yet, so add-iam-policy-binding fails with
# "Service account … does not exist" (the "binding with condition" line gcloud
# prints above it is generic noise — we pass --condition=None). Retry each
# binding until the SA has propagated; bindings are idempotent, so this is safe.
# cloudbuild.builds.editor lets the dashboard SA SUBMIT the image-export Cloud
# Build (the "403 The caller does not have permission" at export time otherwise).
# container.admin lets the dashboard SA create/manage GKE clusters + node pools
# (compute.admin covers the module's VPC/subnet/router/NAT but not the cluster).
# logging.viewer lets the dashboard READ Cloud Logging so it can surface the
# real Cloud Build export failure (the Daisy error, e.g. a zone-capacity or
# quota message) on the job page instead of a generic "Build failed".
# The next three roles power GKE Entra federation (Workforce Identity + Connect
# Gateway; see docs/integrations/entra-k8s-federation.md). serviceusage.serviceUsageAdmin
# lets the dashboard enable the Connect Gateway APIs (the "403 Forbidden … services:batchEnable"
# at Enable-federation time otherwise); gkehub.admin lets it register the cluster to the
# fleet; resourcemanager.projectIamAdmin lets it grant the workforce principalSet the
# gkehub.gateway* roles (a project-level setIamPolicy).
# bigquery.jobUser + bigquery.dataViewer power the Cloud Costs page: the dashboard
# runs a query job (jobUser) against the Cloud Billing export table and reads its
# rows (dataViewer). Both are granted at project scope — if your billing export
# dataset lives in a DIFFERENT project, also grant dataViewer on that dataset there.
for role in roles/compute.admin roles/secretmanager.secretAccessor \
             roles/iam.serviceAccountUser roles/run.admin roles/run.developer \
             roles/run.invoker roles/cloudsql.admin roles/servicenetworking.networksAdmin \
             roles/cloudbuild.builds.editor roles/container.admin roles/logging.viewer \
             roles/serviceusage.serviceUsageAdmin roles/gkehub.admin \
             roles/resourcemanager.projectIamAdmin \
             roles/bigquery.jobUser roles/bigquery.dataViewer; do
  retry 8 5 gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:$SA_EMAIL" --role "$role" \
    --condition=None --quiet >/dev/null
done
ok "Granted compute.admin, secretmanager.secretAccessor, iam.serviceAccountUser, run.{admin,developer,invoker}, cloudsql.admin, servicenetworking.networksAdmin, cloudbuild.builds.editor, container.admin, logging.viewer, serviceusage.serviceUsageAdmin, gkehub.admin, resourcemanager.projectIamAdmin, bigquery.jobUser, bigquery.dataViewer"

SA_KEY_PATH="$(state_dir gcp)/sa-key.json"
if [[ ! -s "$SA_KEY_PATH" ]]; then
  gcloud iam service-accounts keys create "$SA_KEY_PATH" \
    --iam-account "$SA_EMAIL" --project "$PROJECT_ID" --quiet >/dev/null
  chmod 600 "$SA_KEY_PATH"
  ok "Created SA key at $SA_KEY_PATH (mode 600)"
else
  ok "Reusing SA key at $SA_KEY_PATH"
fi

# ── 5b. Image-hub GCS bucket + promote-runner plumbing ───────────────────────
# Provisions the prerequisites the dashboard's automated cross-cloud image
# promote runner needs (see docs/image-management.md, runners/promote/README.md):
#
#   • A GCS bucket that doubles as the image-registry hub and the staging
#     bucket the promote-runner Cloud Run Job writes converted tar.gz disks
#     to (under promote-staging/) before compute.images.insert consumes
#     them.
#   • storage.objectAdmin on that bucket for the dashboard SA — the runner
#     uploads as this SA via workload identity.
section "Image-hub GCS bucket + promote-runner IAM"

# GCS bucket names are globally unique; prefix with project ID to avoid
# collisions in shared organisations.
STORAGE_BUCKET="${PROJECT_ID}-${NAME}-storage"
# gcloud storage buckets describe returns nonzero if missing.
if gcloud storage buckets describe "gs://$STORAGE_BUCKET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  ok "Reusing GCS bucket gs://$STORAGE_BUCKET"
else
  gcloud storage buckets create "gs://$STORAGE_BUCKET" \
    --project "$PROJECT_ID" --location "$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention --quiet >/dev/null
  gcloud storage buckets update "gs://$STORAGE_BUCKET" \
    --project "$PROJECT_ID" \
    --update-labels "$LABELS" --quiet >/dev/null 2>&1 || true
  ok "Created GCS bucket gs://$STORAGE_BUCKET (uniform access, public prevention)"
fi
state_write gcp storage_bucket "$STORAGE_BUCKET"

# Grant the dashboard SA objectAdmin on the bucket — covers upload (runner)
# + read (dashboard mints signed URLs) + delete (cleanup after promote).
retry 8 5 gcloud storage buckets add-iam-policy-binding "gs://$STORAGE_BUCKET" \
  --member "serviceAccount:$SA_EMAIL" --role "roles/storage.objectAdmin" \
  --quiet >/dev/null
ok "Granted $SA_EMAIL storage.objectAdmin on gs://$STORAGE_BUCKET"

# ── 5c. Cloud Build image-export IAM ─────────────────────────────────────────
# The dashboard SUBMITS the image-export Cloud Build as itself (granted
# cloudbuild.builds.editor above), but the build RUNS as Cloud Build's default
# build service account — which spins up a temporary export VM and writes the
# VHD, so THAT SA needs compute + act-as + storage roles (otherwise the export
# fails a few minutes in with a permission error, not at submit time).
# Cloud Build's default build SA is the legacy <num>@cloudbuild SA on older
# projects and the Compute Engine default <num>-compute@developer SA on newer
# ones, so grant both — whichever the project uses is covered. Best-effort: a
# project may not have the legacy SA, which is fine (don't abort setup).
section "Cloud Build image-export IAM"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
for cb_sa in "${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
             "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"; do
  for role in roles/compute.admin roles/iam.serviceAccountUser \
              roles/iam.serviceAccountTokenCreator roles/storage.admin \
              roles/logging.logWriter; do
    retry 3 4 gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member "serviceAccount:$cb_sa" --role "$role" \
      --condition=None --quiet >/dev/null 2>&1 \
      || warn "Could not grant $role to $cb_sa (SA may not exist on this project — safe to ignore if export works)"
  done
done
ok "Granted Cloud Build export SA(s) compute.admin, iam.serviceAccountUser, iam.serviceAccountTokenCreator, storage.admin, logging.logWriter (best-effort)"

# ── 6. Secret Manager: SSH keypair JSON ─────────────────────────────────────
section "Secret Manager — SSH keypair"
SSH_SECRET="dashboard-sandbox-ssh-keypair"

if ! gcloud secrets describe "$SSH_SECRET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  TMPDIR="$(mktemp -d)"; trap 'rm -rf "$TMPDIR"' EXIT
  ssh-keygen -t rsa -b 4096 -N "" -C "dashboard-sandbox" -f "$TMPDIR/key" >/dev/null
  PUB="$(cat "$TMPDIR/key.pub")"
  PRIV="$(cat "$TMPDIR/key")"
  jq -n --arg pub "$PUB" --arg priv "$PRIV" \
    '{public_key:$pub, private_key:$priv}' > "$TMPDIR/keypair.json"
  gcloud secrets create "$SSH_SECRET" \
    --project "$PROJECT_ID" --replication-policy=automatic \
    --labels="$LABELS" --data-file "$TMPDIR/keypair.json" --quiet >/dev/null
  ok "Created secret $SSH_SECRET"
else
  ok "Reusing secret $SSH_SECRET"
fi

# ── 7. Print config to paste into /setup ────────────────────────────────────
_cfg=(
  "gcp_project_id=$PROJECT_ID"
  "gcp_region=$REGION"
  "gcp_zone=$ZONE"
  "gcp_network=$VPC"
  "gcp_subnetwork=$VM_SUBNET                                # User VMs land here (no NAT, no internet)"
  "gcp_jumpoint_subnetwork=$JP_SUBNET                       # Jumpoint COS lands here (Cloud NAT)"
  "gcp_ssh_key_secret_name=$SSH_SECRET                      # JSON {public_key, private_key}"
  "gcp_jumpoint_image=beyondtrust/sra-jumpoint:latest"
  "gcp_jumpoint_machine_type=e2-micro"
  "gcp_default_network_tag=$NETWORK_TAG_VM                  # Auto-attached to every dashboard-deployed VM so the sandbox firewall rules apply"
  "gcp_service_account_json=\$(cat $SA_KEY_PATH | jq -c .)   # paste the JSON contents"
  ""
  "# Managed databases (Cloud SQL private IP via the PRA tunnel):"
  "gcp_db_network=projects/$PROJECT_ID/global/networks/$VPC   # Cloud SQL private_network (private-services-access peered on it)"
  ""
  "# CO-LOCATE GKE in the sandbox VPC (so the in-cluster Entitle agent reaches VMs"
  "# AND Cloud SQL — peering is non-transitive and can't reach the PSA range). Set"
  "# gcp_k8s_subnetwork to switch GKE provisioning into co-located mode; blank ="
  "# self-contained-VPC + peering (unchanged)."
  "gcp_k8s_subnetwork=projects/$PROJECT_ID/regions/$REGION/subnetworks/$K8S_SUBNET   # GKE nodes land here (Cloud NAT egress)"
  "gcp_k8s_pods_range_name=gke-pods                          # VPC-native pods secondary range on the k8s subnet"
  "gcp_k8s_services_range_name=gke-services                  # VPC-native services secondary range on the k8s subnet"
  "gcp_k8s_node_tag=$NETWORK_TAG_K8S                         # Network tag on co-located GKE nodes (drives allow-db-from-k8s)"
  ""
  "# Image-registry hub + automated cross-cloud promote:"
  "storage_gcs_bucket=$STORAGE_BUCKET                       # Image hub + promote staging"
  "storage_active_backend=gcs                                # Active asset backend"
  "storage_hub_backend=gcs                                   # Image hub (defaults to active if unset)"
  "promote_runner_image=chrweav/dashboard-promote-runner:latest   # Public multi-arch image; override to Artifact Registry for a private/air-gapped registry"
  "promote_runner_gcp_region=$REGION                         # Cloud Run Job lands here"
  "promote_runner_gcp_service_account=$SA_EMAIL              # Workload-identity SA for the runner"
  "promote_runner_gcp_staging_bucket=$STORAGE_BUCKET"
  ""
  "# Cloud Costs page (GCP has no cost API — query the Cloud Billing BigQuery export):"
  "#   1. Billing → Billing export → enable 'Detailed usage cost' export to a BigQuery dataset."
  "#   2. Paste the fully-qualified export table below (the SA was granted bigquery.jobUser + dataViewer above)."
  "gcp_billing_export_table=…   # e.g. ${PROJECT_ID}.billing_export.gcp_billing_export_resource_v1_XXXXXX (paste manually)"
  ""
  "# ── Per-region set for $REGION ──────────────────────────────────────────────"
  "# The flat keys above configure the DEFAULT region. These gcp_region.<region>.*"
  "# keys land in gcp_region_configs, so re-running with a different REGION MERGES"
  "# that region in rather than overwriting this one. (Every field falls back to"
  "# its flat key when blank, so a single-region install is unchanged.)"
  "gcp_region.$REGION.zone=$ZONE"
  "gcp_region.$REGION.network=$VPC"
  "gcp_region.$REGION.subnetwork=$VM_SUBNET"
  "gcp_region.$REGION.jumpoint_subnetwork=$JP_SUBNET"
  "gcp_region.$REGION.db_network=projects/$PROJECT_ID/global/networks/$VPC"
  "gcp_region.$REGION.ssh_key_secret=$SSH_SECRET"
  "gcp_region.$REGION.default_network_tag=$NETWORK_TAG_VM"
  "gcp_region.$REGION.router_name=$ROUTER"
  "gcp_region.$REGION.nat_name=$NAT"
  "gcp_region.$REGION.k8s_subnetwork=projects/$PROJECT_ID/regions/$REGION/subnetworks/$K8S_SUBNET"
  "gcp_region.$REGION.k8s_pods_range=gke-pods"
  "gcp_region.$REGION.k8s_services_range=gke-services"
  "gcp_region.$REGION.k8s_node_tag=$NETWORK_TAG_K8S"
  ""
  "# BeyondTrust deploy key — set in /setup or /secrets:"
  "gcp_cloud_run_docker_deploy_key=…"
)
print_dashboard_config "GCP sandbox configuration" "${_cfg[@]}"
write_config_json gcp "${_cfg[@]}"   # machine-readable twin for onboard-sandbox.sh
# The printed block shows a "$(cat …)" placeholder so the SA private key never
# hits the terminal; the machine-readable config.json needs the real contents.
if command -v jq >/dev/null 2>&1 && [[ -f "$SA_KEY_PATH" ]]; then
  _gcp_cfg="$(state_dir gcp)/config.json"
  jq -c --arg sa "$(jq -c . "$SA_KEY_PATH")" '.gcp_service_account_json = $sa' "$_gcp_cfg" > "$_gcp_cfg.tmp" \
    && mv "$_gcp_cfg.tmp" "$_gcp_cfg"
fi

cat <<EOF
Sandbox topology summary

  VPC $VPC
    ├─ $JP_SUBNET (10.99.1.0/24) → Cloud NAT → internet  [Jumpoint COS]
    ├─ $VM_SUBNET (10.99.2.0/24) → no NAT → no internet  [user VMs]
    └─ $K8S_SUBNET (10.99.3.0/24) → Cloud NAT → internet  [co-located GKE nodes]
         pods 10.99.128.0/18 · services 10.99.192.0/20 (VPC-native secondary ranges)
    + servicenetworking PSA /20 (Cloud SQL private IP), reachable from jumpoint + co-located k8s

  Firewall:
    • allow-internal      : within 10.99.0.0/16 (covers k8s nodes/pods → VMs)
    • allow-ssh-from-jumpoint : tag $NETWORK_TAG_JP → tag $NETWORK_TAG_VM, tcp/22
    • deny-vm-egress      : tag $NETWORK_TAG_VM → 0.0.0.0/0 (any proto)
    • allow-vm-egress-vpc : tag $NETWORK_TAG_VM → 10.99.0.0/16
    • allow-db-from-jumpoint : tag $NETWORK_TAG_JP → PSA range, tcp/5432,3306,1433
    • allow-db-from-k8s   : tag $NETWORK_TAG_K8S → PSA range, tcp/5432,3306,1433

Service-account JSON cached at $SA_KEY_PATH (mode 600).

The dashboard auto-applies the ${NETWORK_TAG_JP} network tag to its Jumpoint
COS GCE instance and reads gcp_default_network_tag from config to attach
${NETWORK_TAG_VM} to every user VM it deploys, so the sandbox firewall
rules take effect automatically — no per-deploy manual tagging needed.

To tear it down:
  ./scripts/sandbox/Linux/rollback.sh --cloud gcp

EOF
