# Rancher configuration reference

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you need what a Rancher setting does, or which one to change.

Part of [Rancher Kubernetes Management](../rancher.md).

All keys are set via **Settings** (encrypted in the application database) and
apply immediately.

| Key | Default | Purpose |
|---|---|---|
| `k8s_management_enabled` | `false` | Master toggle; surfaces the Rancher tab |
| `rancher_bootstrap_password` | — | First-run bootstrap password (secret; transient — Rancher requires the admin password to differ). ≥12 chars |
| `rancher_admin_password` | `""` | Admin UI password for auto first-run (secret); blank = auto-generate a distinct one, surfaced in the panel + job result |
| `rancher_auto_first_run` | `true` | Auto-complete Rancher's first-run wizard on a fresh deploy (password + EULA + telemetry) |
| `rancher_node_cloud` | `gcp` | `aws` \| `azure` \| `gcp` — which cloud hosts the node. Picked on the deploy form and rewritten to where it actually landed, so teardown and bare redeploys stay put. Defaults to `gcp` because every node deployed before this key existed is a GCE VM |
| `rancher_allowed_source_cidrs` | `""` | *Additive* manual CIDRs (tcp 80/443); the dashboard's own egress, provisioned clusters + the Web-Jump Gateway are auto-added. Empty + nothing auto-discovered = closed |
| `rancher_dashboard_egress_cidr` | (runtime) | The dashboard's own public egress IP/CIDR, auto-detected + persisted on deploy so the worker can reach the node's public IP. Behind a corp proxy pool set the pool's CIDR — a stored CIDR containing the detected IP is kept, not clobbered. Bare IP → `/32` |
| `rancher_dashboard_egress_recent` | (runtime) | Bounded CSV of recently-detected egress `/32`s, admitted alongside the current one so a host with no stable outbound address does not lock the deploy out mid-job |
| `rancher_acme_domain` | _(blank)_ | FQDN for a publicly trusted, auto-renewing Let's Encrypt certificate via Rancher's built-in ACME client; blank = self-signed. Requires an A record already pointing at the node and opens tcp 80 to `0.0.0.0/0` for HTTP-01 (443 stays source-restricted) ([details](network-and-tls.md#public-certificate-lets-encrypt)) |
| `rancher_ready_timeout_s` | `360` | Seconds the deploy waits for Rancher to serve after boot; raise for slow disks / large images |
| `rancher_api_transport` | `direct` | `direct` \| `runner` — run the Rancher API calls as curl in a one-shot job **in the node's own cloud** when this network's TLS inspection blocks the node's self-signed cert ([details](network-and-tls.md#corp-tls-inspection-api-transport)) |
| `rancher_internal_url` | (runtime) | `https://<node internal IP>` captured at deploy — what the runner transport dials |
| `rancher_runner_source_cidr` | `""` | The runner's source range (the direct-egress subnet's CIDR, the connector's `/28`, the ECS runner subnet, or the ACI delegated subnet); auto-added to the node's ingress while the transport is `runner` |
| `gcp_run_network` / `gcp_run_subnetwork` | `""` | Direct VPC egress for Cloud Run runner jobs (preferred over `gcp_ansible_vpc_connector`; no standing infra) |
| `rancher_verify_tls` | `false` | Verify the node's cert on API calls |
| `rancher_server_url` | (runtime) | Set to `https://<node IP>` by the deploy job |
| `rancher_api_token` | (runtime) | Minted at bootstrap (secret) |
| `rancher_ui_web_jump_enabled` | `false` | Opt-in PRA Web Jump broker for the UI |
| `rancher_ui_verify_certificate` | `false` | Web Jump cert verification |
| `rancher_ui_jump_group` / `rancher_ui_jumpoint_name` | `""` (= `bt_jump_group_name` / `bt_jumpoint_name`) | The default Asset Group and Gateway for the Web Jump. The Containers page can pick both per deploy; these are what it starts from |
| `rancher_ui_jumpoint_cloud` | `gcp` | Which dashboard-managed Gateway host brokers the UI (`gcp`\|`aws`\|`azure`); its egress IP is auto-whitelisted |
| `rancher_ui_jumpoint_egress_ip` | (runtime) | Captured egress IP of the SHARED Web-Jump Gateway (auto-added to the firewall). Gateways you deploy yourself come from the gateway registry, so every cluster node is allowed |
| `rancher_ui_vault_account_group_id` | `""` | PRA Vault account group (numeric id) the admin credential is vaulted into for Web-Jump injection; usually chosen per-deploy |
| `rancher_ui_vault_account_id` | (runtime) | PRA Vault account id created for the admin credential; cleared on teardown |
| `entitle_source_cidrs` | `""` | CSV of Entitle's own egress CIDRs, merged into the node firewall while a directly-registered integration exists. Blank falls back to the published per-region list (US/Pathfinder is built in). Set it only if your tenant is on another deployment |
| `entitle_rancher_private` | `false` | Attach the Entitle agent token (node not reachable from Entitle's cloud). Makes `entitle_source_cidrs` irrelevant — no inbound ranges are opened. Env/config only — no Settings widget |
| `entitle_rancher_integration_id` | (runtime) | The registered Entitle integration; blank = not registered. Drives the node row's `Entitle ✓` chip and hides **Register** so a re-register cannot orphan it |
| `entitle_rancher_tfstate` | (runtime) | Terraform state for that integration, and the only handle `deregister` has on it. Cleared on teardown |

## Per-cloud node keys

One group per cloud, all optional — the defaults are usable. Only the group for the
cloud the node runs on has any effect, which is why each cloud can keep its own.

| Key | Default | Purpose |
|---|---|---|
| `gcp_rancher_image` / `aws_rancher_image` / `azure_rancher_image` | `rancher/rancher:latest` | Rancher container image. Pin a version for reproducibility |
| `gcp_rancher_machine_type` | `e2-medium` | GCE size (≥ 4 GB enforced — the launcher refuses the obvious too-small types) |
| `aws_rancher_instance_type` | `t3.medium` | EC2 size. `t3.small` (2 GB) OOMs |
| `azure_rancher_vm_size` | `Standard_B2s` | Azure size. `Standard_B1s` (1 GB) OOMs |
| `gcp_rancher_name` / `aws_rancher_name` / `azure_rancher_name` | `rancher-server` | VM (or instance) name, and the base name of its ingress rule (`<name>-allow-mgmt`) |
| `gcp_rancher_boot_disk_gb` / `aws_rancher_boot_disk_gb` / `azure_rancher_boot_disk_gb` | `30` | Boot / root / OS disk size. Holds `/var/lib/rancher` and is deleted with the VM |
| `gcp_rancher_allow_open` / `aws_rancher_allow_open` / `azure_rancher_allow_open` | `false` | Open `0.0.0.0/0` on that cloud when no CIDRs are set (discouraged) |
| `gcp_rancher_network_tag` | `rancher` | GCE network tag = the firewall rule's target. No analogue on AWS/Azure, where a dedicated security group / NSG *is* the scope |
| `gcp_rancher_zone` | `""` | Zone the node runs in. Overwritten on deploy with the **actual** launched zone. Blank → the region's first available zone. Pick a region/zone per-deploy on the Rancher tab |
| `aws_rancher_zone` | (runtime) | **Recorded, not chosen** — the availability zone the node landed in. The subnet pins it, so there is nothing to pick |
| `azure_rancher_zone` | (runtime) | **Recorded, not chosen** — the location the node landed in, so a bare redeploy stays there |
