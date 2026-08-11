# BeyondTrust Privileged Remote Access

## What is it?

**BeyondTrust Privileged Remote Access (PRA/SRA)** brokers access to everything the
dashboard builds. The dashboard creates and tears down **Shell Jump**, **Web Jump**,
**Remote RDP** and **Protocol Tunnel** jump items, plus PRA Vault accounts, so operators
reach VMs, container UIs, databases and Kubernetes API servers through PRA rather than
direct network exposure.

Driven by the `sra` Terraform provider (ARM64-native, no binary dependency) plus a small
REST client for the few calls the provider can't make. It also runs the **Gateway hosts**
those jumps are brokered through — one auto-managed per cloud, plus any you deploy to
carry session load. See [Gateway hosts](gateways.md).

Gated by `pra_enabled`. This is one of three independently-gated BeyondTrust products; see
[BeyondTrust Integrations](beyondtrust.md) for the map, and
[Password Safe](password-safe.md) for the credential-vaulting half of the story.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust PRA | Appliance or cloud tenant. Required for jump items, protocol tunnels and PRA Vault accounts |
| Terraform + the `sra` provider | How PRA jump items are created and destroyed; fetched by the dashboard's Terraform flow. No CLI to install |
| A Jump Group and a Gateway | Both must **already exist** in PRA — the dashboard looks them up by name and does not create them |

---

## Setup

### Step 1 — PRA Config API credentials

The `sra` Terraform provider and the REST client both authenticate to the BeyondTrust
PRA Configuration API with an OAuth 2.0 client-credentials pair.

1. In **BeyondTrust PRA** → **Configuration** → **API Configuration** →
   **Add API Account**. Copy the **Client ID** and **Client Secret**.
2. The API host is the hostname of your PRA appliance, e.g.
   `https://pra.company.com`.

Grant the account permission to manage **Jump Items** and **Jump Groups**, and — if you
use the cloud-database or Kubernetes tunnels — read access to **Vault Accounts** (the
dashboard enumerates account groups to populate the provision form).

