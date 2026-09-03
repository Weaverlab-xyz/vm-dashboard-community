# GCP setup

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are giving the dashboard a GCP project to deploy into.

Part of the [Onboarding Guide](../ONBOARDING.md).


The dashboard deploys Compute Engine instances into **your** GCP project using
a service account. GCP is optional — AWS and Azure work without it.

### 1. Prerequisites

Install the Google Cloud CLI (gcloud) if you haven't already:
<https://cloud.google.com/sdk/docs/install>

```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
```

### 2. Enable required APIs

For core VM deploys:

```bash
gcloud services enable compute.googleapis.com secretmanager.googleapis.com iam.googleapis.com
```

Optional features each need their own API. Rather than list a set that goes
stale, enable the same ones the sandbox script does — see the two
`gcloud services enable` calls in
[`scripts/sandbox/Linux/setup-gcp.sh`](../../scripts/sandbox/Linux/setup-gcp.sh),
which cover Cloud Run, Cloud Build, GKE (plus Fleet/Connect Gateway),
BigQuery, Cloud Functions, Artifact Registry, Service Networking and Cloud SQL
Admin.

### 3. Create a service account and download a key

```bash
# Create the service account
gcloud iam service-accounts create dashboard-sa \
  --display-name "VM Dashboard SA"

# Core roles: instances, impersonation for attached SAs, and secrets
for ROLE in roles/compute.admin \
            roles/iam.serviceAccountUser \
            roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding <PROJECT_ID> \
    --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
    --role "$ROLE"
done

# Terraform state lives in a bucket, so the SA needs to WRITE objects there.
# Deliberately bucket-scoped, not project-wide.
gcloud storage buckets add-iam-policy-binding gs://<YOUR_STATE_BUCKET> \
  --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role "roles/storage.objectAdmin"

# Download the JSON key
gcloud iam service-accounts keys create sa-key.json \
  --iam-account "dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com"
```

Keep `sa-key.json` safe. You'll paste its entire contents into the wizard.

> **`roles/compute.admin` alone is not enough.** It grants nothing on Cloud
> Storage, and Terraform keeps its state in your active storage backend — so
> without the `storage.objectAdmin` binding above, every apply and destroy
> fails. And with no storage backend configured at all, state falls back to the
> container's local disk, where losing that directory **orphans live cloud
> resources**. See
> [infrastructure-as-code.md](../infrastructure-as-code.md#state-the-thing-that-makes-iac-work).

Optional features need more roles on top:

| Feature | Extra roles |
|---------|-------------|
| Kubernetes clusters (GKE) | `roles/container.admin`, `roles/gkehub.admin`, `roles/resourcemanager.projectIamAdmin`, `roles/iam.roleAdmin`, `roles/serviceusage.serviceUsageAdmin` — see [kubernetes.md](../kubernetes.md) for why `container.admin` alone is insufficient |
| Cloud databases (Cloud SQL) | `roles/cloudsql.admin`, `roles/servicenetworking.networksAdmin` |
| Cloud Functions / Cloud Run | `roles/run.admin`, `roles/run.developer`, `roles/run.invoker`, `roles/cloudfunctions.developer`, `roles/cloudbuild.builds.builder`, `roles/artifactregistry.writer`, `roles/secretmanager.admin` |
| Image export (VHD) | `roles/cloudbuild.builds.editor`, plus the roles the Cloud Build service identities need — see [image-management.md](../image-management.md) |
| Cloud Costs | `roles/bigquery.jobUser`, `roles/bigquery.dataViewer` (see below) |
| External secrets backend (writing secrets) | `roles/secretmanager.secretVersionAdder`, or `roles/secretmanager.admin` on the project &mdash; see [secrets-management.md](../secrets-management.md#iam-permissions-required-per-backend) |
| Job log viewing | `roles/logging.viewer` |

The full set the sandbox grants is the `for role in ...` loop in
[`setup-gcp.sh`](../../scripts/sandbox/Linux/setup-gcp.sh), which carries a
why-comment per role. That script is the source of truth — listing every role
here would only go stale.

> **Cloud Costs on GCP is a BigQuery query, not a cost API.** You must first
> create a **Cloud Billing export to BigQuery** in the Billing console — no
> setup script can create it for you. Then set the export table on the Cloud
> Costs settings page (`<project>.<dataset>.gcp_billing_export_v1_XXXX`) and
> grant the service account `roles/bigquery.jobUser` +
> `roles/bigquery.dataViewer`. **If the export dataset lives in a different
> project**, grant `dataViewer` on that dataset in that project too — a
> project-level binding here will not reach it.

### 4. (Optional) Store an SSH key pair in Secret Manager

If you want the dashboard to inject SSH keys automatically:

```bash
# Create a JSON secret with your public key
echo '{"public_key":"ssh-rsa AAAA... user@host"}' | \
  gcloud secrets create my-ssh-keypair \
    --data-file=- \
    --replication-policy=automatic

# Grant the service account access (if not already inherited from secretAccessor above)
gcloud secrets add-iam-policy-binding my-ssh-keypair \
  --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role "roles/secretmanager.secretAccessor"
```

Note the secret name (`my-ssh-keypair`) — you'll enter it in the wizard.

### 5. Enter credentials in the wizard

When you run the onboard script, the wizard Step 4 (GCP) asks for:

| Field | Where to get it |
|-------|-----------------|
| Project ID | `gcloud config get project` |
| Region | Your preferred GCP region (e.g. `us-central1`) |
| Zone | A zone in that region (e.g. `us-central1-a`) |
| Service Account JSON | Full contents of `sa-key.json` |
| SSH Key Secret Name | Name of the Secret Manager secret from step 4 |

---
