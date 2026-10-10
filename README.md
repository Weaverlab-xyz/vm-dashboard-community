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
> See [docs/editions/comparison.md](docs/editions/comparison.md) for how it compares.

## Who this is for

Every page under [`docs/`](docs/README.md) opens with the same line: who it is for,
which install profile it applies to, and the situation that should send you to it.
Five readers, five ways in:

| You are… | Start here |
|---|---|
| **installing or running** the dashboard | [Onboarding](docs/ONBOARDING.md), then [Where everything is](#where-everything-is) |
| **running your own infrastructure** with it | [Where everything is](#where-everything-is) — you want the `demo` profile, whatever its name suggests |
| **showing** it to someone | [Demo profile](docs/profiles/demo/README.md) — one page per role, plus the OT demo cell |
| running **customer proof-of-value** work | [POV profile](docs/profiles/pov/README.md) |
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

1. [Infrastructure as Code](docs/cloud/infrastructure-as-code.md): a Terraform run per
   deploy, per-job state, idempotent destroy, and where Packer and the sandbox
   bootstrappers fit.
2. [Config Management](docs/operations/config-management.md): why one-shot, ephemeral
   runners are the security argument, and how on-prem and cloud targets are reached.
3. [Secrets Management](docs/access/secrets-management.md): encrypted database, then an
   external vault, then runtime checkout from a vault, and why the JWT root key can't move.
4. [Storage Management](docs/operations/storage-management.md): four backends, and why a
   backend is a deployment-level choice rather than a per-feature one.
5. [Image Management](docs/cloud/image-management.md): build an image once, then promote it
   to the other clouds.

Together they're the philosophy of the tool: **declarative, version-controlled,
idempotent, ephemeral where it should be and persistent where it must be**. The features in
the rest of this README make sense in that frame.

## Where everything is

The docs are grouped into sections. Each one has a front page that says which page
answers what. The full index of every page is [docs/README.md](docs/README.md).

| Section | Read this when |
|---|---|
| [Onboarding](docs/ONBOARDING.md) | you're setting the dashboard up for the first time, and want the per-cloud setup and the feature-test checklist |
| [Cloud](docs/cloud.md) | you're putting workloads on AWS, Azure, GCP or OCI: VMs, containers, virtual desktops, Active Directory in the cloud, images and costs |
| [Databases](docs/databases.md) | you're provisioning Postgres, MySQL, SQL Server, Oracle or MongoDB, or registering a database you already run |
| [Kubernetes](docs/kubernetes.md) | you're managing clusters and the privileged access into them: managed EKS / AKS / GKE / OKE, on-prem k3s, or single-node KubeSolo |
| [Inventory](docs/inventory.md) | you want one list across every cloud and hypervisor, filtered by tag or Password Safe attribute, and to act on a selection |
| [Remote Agents](docs/remote-agents.md) | your hypervisors, databases, directories or clusters live somewhere the dashboard can't reach |
| [Identity and access](docs/access.md) | you're deciding who and what may use the dashboard, how it keeps credentials, or how you would prove what happened |
| [Directories](docs/directories.md) | you want Windows servers joined to an Active Directory domain, to manage on-prem AD or LDAP, or to manage group membership in Entra ID, Okta or PingOne |
| [OIDC and single sign-on](docs/oidc.md) | you want single sign-on, Dex in front of your clusters, Entra federation to Kubernetes, or the dashboard reaching the clouds with its own short-lived identity |
| [Workload Lab](docs/workload-lab.md) | something that isn't a person needs a credential (a certificate, a SPIFFE identity, a cluster token or a cloud key) and you want to pick the mechanism |
| [Operations](docs/operations.md) | the dashboard is running and you're managing it day to day: scheduling and change windows, Config Management, the job worker, notifications, the auto-delete timer, hosting it in a cloud |
| [Integrations](docs/integrations/README.md) | you're connecting an external system: the BeyondTrust products, the hypervisors, Rancher, Portainer, Cloud Functions, the Ansible runners, the MCP server |
| [Demo and POV profiles](docs/profiles/README.md) | you're presenting to a particular role (the [Personas](docs/profiles/demo/personas/README.md), one page per role), or running customer proof-of-value work on a separate instance |
| [Editions](docs/editions.md) | you're choosing between running the dashboard yourself and a hosted edition |

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
  live as soon as an issuer is configured; see [docs/oidc.md](docs/oidc.md). The older
  Sign in with Microsoft button is still there.
- **Service accounts** — OAuth 2.0 client credentials for CI jobs, scripts and agents,
  instead of handing them a personal token; see
  [docs/access/service-accounts.md](docs/access/service-accounts.md)
- **Audit log** — who did what, and a way to check the record has not been edited; see
  [docs/access/audit-log.md](docs/access/audit-log.md)
- **Jobs** — background task tracking with live WebSocket updates

## What's optional (feature-flagged, off by default)

Enable these on the **setup wizard's Feature Flags step** or in **Settings →
Integrations** after first login — only if you have the backing infrastructure.
The wizard turns a flag on; the per-integration fields live in Settings:

- **VMware Workstation** — VM management (Windows host only; needs a remote
  agent on the Workstation host — see [docs/integrations/vmware.md](docs/integrations/vmware.md))
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
  runner is one-shot — see [docs/operations/config-management.md](docs/operations/config-management.md)
  for the security argument. Integration setup in
  [docs/integrations/ansible.md](docs/integrations/ansible.md).
- **Remote Agents** — an outbound-dialing agent you run inside a private
  network so the dashboard can manage what it can't route to: hypervisor
  discovery and inventory, agent-executed Config Management, and Hyper-V /
  bare-ESXi access via a one-shot sibling container. The agent holds the
  credentials; the dashboard never needs a path in. See
  [docs/remote-agents.md](docs/remote-agents.md).
- **Cloud Databases** — provision private Postgres / MySQL / SQL Server on
  AWS/Azure/GCP, Oracle on AWS RDS or OCI, and MongoDB on Atlas, brokered through a PRA tunnel, or register a
  database you already run (on-premises included) as a Config Management
  target. See [docs/databases.md](docs/databases.md).
- **Kubernetes** — provision or import EKS / AKS / GKE / OKE, run a Rancher
  management plane, deliver secrets via ESO, and layer PRA tunnels, Password
  Safe token rotation and Entra→RBAC federation on top. See
  [docs/kubernetes.md](docs/kubernetes.md).
- **Cloud Costs** (`cost_explorer_enabled`) — month-to-date spend for every configured
  cloud on a `/costs` page and a home-page tile, with budgets and alerts: AWS Cost
  Explorer, Azure Cost Management, GCP's BigQuery billing export and OCI's Usage API. See
  [docs/cloud/costs.md](docs/cloud/costs.md).
- **BeyondTrust Password Safe** — on-demand checkout of SSH keys and passwords, plus onboarding of the VMs, databases and Kubernetes tokens the dashboard builds as managed systems + accounts. See [docs/integrations/beyondtrust/password-safe.md](docs/integrations/beyondtrust/password-safe.md).
- **BeyondTrust Privileged Remote Access** — Shell Jump, Web Jump, Remote RDP and protocol-tunnel jump items plus PRA Vault accounts, and the Gateway hosts they broker through. See [docs/integrations/beyondtrust/privileged-remote-access.md](docs/integrations/beyondtrust/privileged-remote-access.md).
- **BeyondTrust EPM for Linux (EPM-L)** — list and build agent packages, one-click sync of `.rpm`/`.deb` packages to your Ansible asset bucket, installation-token issuance for new endpoint registration. See [docs/integrations/beyondtrust/epml.md](docs/integrations/beyondtrust/epml.md).
- **Portainer CE** — on-prem Docker host management, through a Portainer you run or one
  the dashboard deploys for you. See [docs/integrations/portainer.md](docs/integrations/portainer.md).
- **Entitle** — just-in-time access: register what the dashboard builds (VMs, databases,
  clusters, Rancher, Portainer) as Entitle resources, and grant dashboard permissions
  themselves for a limited time. See [docs/integrations/beyondtrust/entitle.md](docs/integrations/beyondtrust/entitle.md).
- **Directories** (`directories_enabled`, preview) — build or register Active Directory in
  AWS, Azure and GCP and join Windows servers to it, manage on-prem AD and LDAP through a
  remote agent, and browse and change group membership in Entra ID, Okta and PingOne. See
  [docs/directories.md](docs/directories.md).
- **Virtual Desktops** (`vdesktops_enabled`, preview) — pools of private desktop VMs that
  people reach through the PRA Gateway. See [docs/cloud/virtual-desktops.md](docs/cloud/virtual-desktops.md).
- **Cloud Functions** (`cloud_functions_enabled`) — one Python handler deployed as an AWS
  Lambda, Azure Function App or GCP Cloud Run function: a stable HTTPS endpoint inside your
  network, including the Entitle adapters. See [docs/integrations/cloud-functions.md](docs/integrations/cloud-functions.md).
- **Workload Lab** (`cert_lab_enabled`, `spire_lab_enabled`, `agentcell_enabled`,
  `workload_credentials_enabled`) — credentials for things that are not people: a
  private CA, SPIFFE identities, bound Kubernetes tokens, short-lived cloud keys. See
  [docs/workload-lab.md](docs/workload-lab.md).
- **Notifications** (`notifications_enabled`, dry-run by default) — outbound webhooks to
  Slack, Microsoft Teams or anything that takes signed JSON, for auto-delete warnings, job
  failures and budget, secret and drift alerts. See [docs/operations/notifications.md](docs/operations/notifications.md).
- **MCP server** (`mcp_server_enabled`) — read-only AI client integration (Claude Desktop, Claude Code, Cursor…) via Personal Access Token; mounted at `/mcp`, no extra containers needed. Each tool returns only what the token's owner can see in the UI. See [docs/integrations/mcp-server.md](docs/integrations/mcp-server.md)
- **Unmanaged VM discovery** (`cloud_unmanaged_discovery_enabled`) — show cloud VMs this dashboard did not deploy, in a separate list per cloud console. Discovered VMs can be started and stopped; they can never be destroyed from here. Off by default because it lists every instance in the account rather than the ones the dashboard deployed. A discovered VM is admin-only unless it carries a `workgroup` tag.
- **Action Guardrails** — pre-action policy gate (OPA): evaluate every deploy against Rego policies *before* the job starts — allowed regions, blocked instance sizes, change-freeze windows — and block disallowed ones (403, audited). Fails closed. See [docs/operations/scheduling/policy-guardrails.md](docs/operations/scheduling/policy-guardrails.md).

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
