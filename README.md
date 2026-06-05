# Infrastructure Management Dashboard — Community Edition

A self-hosted web dashboard for managing infrastructure across AWS, Azure, GCP,
and (optionally) on-prem infrastructure (VMware, Hyper-V, Proxmox, Nutanix). Bring your own cloud credentials;
the dashboard deploys resources into **your** accounts.

> **Looking for a hosted version?** A managed SaaS edition is on the roadmap.
> It removes the on-disk JWT root key by fetching it from Azure Key Vault via
> a workload-managed identity (OIDC federation, no static credentials), and
> adds multi-tenant isolation, automatic rotation, and managed upgrades.
> See [docs/saas-comparison.md](docs/saas-comparison.md) for how it compares.

> **Status:** private preview. This README is a placeholder — a full rewrite
> with screenshots and a demo video lands with the public release.

## How the dashboard thinks

Before you spin it up, the four reference docs below explain the
opinions baked into the codebase — what the dashboard does *for* you,
and what discipline it expects from you. Read them in order if you're
new to the tool; skim if you already know how this kind of platform
works:

| Doc | What's in it | Read this when |
|---|---|---|
| [Infrastructure as Code](docs/infrastructure-as-code.md) | The Terraform-per-deploy model, per-job state, idempotent destroy, where Packer + the sandbox bootstrappers fit. | You're about to deploy your first cloud VM and want to know what's actually running underneath. |
| [Image Management](docs/image-management.md) | Build-once-promote-many lifecycle: build a portable VHD in one cloud, hub it in your designated storage backend, run **one-click cross-cloud promote** via a transient runner in the target cloud (ECS / ACI / Cloud Run). | You're about to build a custom image and need to know how it'll reach the other clouds. |
| [Config Management](docs/config-management.md) | Why one-shot ephemeral runners are the security argument, the .yml/.sh/.ps1/.rpm/.deb wrap rules, on-prem vs cloud target paths. | You're about to run an Ansible job and want to know how the runner handles secrets and isolation. |
| [Secrets Management](docs/secrets-management.md) | Tier 1 (encrypted DB) → Tier 2 (external vault) → Tier 3 (vault-backed runtime checkout); migration UI; why the JWT root key can't move. | You're deciding where to store cloud credentials and how to evolve that over time. |
| [Storage Management](docs/storage-management.md) | Four backends (S3, Azure Blob, GCS, Local/UNC); migration; why backends are a deployment-level concern, not a per-feature one. | You're about to enable the Ansible feature flag — storage is a prerequisite. |

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
browser opens automatically to a **setup wizard** where you create the admin
account and enter your cloud credentials. Credentials are encrypted with
AES-256 and stored in the database — nothing sensitive stays in any file on
disk.

> **Just want to kick the tyres without cloning the repo?** Drop
> `docker-compose.hub.yml` and `.env.example` into an empty folder, copy
> `.env.example` to `.env` and set `POSTGRES_PASSWORD`, generate a stable key
> with `openssl rand -hex 32 > .jwt_secret_key`, then
> `docker compose -f docker-compose.hub.yml up -d`.

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
- **Identity** — local username/password, optional WebAuthn/FIDO2 MFA,
  optional Sign in with Microsoft (Entra ID)
- **Jobs** — background task tracking with live WebSocket updates

## What's optional (feature-flagged, off by default)

Enable these in the **setup wizard → Step 5** or **Settings → Integrations**
after first login — only if you have the backing infrastructure:

- **VMware Workstation** — VM management (Windows host only; requires the
  Windows Compose override — see [docs/ONBOARDING.md](docs/ONBOARDING.md) Appendix A)
- **Proxmox VE** — VM and node management via the Proxmox REST API
- **VMware vSphere / ESXi** — VM power operations and inventory via SSH/API
- **Microsoft Hyper-V** — VM management via WinRM
- **Nutanix AHV** — VM management via Prism Central REST API
- **XCP-ng / XenServer** — VM management via XAPI
- **Ansible provisioning runner** — run playbooks (`.yml`) and provisioning
  assets (`.sh`, `.ps1`, `.rpm`, `.deb`) against any target: on-premises
  hypervisors (Proxmox, vSphere, Hyper-V, Nutanix, XCP-ng) *or* cloud VMs
  (EC2, Azure VMs, GCE). Assets live in storage you configure on `/storage`
  (AWS S3 / Azure Blob / GCS / Local-or-UNC); the runner can be local Docker
  for any target reachable from the dashboard host, or AWS ECS / Azure ACI /
  GCP Cloud Run Jobs for VMs in private cloud subnets. Every runner is
  one-shot — see [docs/config-management.md](docs/config-management.md) for
  the security argument. Integration setup in
  [docs/integrations/ansible.md](docs/integrations/ansible.md).
- **BeyondTrust Password Safe and/or PRA** — secret retrieval and session recording
- **BeyondTrust EPM for Linux (EPM-L)** — list and build agent packages, one-click sync of `.rpm`/`.deb` packages to your Ansible asset bucket, installation-token issuance for new endpoint registration. See [docs/integrations/epml.md](docs/integrations/epml.md).
- **Portainer CE** — on-prem Docker host management
- **Entitle** — approval-workflow integration
- **MCP server** — read-only AI client integration (Claude Desktop, Claude Code, Cursor…) via Personal Access Token; mounted at `/mcp`, no extra containers needed

## License

MIT — see [LICENSE](LICENSE).
