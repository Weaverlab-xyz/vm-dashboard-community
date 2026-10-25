# Wiring the sandbox into the dashboard

> **Audience:** operator · **Profile:** `demo` · **Read this when:** the sandbox scripts have run and you are giving the dashboard their output.

Part of [Cloud Sandbox Guide](../sandbox.md).

Each setup script ends with a config block formatted like this:

```
═══════════════════════════════════════════════════════════════
  GCP sandbox configuration — paste into /setup or Settings
═══════════════════════════════════════════════════════════════

gcp_project_id=my-lab-project
gcp_region=us-central1
gcp_zone=us-central1-a
gcp_network=dashboard-sandbox-vpc
gcp_subnetwork=dashboard-sandbox-vm-subnet
gcp_jumpoint_subnetwork=dashboard-sandbox-jumpoint-subnet
gcp_ssh_key_secret_name=dashboard-sandbox-ssh-keypair
gcp_jumpoint_image=beyondtrust/sra-jumpoint:latest
gcp_jumpoint_machine_type=e2-micro
gcp_default_network_tag=dashboard-sandbox-vm
gcp_service_account_json=$(cat …/sa-key.json | jq -c .)

# Managed databases (Cloud SQL private IP via the PRA tunnel):
gcp_db_network=projects/my-lab-project/global/networks/dashboard-sandbox-vpc

# Cloud Functions — gen2 sources reuse the image-hub bucket:
cloud_functions_enabled=true
function_package_gcs_bucket=my-lab-project-dashboard-sandbox-storage
gcp_functions_service_account=dashboard-sandbox-sa@my-lab-project.iam.gserviceaccount.com
gcp_functions_network=dashboard-sandbox-vpc
gcp_functions_subnetwork=dashboard-sandbox-vm-subnet

# BeyondTrust deploy key — set in /setup or /secrets:
gcp_cloud_run_docker_deploy_key=…
```

Note that `cloud_functions_enabled=true` rides along in the import, so **Cloud
Functions is already switched on** under Settings → Integrations when the
dashboard comes up — no restart and no extra click.

Three ways to apply these to a running dashboard:

**Automatic — `onboard-sandbox` (Path B, no wizard).** `onboard-sandbox.sh --cloud all`
(PowerShell `Onboard-Sandbox.ps1 -Cloud all`) provisions the cloud(s) **and** reads
what each `setup-*.sh` produced, then POSTs it to the dashboard's setup API
(`POST /api/setup/import`) — creating your admin and marking setup complete. You
never open `/setup`; you just log in. On a fresh dashboard it needs the one-time setup
token (`--setup-token` / `-SetupToken`, or `SETUP_TOKEN` in your shell; it is
`SETUP_TOKEN` in the dashboard's `.env`, else `/app/data/setup_token` in its container).

**Setup wizard (Path A, first run, manual paste).** Open the dashboard, walk
through `/setup`, and paste the matching values into Step 2 (AWS), Step 3 (Azure),
or Step 4 (GCP). Some keys are exposed under **Advanced** within each step.

**Settings panel (after first run, manual paste).** Open `/settings`, expand the
relevant cloud panel, paste the values, and **Save**. Each cloud panel patches the
same encrypted config DB the wizard writes to. Restarts are not needed — config is
read per-request.

For the sandbox specifically, you'll want to also paste the BeyondTrust
deploy key into the relevant cloud's step in `/setup` (under **Advanced**), or into
`/secrets` if you're using an external
secrets backend) — the setup scripts can't generate that for you because
it's issued by your PRA tenant.
