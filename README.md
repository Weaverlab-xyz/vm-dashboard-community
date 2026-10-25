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
> See [Editions](docs/editions.md) for how it compares.

## Who this is for

Every page under [`docs/`](docs/README.md) opens with the same line: who it is for,
which install profile it applies to, and the situation that should send you to it.
Five readers, five ways in:

| You are… | Start here |
|---|---|
| **installing or running** the dashboard | [Getting started](docs/getting-started.md), then [Where everything is](#where-everything-is) |
| **running your own infrastructure** with it | [Where everything is](#where-everything-is) — you want the `demo` profile, whatever its name suggests |
| **showing** it to someone | [Demo and POV profiles](docs/profiles/README.md) — the demo profile has one page per role, plus the OT demo cell |
| running **customer proof-of-value** work | [Demo and POV profiles](docs/profiles/README.md) — the POV profile |
| **changing the code** | [CONTRIBUTING.md](CONTRIBUTING.md), then [design notes](docs/design/README.md) |

And pick a profile before you install, because it is a gate rather than a preference:
`install_profile` is **`demo`** (the default — one BeyondTrust tenant of your own, whether
you run that estate or demonstrate from it) or **`pov`** (evaluation sandboxes you run, each
wired into the customer's own BeyondTrust tenant), and the two are mutually exclusive. The
gate is about *whose tenant*, not about whether the work is real — so if you are
administering infrastructure you actually depend on, `demo` is still the profile you
want. [Demo and POV profiles](docs/profiles/README.md)
explains why, and which features each one gets.

## How the dashboard thinks

Before you spin it up, five pages explain the opinions baked into the codebase: what the
dashboard does *for* you, and what discipline it expects from you. Read them in order if
you're new to the tool; skim if you already know how this kind of platform works.

1. **Infrastructure as Code**, in [Clouds](docs/clouds.md): a Terraform run per deploy,
   per-job state, idempotent destroy, and where Packer and the sandbox bootstrappers fit.
2. **Config Management**, in [Operations](docs/operations.md): why one-shot, ephemeral
   runners are the security argument, and how on-prem and cloud targets are reached.
3. **Secrets Management**, in [Admin](docs/admin.md): encrypted database, then an external
   vault, then runtime checkout from a vault, and why the JWT root key can't move. The
   recommended end state stores no cloud key at all: **workload identity**, in
   [Platform](docs/platform.md), with write privilege granted just in time.
4. **Storage Management**, in [Operations](docs/operations.md): four backends, and why a
   backend is a deployment-level choice rather than a per-feature one.
5. **Image Management**, in [Operations](docs/operations.md): build an image once, then
   promote it to the other clouds.

Together they're the philosophy of the tool: **declarative, version-controlled,
idempotent, ephemeral where it should be and persistent where it must be**. The features in
the rest of this README make sense in that frame.

## Where everything is

The docs follow the app: one section per group of its navigation menu, one for each
Settings → Integrations group that has no menu of its own, and one for first-run setup.
Each section has a front page that says which page answers what; this README links only
those, and [docs/README.md](docs/README.md) is the index of them.

| Section | In the app | Read this when |
|---|---|---|
| [Getting started](docs/getting-started.md) | first run | you're setting the dashboard up for the first time, and want the per-cloud setup and the feature-test checklist |
| [Hypervisors](docs/hypervisors.md) | menu → Hypervisors | your VMs run on VMware, Proxmox, Hyper-V, Nutanix or XCP-ng |
| [Clouds](docs/clouds.md) | menu → Clouds | you're putting workloads on AWS, Azure, GCP or OCI: how a deploy works, a sandbox, VMs and Windows servers |
| [Cloud services](docs/cloud-services.md) | menu → Cloud services | you're running containers (with or without Portainer), databases, Kubernetes (with or without Rancher) or functions |
| [Preview](docs/preview.md) | menu → Preview | you're trying virtual desktops, directories (Active Directory, LDAP, Entra ID, Okta, PingOne) or the Workload Lab |
| [Operations](docs/operations.md) | menu → Operations | you're running it day to day: Config Management, scheduling and change windows, images, storage, inventory, the job worker, costs and the auto-delete timer |
| [Admin](docs/admin.md) | menu → Admin | you're deciding who and what may use the dashboard, where its secrets live and how you'd prove what happened, or running its remote agents, health checks and notifications |
| [BeyondTrust](docs/beyondtrust.md) | Settings → BeyondTrust | you're wiring it into Privileged Remote Access, Password Safe, EPM for Linux or Entitle |
| [Platform](docs/platform.md) | Settings → Platform | you want single sign-on, the dashboard's own identity in place of stored keys, SPIRE, hosting it in a cloud, its Ansible and Kubernetes runners, or the MCP server |
| [Demo and POV profiles](docs/profiles/README.md) | — | you're presenting to a particular role (the Personas: one page per role), or running customer proof-of-value work on a separate instance |
| [Editions](docs/editions.md) | — | you're choosing between running the dashboard yourself and a hosted edition |

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
browser opens automatically to a **setup wizard**. Enter the one-time setup token the script prints
(`SETUP_TOKEN` in `.env`; it stops whoever reaches a fresh dashboard first from claiming it),
create your admin account, then either bring your own cloud
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

See [Getting started](docs/getting-started.md) for the full walkthrough,
including AWS IAM setup, Azure service principal setup, and the
feature-test checklist. The "How the dashboard thinks" pages above
go deeper on each axis once you're up and running.

## What's included

- **AWS** — EC2 deployment, AMI browsing, image capture, SSH-key management
- **Azure** — VM deployment (Marketplace + private images), Shared Image
  Gallery, Azure Container Instances
- **GCP** — Compute Engine deployment (public OS images + custom images),
  instance management, image capture, Secret Manager SSH-key integration
- **OCI** — Compute deployment, custom images, Autonomous Database, OKE
  clusters; API-key signing auth, compartment-scoped
- **Identity** — local username/password, optional WebAuthn/FIDO2 MFA, and single
  sign-on through any OpenID Connect provider (Okta, Entra ID, Keycloak, Google, …),
  live as soon as an issuer is configured; see [Platform](docs/platform.md). The older
  Sign in with Microsoft button is still there.
- **Service accounts** — OAuth 2.0 client credentials for CI jobs, scripts and agents,
  instead of handing them a personal token; see
  [Admin](docs/admin.md)
- **Audit log** — who did what, and a way to check the record has not been edited; see
  [Admin](docs/admin.md)
- **Jobs** — background task tracking with live WebSocket updates

## What's optional (feature-flagged, off by default)

Enable these on the **setup wizard's Feature Flags step** or in **Settings →
Integrations** after first login — only if you have the backing infrastructure.
The wizard turns a flag on; the per-integration fields live in Settings:

- **VMware Workstation** — VM management (Windows host only; needs a remote
  agent on the Workstation host — see [Hypervisors](docs/hypervisors.md))
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
  runner is one-shot — see [Operations](docs/operations.md)
  for the security argument. Integration setup in
  [Platform](docs/platform.md).
- **Remote Agents** — an outbound-dialing agent you run inside a private
  network so the dashboard can manage what it can't route to: hypervisor
  discovery and inventory, agent-executed Config Management, and Hyper-V /
  bare-ESXi access via a one-shot sibling container. The agent holds the
  credentials; the dashboard never needs a path in. See
  [Admin](docs/admin.md).
- **Cloud Databases** — provision private Postgres / MySQL / SQL Server on
  AWS/Azure/GCP, Oracle on AWS RDS or OCI, and MongoDB on Atlas, brokered through a PRA tunnel, or register a
  database you already run (on-premises included) as a Config Management
  target. See [Cloud services](docs/cloud-services.md).
- **Kubernetes** — provision or import EKS / AKS / GKE / OKE, run a Rancher
  management plane, deliver secrets via ESO, and layer PRA tunnels, Password
  Safe token rotation and Entra→RBAC federation on top. See
  [Cloud services](docs/cloud-services.md).
- **Cloud Costs** (`cost_explorer_enabled`) — month-to-date spend for every configured
  cloud on a `/costs` page and a home-page tile, with budgets and alerts: AWS Cost
  Explorer, Azure Cost Management, GCP's BigQuery billing export and OCI's Usage API. See
  [Operations](docs/operations.md).
- **BeyondTrust Password Safe** — on-demand checkout of SSH keys and passwords, plus onboarding of the VMs, databases and Kubernetes tokens the dashboard builds as managed systems + accounts. See [BeyondTrust](docs/beyondtrust.md).
- **BeyondTrust Privileged Remote Access** — Shell Jump, Web Jump, Remote RDP and protocol-tunnel jump items plus PRA Vault accounts, and the Gateway hosts they broker through. See [BeyondTrust](docs/beyondtrust.md).
- **BeyondTrust EPM for Linux (EPM-L)** — list and build agent packages, one-click sync of `.rpm`/`.deb` packages to your Ansible asset bucket, installation-token issuance for new endpoint registration. See [BeyondTrust](docs/beyondtrust.md).
- **Portainer CE** — on-prem Docker host management, through a Portainer you run or one
  the dashboard deploys for you. See [Cloud services](docs/cloud-services.md).
- **Entitle** — just-in-time access: register what the dashboard builds (VMs, databases,
  clusters, Rancher, Portainer) as Entitle resources, and grant dashboard permissions
  themselves for a limited time. See [BeyondTrust](docs/beyondtrust.md).
- **Directories** (`directories_enabled`, preview) — build or register Active Directory in
  AWS, Azure and GCP and join Windows servers to it, manage on-prem AD and LDAP through a
  remote agent, and browse and change group membership in Entra ID, Okta and PingOne. See
  [Preview](docs/preview.md).
- **Virtual Desktops** (`vdesktops_enabled`, preview) — pools of private desktop VMs that
  people reach through the PRA Gateway. See [Preview](docs/preview.md).
- **Cloud Functions** (`cloud_functions_enabled`) — one Python handler deployed as an AWS
  Lambda, Azure Function App or GCP Cloud Run function: a stable HTTPS endpoint inside your
  network, including the Entitle adapters. See [Cloud services](docs/cloud-services.md).
- **Workload Lab** (`cert_lab_enabled`, `spire_lab_enabled`, `agentcell_enabled`,
  `workload_credentials_enabled`) — credentials for things that are not people: a
  private CA, SPIFFE identities, bound Kubernetes tokens, short-lived cloud keys. See
  [Preview](docs/preview.md).
- **Notifications** (`notifications_enabled`, dry-run by default) — outbound webhooks to
  Slack, Microsoft Teams or anything that takes signed JSON, for auto-delete warnings, job
  failures and budget, secret and drift alerts. See [Admin](docs/admin.md).
- **MCP server** (`mcp_server_enabled`) — read-only AI client integration (Claude Desktop, Claude Code, Cursor…) via Personal Access Token; mounted at `/mcp`, no extra containers needed. Each tool returns only what the token's owner can see in the UI. See [Platform](docs/platform.md)
- **Unmanaged VM discovery** (`cloud_unmanaged_discovery_enabled`) — show cloud VMs this dashboard did not deploy, in a separate list per cloud console. Discovered VMs can be started and stopped; they can never be destroyed from here. Off by default because it lists every instance in the account rather than the ones the dashboard deployed. A discovered VM is admin-only unless it carries a `workgroup` tag.
- **Action Guardrails** — pre-action policy gate (OPA): evaluate every deploy against Rego policies *before* the job starts — allowed regions, blocked instance sizes, change-freeze windows — and block disallowed ones (403, audited). Fails closed. See [Operations](docs/operations.md).

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
| [`chrweav/dashboard-agent`](https://hub.docker.com/r/chrweav/dashboard-agent) | The **remote on-prem agent** — a long-lived container an operator runs inside a private network, which dials the dashboard outbound (no inbound ports, no credentials in the dashboard). Carries exactly three dependencies and deliberately no ansible / kubectl / helm / container client. Built from [`runners/agent/`](runners/agent/). Unlike the runners above, **you pull this one** — the Agents page hands out an install command naming this tag, so the published image *is* the distribution channel. See [Admin](docs/admin.md). |
| [`chrweav/hypervisor-runner`](https://hub.docker.com/r/chrweav/hypervisor-runner) | The agent's one-shot **sibling runner** for the two transports its three-dependency image can't carry: Hyper-V (WinRM/NTLM) and bare ESXi (SOAP). Built from [`runners/hypervisor/`](runners/hypervisor/). Operator-pulled as well — the agent never pulls it for you, because a pull is a network fetch of executable content and that's the operator's call, not a job's. |

## License

MIT — see [LICENSE](LICENSE).
