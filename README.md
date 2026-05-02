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

## Quick start

**Windows** (PowerShell 7):

```powershell
.\scripts\Onboard-Dashboard.ps1
```

**macOS / Linux / WSL / Raspberry Pi** (bash):

```bash
./scripts/onboard.sh
```

The script checks prerequisites, generates bootstrap secrets (JWT signing
key + Postgres password), and brings up the Docker Compose stack. Your
browser opens automatically to a **setup wizard** where you create the
admin account and enter your cloud credentials. Credentials are encrypted
with AES-256 and stored in the database — nothing sensitive stays in any
file on disk.

> **WSL users:** Docker Desktop is not required. Install Docker Engine
> directly in your WSL distro (`sudo apt install docker.io` or follow the
> [official guide](https://docs.docker.com/engine/install/ubuntu/)), start
> it with `sudo service docker start`, then run `./scripts/onboard.sh`.
> The script detects WSL automatically and opens the dashboard in your
> Windows browser.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for the full walkthrough,
including AWS IAM setup, Azure service principal setup, and the
feature-test checklist. See [docs/secrets-management.md](docs/secrets-management.md)
for the credential storage model, external vault migration, and security
best practices.

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
- **Ansible provisioning runner** — run playbooks and assets (`.sh` scripts,
  `.rpm`/`.deb` packages) stored in S3, Azure Blob, or GCS against any target:
  on-premises hypervisors (Proxmox, vSphere, Hyper-V, Nutanix, XCP-ng) *or*
  cloud VMs (EC2, Azure VMs, GCE). The local Docker runner needs no extra
  infrastructure — assets are fetched from cloud storage and Ansible SSHes
  directly to the target. Cloud runners (ECS, ACI, GCP Cloud Run Jobs) are
  available when you need the runner network-local to the VM.
  See [docs/integrations/ansible.md](docs/integrations/ansible.md).
- **BeyondTrust Password Safe and/or PRA** — secret retrieval and session recording
- **BeyondTrust EPM for Linux (EPM-L)** — list and build agent packages, one-click sync of `.rpm`/`.deb` packages to your Ansible asset bucket, installation-token issuance for new endpoint registration. See [docs/integrations/epml.md](docs/integrations/epml.md).
- **Portainer CE** — on-prem Docker host management
- **Entitle** — approval-workflow integration
- **MCP server** — read-only AI client integration (Claude Desktop, Claude Code, Cursor…) via Personal Access Token; mounted at `/mcp`, no extra containers needed

## License

MIT — see [LICENSE](LICENSE).
