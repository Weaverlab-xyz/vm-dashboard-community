# Infrastructure Management Dashboard — Community Edition

A self-hosted web dashboard for managing infrastructure across AWS, Azure, GCP,
and (optionally) on-prem infrastructure (VMware, Hyper-V, Proxmox, Nutanix). Bring your own cloud credentials;
the dashboard deploys resources into **your** accounts.

> **▶️ Watch the intro:** [Infrastructure Management Dashboard — Community Edition (YouTube)](https://www.youtube.com/watch?v=RwMMBpfVg2o)
> — a short tour of what the dashboard does and how to get started.

> **Looking for a hosted version?** A managed SaaS edition is on the roadmap.
> It removes the on-disk JWT root key by fetching it from Azure Key Vault via
> a workload-managed identity (OIDC federation, no static credentials), and
> adds multi-tenant isolation, automatic rotation, and managed upgrades.
> See [docs/saas-comparison.md](docs/saas-comparison.md) for how it compares.

## How the dashboard thinks

Before you spin it up, the reference docs below explain the
opinions baked into the codebase — what the dashboard does *for* you,
and what discipline it expects from you. Read them in order if you're
new to the tool; skim if you already know how this kind of platform
works:

| Doc | What's in it | Read this when |
|---|---|---|
| [Infrastructure as Code](docs/infrastructure-as-code.md) | The Terraform-per-deploy model, per-job state, idempotent destroy, where Packer + the sandbox bootstrappers fit. | You're about to deploy your first cloud VM and want to know what's actually running underneath. |
| [Image Management](docs/image-management.md) | Build-once-promote-many lifecycle: build a portable VHD in one cloud, hub it in your designated storage backend, run **one-click cross-cloud promote** via a transient runner in the target cloud (ECS / ACI / Cloud Run). | You're about to build a custom image and need to know how it'll reach the other clouds. |
| [Config Management](docs/config-management.md) | Why one-shot ephemeral runners are the security argument, the .yml/.sh/.ps1/.rpm/.deb wrap rules, on-prem vs cloud target paths; ready-to-adapt Linux + Windows playbooks in [`examples/playbooks/`](examples/playbooks/). | You're about to run an Ansible job and want to know how the runner handles secrets and isolation. |
| [Secrets Management](docs/secrets-management.md) | Tier 1 (encrypted DB) → Tier 2 (external vault) → Tier 3 (vault-backed runtime checkout); migration UI; why the JWT root key can't move. | You're deciding where to store cloud credentials and how to evolve that over time. |
| [Storage Management](docs/storage-management.md) | Four backends (S3, Azure Blob, GCS, Local/UNC); migration; why backends are a deployment-level concern, not a per-feature one. | You're about to enable the Ansible feature flag — storage is a prerequisite. |
| [Cloud VMs](docs/cloud-vms.md) | Deploy EC2 / Azure VM / GCE / OCI Compute; the provisioning + PRA / Password Safe / Entitle layer model; per-cloud prerequisites and config keys. | You're deploying cloud VMs and want the full access/onboarding story. |
| [Databases](docs/databases.md) | Provision private Postgres / MySQL / SQL Server (AWS/Azure/GCP) + Oracle (OCI) — PRA tunnel, optional Password Safe rotation + Entitle JIT — or register a database you already run, on-premises included, as a Configuration Management target. | You're standing up a managed database and need the per-cloud prerequisites, or you want to manage one that already exists. |
| [Cloud Containers](docs/cloud-containers.md) | Deploy a stored Docker Compose file to ECS / ACI / GCE-COS; the supported subset; app starters in [`examples/compose/`](examples/compose/); monitoring the container fleet. | You want to run a containerized app (Guacamole, Trivy, OPA, …) on a cloud runtime without Portainer. |
| [Cloud Functions](docs/integrations/cloud-functions.md) *(preview)* | One Python handler deployed unchanged as an AWS Lambda / Azure Function App / GCP Cloud Run function, optionally VPC-attached; layered auth; workload templates in [`examples/functions/`](examples/functions/). | You need a stable HTTPS endpoint that external systems can call to act *inside* your network — the case a one-shot container can't serve. |
| [Entitle Dashboard Permissions](docs/integrations/entitle-dashboard-permissions.md) | Time-boxed permissions **inside the dashboard**, granted by Entitle. Compares the two mechanisms — REST (current; any user, immediate) and Entra groups (legacy; Entra only, next login) — and how to migrate between them. | You want dashboard access without standing admins, or you need to tell the two mechanisms apart. |
| [Kubernetes](docs/kubernetes.md) | Provision/import EKS / AKS / GKE; a Rancher management plane on the cloud of your choice; ESO secret delivery; PRA tunnels, Password Safe rotation of the injected ServiceAccount token, Entra→RBAC federation, Entitle JIT. | You're managing Kubernetes clusters and their privileged access. |
| [Generic OIDC (SSO)](docs/integrations/oidc.md) | Discovery-driven OpenID Connect login for any IdP (Okta, Entra, Keycloak, Google, …); PKCE, group→workgroup mapping, admin Settings panel. | You want single sign-on for the dashboard instead of local passwords. |
| [Auto-delete Timer](docs/auto-delete-timer.md) | Give provisioned VMs, databases and clusters an expiry, then run the same teardown the Destroy button runs. The four gates, the two one-hour arming clocks, extending and pinning. | You want lab resources to clean themselves up — read it *before* enabling, because it deletes infrastructure. |
| [Notifications](docs/notifications.md) | Outbound webhooks — Slack, Microsoft Teams (Power Automate Workflows), or a signed JSON envelope you point at anything. Auto-delete warnings, job failures, budget/secret/drift alerts. | You've turned on the auto-delete timer and want to hear about it without opening the dashboard. |
| [Remote Agents](docs/remote-agents.md) | Reaching infrastructure the dashboard cannot route to: an outbound-dialing agent an operator runs inside the private network — no inbound ports, no stored path in. Enrolment and credential sealing (*the config file travels, the key does not*), discovery, agent-executed Config Management, and the one-shot sibling runners for Hyper-V and bare ESXi. | Your hypervisors, databases or clusters live somewhere the dashboard can't reach — a customer network, a lab behind NAT, an air-gapped segment. |
| [Cloud Hosting](docs/cloud-hosting.md) | Running the dashboard itself on Azure Container Apps / Cloud Run / ECS instead of Compose: the gateway sidecar that splits the agent endpoint from the UI, why an unset `DATABASE_URL` or `JWT_SECRET_KEY` fails silently, and what on-premises capability you give up. | You want the dashboard reachable from outside your LAN, or fronting remote agents. |
| [Config Migration](docs/config-migration.md) | Moving Settings configuration between two instances. Why `pg_dump` of the config table restores unreadable ciphertext without erroring, and what deliberately stays behind. | You're standing up a second instance and don't want to re-type months of configuration. |
| [Personas](docs/personas/) | One page per role — Cloud Ops, DevOps, hypervisor admin, IT, OT/ICS, DBA, security analyst, SRE — covering what that person owns, the four-layer story in their language (provisioning → PRA → Password Safe → Entitle), the use cases to run, and which integrations each needs. The in-app **Use cases** page is the same catalog, with each card reporting whether this instance can actually run it. | You are presenting to a specific role and want the story and the click path, rather than a feature list. |
| [POV Instance](docs/pov-instance.md) | Running a **second** dashboard for customer POV/POC environments: why `install_profile` makes demo and POV mutually exclusive, which features each profile gets, the **BeyondTrust tenant registry** that replaces the singletons (and why an explicit tenant never falls back to the default), and the four things that bite — its own JWT key, a cold first boot, a reachable agent endpoint, and an unarmed auto-delete timer. | You do customer proof-of-value work and don't want it sharing an instance, a database or a BeyondTrust tenant with your demo estate. |
| [Skytap](docs/integrations/skytap.md) | The first POV **lab platform**: the API token (not your password), why 423 is normal rather than an error, why every read carries `keep_idle`, why the Terraform provider is deliberately unused, the **template contract** the broker VM has to satisfy — Skytap hands `user_data` to the guest and nothing executes it — and the **template builder** that now writes that runner for you and bakes the result into a new template. | You're pointing a POV instance at Skytap and want to know what it will and won't do with the account. |

Together they're the philosophy of the tool: **declarative,
version-controlled, idempotent, ephemeral where it should be and
persistent where it must be**. The features in the rest of this
README make sense in that frame.

## Quick start

The fastest way to run the dashboard is to **pull the prebuilt image** from
Docker Hub — no local image build required. The image is multi-arch, so
`docker pull` automatically selects the right build for your machine
(Intel/AMD, Apple Silicon, AWS Graviton, Raspberry Pi 5).

**Windows** (PowerShell 7):

```powershell
.\scripts\Onboard-Dashboard.ps1 -Hub
```

**macOS / Linux / WSL / Raspberry Pi** (bash):

```bash
./scripts/onboard.sh --hub
```

This pulls `chrweav/infra-dashboard` and starts it alongside Postgres using
`docker-compose.hub.yml`. Drop the `--hub` / `-Hub` flag to **build the image
from source** instead (for contributors, or to customize the build).

Either way the script checks prerequisites, generates bootstrap secrets (JWT
signing key + Postgres password), and brings up the Docker Compose stack. Your
browser opens automatically to a **setup wizard**. Create your admin account, then either bring your own cloud
credentials (paste an access key / service principal / service-account JSON) or
skip a cloud to just explore the UI. No creds handy? each cloud step has an
optional panel to spin up a throwaway lab sandbox. Credentials are encrypted
with AES-256 and stored in the database — nothing sensitive stays in any file
on disk.

**Prefer not to click through the wizard?** For a throwaway lab, the all-in-one
sandbox onboarder provisions infra in your chosen cloud(s) *on your machine* and
pushes the result straight into the dashboard's setup API — no wizard:

```bash
./scripts/sandbox/Linux/onboard-sandbox.sh --cloud all
# Windows:  .\scripts\sandbox\Windows\Onboard-Sandbox.ps1 -Cloud all
```

It prompts for an admin login, provisions, configures, then you log in — see
[`scripts/sandbox/README.md`](scripts/sandbox/README.md) for flags and teardown.

> **Just want to kick the tires without cloning the repo?** Everything you need is
> on the image's Docker Hub page. Copy the `docker-compose.yml` from
> **[hub.docker.com/r/chrweav/infra-dashboard](https://hub.docker.com/r/chrweav/infra-dashboard)**
> into an empty folder, generate a stable key with
> `openssl rand -hex 32 > .jwt_secret_key`, then `docker compose up -d`
> (set `POSTGRES_PASSWORD` first for anything beyond a quick trial).

> **WSL users:** Docker Desktop is not required. Install Docker Engine
> directly in your WSL distro (`sudo apt install docker.io` or follow the
> [official guide](https://docs.docker.com/engine/install/ubuntu/)), start
> it with `sudo service docker start`, then run `./scripts/onboard.sh`.
> The script detects WSL automatically and opens the dashboard in your
> Windows browser.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for the full walkthrough,
including AWS IAM setup, Azure service principal setup, and the
feature-test checklist. The "How the dashboard thinks" docs above
go deeper on each axis once you're up and running.

## What's included

- **AWS** — EC2 deployment, AMI browsing, image capture, SSH-key management
- **Azure** — VM deployment (Marketplace + private images), Shared Image
  Gallery, Azure Container Instances
- **GCP** — Compute Engine deployment (public OS images + custom images),
  instance management, image capture, Secret Manager SSH-key integration
- **OCI** — Compute deployment, custom images, Autonomous Database, OKE
  clusters; API-key signing auth, compartment-scoped
- **Identity** — local username/password, optional WebAuthn/FIDO2 MFA,
  optional Sign in with Microsoft (Entra ID)
- **Jobs** — background task tracking with live WebSocket updates

## What's optional (feature-flagged, off by default)

Enable these on the **setup wizard's Feature Flags step** or in **Settings →
Integrations** after first login — only if you have the backing infrastructure.
The wizard turns a flag on; the per-integration fields live in Settings:

- **VMware Workstation** — VM management (Windows host only; requires the
  Windows Compose override — see [docs/ONBOARDING.md](docs/ONBOARDING.md) Appendix A)
- **Proxmox VE** — VM and node management via the Proxmox REST API
- **VMware vSphere / ESXi** — VM power operations and inventory via SSH/API
- **Microsoft Hyper-V** — VM management via WinRM
- **Nutanix AHV** — VM management via Prism Central REST API
- **XCP-ng / XenServer** — VM management via XAPI
- **Remote Worker (Ansible + Kubernetes runners)** — the **Ansible runner**
  runs playbooks (`.yml`) and provisioning assets (`.sh`, `.ps1`, `.rpm`,
  `.deb`) against any target: on-premises hypervisors (Proxmox, vSphere,
  Hyper-V, Nutanix, XCP-ng) *or* cloud VMs (EC2, Azure VMs, GCE). Assets
  live in storage you configure on `/storage` (AWS S3 / Azure Blob / GCS /
  Local-or-UNC). The **Kubernetes runner** runs cluster-API ops
  (`kubectl`/`helm`) for the entitle agent, ESO, and mgmt-plane. Both can be
  local, or a one-shot AWS ECS / Azure ACI / GCP Cloud Run task for private
  subnets or to side-step a corp proxy — and they share the same per-cloud
  cloud-task settings (the image-promote runner reuses them too). Every
  runner is one-shot — see [docs/config-management.md](docs/config-management.md)
  for the security argument. Integration setup in
  [docs/integrations/ansible.md](docs/integrations/ansible.md).
- **Remote Agents** — an outbound-dialing agent you run inside a private
  network so the dashboard can manage what it can't route to: hypervisor
  discovery and inventory, agent-executed Config Management, and Hyper-V /
  bare-ESXi access via a one-shot sibling container. The agent holds the
  credentials; the dashboard never needs a path in. See
  [docs/remote-agents.md](docs/remote-agents.md).
- **Cloud Databases** — provision private Postgres / MySQL / SQL Server on
  AWS/Azure/GCP and Oracle on OCI, brokered through a PRA tunnel, or register a
  database you already run (on-premises included) as a Config Management
  target. See [docs/databases.md](docs/databases.md).
- **Kubernetes** — provision or import EKS / AKS / GKE / OKE, run a Rancher
  management plane, deliver secrets via ESO, and layer PRA tunnels, Password
  Safe token rotation and Entra→RBAC federation on top. See
  [docs/kubernetes.md](docs/kubernetes.md).
- **Cloud Costs** — a spend tile and a `/costs` page built on AWS Cost Explorer
  and Azure Cost Management, scoped to what the dashboard deployed.
- **BeyondTrust Password Safe** — on-demand checkout of SSH keys and passwords, plus onboarding of the VMs, databases and Kubernetes tokens the dashboard builds as managed systems + accounts. See [docs/integrations/password-safe.md](docs/integrations/password-safe.md).
- **BeyondTrust Privileged Remote Access** — Shell Jump, Web Jump, Remote RDP and protocol-tunnel jump items plus PRA Vault accounts, and the Gateway hosts they broker through. See [docs/integrations/privileged-remote-access.md](docs/integrations/privileged-remote-access.md).
- **BeyondTrust EPM for Linux (EPM-L)** — list and build agent packages, one-click sync of `.rpm`/`.deb` packages to your Ansible asset bucket, installation-token issuance for new endpoint registration. See [docs/integrations/epml.md](docs/integrations/epml.md).
- **Portainer CE** — on-prem Docker host management
- **Entitle** — approval-workflow integration
- **MCP server** — read-only AI client integration (Claude Desktop, Claude Code, Cursor…) via Personal Access Token; mounted at `/mcp`, no extra containers needed
- **Action Guardrails** — pre-action policy gate (OPA): evaluate every deploy against Rego policies *before* the job starts — allowed regions, blocked instance sizes, change-freeze windows — and block disallowed ones (403, audited). Fails closed. See [docs/policy-guardrails.md](docs/policy-guardrails.md).

## Docker images

Published to Docker Hub (multi-arch `amd64`/`arm64`) by the **Publish images**
workflow — each tagged `latest` and by version (`MAJOR.MINOR`, `MAJOR.MINOR.PATCH`)
on release:

| Image | What it is |
|---|---|
| [`chrweav/infra-dashboard`](https://hub.docker.com/r/chrweav/infra-dashboard) | The dashboard application container (pulled by `docker-compose.hub.yml`). |
| [`chrweav/ansible-winrm`](https://hub.docker.com/r/chrweav/ansible-winrm) | Default Ansible config-management runner — upstream `willhallonline/ansible` **+ `pywinrm`**, so both Linux SSH and Windows WinRM targets work out of the box. Built from [`runners/ansible-winrm/`](runners/ansible-winrm/). |
| [`chrweav/ansible-cloud`](https://hub.docker.com/r/chrweav/ansible-cloud) | Ansible runner for **Kubernetes cluster / database** targets — `kubernetes.core` + the helm CLI + the DB collections and client libs, for `hosts: localhost` plays on an in-cloud runner or — for on-prem targets — a sibling container on the dashboard host. Built from [`runners/ansible-cloud/`](runners/ansible-cloud/). |
| [`chrweav/dashboard-promote-runner`](https://hub.docker.com/r/chrweav/dashboard-promote-runner) | One-shot cross-cloud image-promote runner (ECS / ACI / Cloud Run). Built from [`runners/promote/`](runners/promote/). |
| [`chrweav/dashboard-agent`](https://hub.docker.com/r/chrweav/dashboard-agent) | The **remote on-prem agent** — a long-lived container an operator runs inside a private network, which dials the dashboard outbound (no inbound ports, no credentials in the dashboard). Carries exactly three dependencies and deliberately no ansible / kubectl / helm / container client. Built from [`runners/agent/`](runners/agent/). Unlike the runners above, **you pull this one** — the Agents page hands out an install command naming this tag, so the published image *is* the distribution channel. See [docs/remote-agents.md](docs/remote-agents.md). |
| [`chrweav/hypervisor-runner`](https://hub.docker.com/r/chrweav/hypervisor-runner) | The agent's one-shot **sibling runner** for the two transports its three-dependency image can't carry: Hyper-V (WinRM/NTLM) and bare ESXi (SOAP). Built from [`runners/hypervisor/`](runners/hypervisor/). Operator-pulled as well — the agent never pulls it for you, because a pull is a network fetch of executable content and that's the operator's call, not a job's. |

## License

MIT — see [LICENSE](LICENSE).
