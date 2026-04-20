# Infrastructure Management Dashboard — Community Edition

A self-hosted web dashboard for managing infrastructure across AWS, Azure, and
(optionally) on-prem VMware Workstation. Bring your own cloud credentials; the
dashboard deploys resources into **your** accounts.

> **Status:** private preview. This README is a placeholder — a full rewrite
> with screenshots and a demo video lands with the public release.

## Quick start (Windows)

1. Install prerequisites: [Docker Desktop](https://www.docker.com/products/docker-desktop/),
   [PowerShell 7](https://aka.ms/powershell), [git](https://git-scm.com/download/win).
2. Clone this repo.
3. Run the onboarding script:
   ```powershell
   .\scripts\Onboard-Dashboard.ps1
   ```
   On first run the script copies `.env.example` to `.env` and opens it in
   Notepad. Fill in your AWS and Azure credentials (instructions inline in
   the file), save, and rerun the script. It auto-generates the local
   secrets (JWT signing key, Postgres password, first-run admin password)
   and brings up the stack.
4. Log in with `admin` and the `FIRST_RUN_ADMIN_PASSWORD` value from `.env`.
   Change it from Settings after you log in.

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

Enable these in `.env` only if you have the backing infrastructure:

- `VMWARE_ENABLED` — VMware Workstation VM management (Windows host only;
  requires the Windows override — see [docs/ONBOARDING.md](docs/ONBOARDING.md)
  Appendix A)
- `BEYONDTRUST_ENABLED` — BeyondTrust Password Safe for secret retrieval
- `PORTAINER_ENABLED` — Portainer CE for on-prem Docker host management
- `ANSIBLE_ENABLED` — Ansible-based config-management jobs
- `ENTITLE_ENABLED` — Entitle approval-workflow integration
- `CHAT_ENABLED` — local LLM chat assistant (Ollama, opt-in `--profile chat`)

## License

TBD — see the repo's `LICENSE` file once the public release lands.
