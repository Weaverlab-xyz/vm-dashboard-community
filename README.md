# Infrastructure Management Dashboard — Community Edition

A self-hosted web dashboard for managing infrastructure across AWS, Azure, and
(optionally) on-prem VMware Workstation. Bring your own cloud credentials; the
dashboard deploys resources into **your** accounts.

> **Status:** private preview. This README is a placeholder — a full rewrite
> with screenshots and a demo video lands with the public release.

## Quick start

**Windows** (PowerShell 7):

```powershell
.\scripts\Onboard-Dashboard.ps1
```

**macOS / Linux / Raspberry Pi** (bash):

```bash
./scripts/onboard.sh
```

The script checks prerequisites, generates bootstrap secrets (JWT signing
key + Postgres password), and brings up the Docker Compose stack. Your
browser opens automatically to a **setup wizard** where you create the
admin account and enter your AWS and Azure credentials. Credentials are
encrypted with AES-256 and stored in the database — nothing sensitive
stays in any file on disk.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for the full walkthrough,
including AWS IAM setup, Azure service principal setup, and the
feature-test checklist.

## What's included

- **AWS** — EC2 deployment, AMI browsing, image capture, SSH-key management
- **Azure** — VM deployment (Marketplace + private images), Shared Image
  Gallery, Azure Container Instances
- **Identity** — local username/password, optional WebAuthn/FIDO2 MFA,
  optional Sign in with Microsoft (Entra ID)
- **Jobs** — background task tracking with live WebSocket updates

## What's optional (feature-flagged, off by default)

Enable these in the **setup wizard → Step 4** or **Settings → Integrations**
after first login — only if you have the backing infrastructure:

- **VMware Workstation** — VM management (Windows host only; requires the
  Windows Compose override — see [docs/ONBOARDING.md](docs/ONBOARDING.md) Appendix A)
- **BeyondTrust Password Safe and/or PRA** — secret retrieval and session recording
- **Portainer CE** — on-prem Docker host management
- **Ansible** — config-management jobs via local Docker or AWS ECS
- **Entitle** — approval-workflow integration
- **Chat (Ollama)** — local LLM assistant (also requires `docker compose --profile chat up -d`)

## License

TBD — see the repo's `LICENSE` file once the public release lands.