> If your PRA appliance and Password Safe are the same host, these credentials may be
> identical to the Password Safe API Registration pair — see
> [Password Safe → Step 1](password-safe.md#step-1--password-safe-oauth-application-ps-cli).

**Optional second credential.** Onboarding a database's PRA Vault account into Password
Safe creates a functional account whose username/password *is* a PRA Config API client
pair. Set `pra_config_api_client_id` / `pra_config_api_client_secret` to use a separate,
narrower client for that; left blank, the dashboard falls back to the main
`bt_client_id` / `bt_client_secret`.

### Step 2 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

The wizard Step 5 lists optional integrations. Toggle **Privileged Remote Access** on and
fill in the fields.

**Option B — Settings → Integrations (after first run)**

1. Open **Settings** → **Integrations** → **Privileged Remote Access** → toggle on
   (`pra_enabled`).
2. Fill in the **API connection** section:

   | Field | Example |
   |---|---|
   | PRA API Host | `https://pra.company.com` |
   | OAuth Client ID | (from API Account) |
   | OAuth Client Secret | (from API Account) |

3. Fill in **Shell Jump provisioning** — the Jump Group and Gateway to use, with optional
   per-cloud overrides for Azure and GCP. The pickers enumerate both from the PRA Config
   API, so a value that isn't listed does not exist.
4. Click **Save**. No container restart is required.

> These are **Settings keys**, not environment variables. The dashboard's config store
> reads from the database only; exporting an equivalently-named env var has no effect.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **PRA jump items** | Shell Jump (VMs), Web Jump (Portainer / Rancher UIs), Remote RDP (virtual desktops) and Protocol Tunnel (databases, Kubernetes API) — created and torn down with the resource |
| **Gateway hosts** | The hosts those jumps broker *through*. One per cloud is auto-ensured and reference-counted; **Containers → Gateways** inventories them and deploys more to carry session load. See [Gateway hosts](gateways.md) |
| **PRA Vault accounts** | Tunnel credentials are minted as PRA Vault accounts, which can themselves be onboarded into [Password Safe](password-safe.md) for rotation |

---

## Kubernetes tunnel identity

A cluster's PRA k8s tunnel injects a ServiceAccount bearer token at session launch
([Kubernetes → Access & identity](../kubernetes.md#access--identity)). PRA mints that
ServiceAccount in-cluster; `pra_k8s_namespace` / `pra_k8s_sa_name` name it, and
`bt_vault_account_group_id` is the vault account group injected Kubernetes, database and
Web Jump credentials land in.

Password Safe can take that token over and rotate it on the tenant's schedule — the
managed account name is `<namespace>/<serviceaccount>`, built from the two keys above. See
[Password Safe → Kubernetes ServiceAccount token rotation](password-safe.md#kubernetes-serviceaccount-token-rotation).
That path needs both products enabled; PRA alone injects a token that never rotates.

---

## Preparing images for PRA

Shell Jump connectivity is baked at image-build time — sshd hardening, passwordless sudo,
and optional SSH certificate-authority trust so accounts accept certificates PRA issues
instead of needing a static key. Because those scripts also prepare Password Safe and
EPM-L in the same pass, they are documented once on the hub:
[BeyondTrust Integrations → Preparing images for BT management](beyondtrust.md#preparing-images-for-bt-management).

---

## Advanced configuration

| Key | Notes |
|---|---|
| `bt_api_host` | PRA appliance hostname, e.g. `tenant.beyondtrustcloud.com` |
| `bt_client_id` / `bt_client_secret` | PRA Config API OAuth client-credentials pair |
| `pra_config_api_client_id` / `pra_config_api_client_secret` | Optional narrower client used only for the PRA Vault functional account; falls back to `bt_client_id` / `bt_client_secret` |
| `bt_jump_group_name` | Jump group new jump items land in |
| `bt_jumpoint_name` | The Gateway to route through, **by name** — the pickers in Settings enumerate both from the PRA Config API |
| `azure_bt_jump_group_name` / `azure_jumpoint_name` | Azure-specific overrides; blank falls back to the values above |
| `gcp_bt_jump_group_name` / `gcp_jumpoint_name` | GCP-specific overrides; blank falls back to the values above |
| `azure_vm_jumpoint_mode` | `shared` (default) or `aci` — whether a single Azure VM deploy borrows the ref-counted Gateway VM or starts its own ACI container group |
| `gcp_vm_jumpoint_mode` | `shared` (default) or `paired` — whether a single GCP VM deploy borrows the ref-counted Gateway host or starts its own |
| `pra_k8s_namespace` / `pra_k8s_sa_name` | `pra-access` — the ServiceAccount PRA injects for Kubernetes tunnels |
| `bt_vault_account_group_id` | Vault account group new tunnel credentials are created in |
| `bt_ps_deploy_key_title` | Title of the Password Safe secret holding the Docker deploy key — the dashboard looks secrets up by title. Needs [Password Safe](password-safe.md) too |
| `bt_shell_jump_id` | Recorded per-VM so the jump item can be torn down with the VM; not operator-set |

**Self-hosted Gateway (AWS).** A separate `bt_ecs_*` block provisions a Gateway host
as an ECS task or EC2 instance (`bt_ecs_launch_type`, `bt_ecs_cluster`,
`bt_ecs_jumpoint_subnet_id`, `bt_ecs_image`, and the CPU/memory/role keys). Configure it
in the same Settings panel; it exists so a cloud VPC can have a Gateway with
line-of-sight to private instances without one being run by hand.

Those keys describe the **auto-managed** AWS host — the one the dashboard ensures on
demand and reclaims when idle. It is not the only one you can have: **Containers →
Gateways** lists every gateway host in every cloud and deploys additional ones, which
join the same PRA Gateway as extra cluster nodes (so `bt_jumpoint_name` never changes).
Full detail — the managed-vs-requested lifecycle, placement, naming, and the node-firewall
consequences — is in **[Gateway hosts](gateways.md)**.

---

## Troubleshooting

**PRA jump item wasn't created** — the jump item is created by Terraform with the `sra`
provider as part of the resource's own job, so the failure is in that job's log, not a
separate one. Check that `bt_api_host` is reachable from the container
(`docker compose exec app curl -Is "https://<bt_api_host>"`), that the PRA Config API
account may manage Jump Items, and that `bt_jump_group_name` / `bt_jumpoint_name` match
entries that exist in PRA — they are matched by **name**, so a rename in PRA breaks
them silently. A self-signed appliance certificate may need adding to the container's
CA store.

**Shell Jump connects but authentication fails** — the target account has to exist on the
VM with the right key. Images built with the `bt-ready` provisioner create `adminuser` and
harden sshd; see
[Preparing images](beyondtrust.md#preparing-images-for-bt-management).

**A session times out reaching a private resource** — the Gateway host needs network
line-of-sight to the target, and its node firewall has to allow the source. Both are
covered in **[Gateway hosts](gateways.md)**.
