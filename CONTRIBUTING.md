# Contributing

Thanks for your interest in improving the Infrastructure Management Dashboard.

## Repo model

This repo (`vm-dashboard-community`) is the **upstream** for the dashboard's
shared code — the core app, AWS, Azure, GCP, auth, jobs, and feature-flag
plumbing. A separate **private** repo tracks internal/enterprise-only code:
custom integrations, hosted-deployment tooling, and any work that depends
on commercial APIs we can't ship publicly. The private repo periodically
syncs from this one.

Two practical implications:

1. **Shared code changes land here first.** Bug fixes or features in
   `web_dashboard/` (anything that would also exist in the private repo)
   should be opened as PRs against this repo. They flow downstream
   automatically on the next sync.
2. **Enterprise-only code doesn't belong here.** If a change requires a
   BeyondTrust Password Safe or PRA API call, a Portainer client, an
   Ansible playbook invocation against shared infra, or an Entitle
   approval check, it lives in the private repo. *(Exception: BeyondTrust
   EPM for Linux — `api/epml.py` and `services/epml_service.py` — is
   community-edition code gated by `BEYONDTRUST_ENABLED`.)* Keep
   integrations behind the existing feature flags
   (`BEYONDTRUST_ENABLED`, `PORTAINER_ENABLED`, `ANSIBLE_ENABLED`,
   `ENTITLE_ENABLED`) — community code paths must function with all of
   them off.

### Where SaaS fits

The hosted SaaS edition is **not** this repo. It builds on the same core
but adds capabilities the community edition deliberately doesn't ship:

- **Multi-tenancy** — tenant isolation, per-tenant Key Vaults, no
  shared filesystem.
- **Security uplift** — workload-identity-bootstrapped JWT root key (no
  static credentials anywhere), audit logs for root-key access, managed
  rotation. See [docs/saas-comparison.md](docs/saas-comparison.md) for
  the detailed walkthrough.
- **AI helper services** — e.g. an AI-assisted Ansible playbook
  generator, hosted as a tenant-scoped service rather than embedded in
  the dashboard image.

If your change is one of those SaaS-only capabilities, it lands in the
hosted codebase, not here. PRs against this repo that fold in
multi-tenant assumptions, hosted-only auth flows, or AI-helper
plumbing will be redirected.

## Making a change

1. Fork + branch from `main`.
2. Bring up a local stack so you can test against it. Either onboarder
   works:
   ```bash
   ./scripts/onboard.sh                 # bash — WSL / Linux / macOS
   ```
   ```powershell
   .\scripts\Onboard-Dashboard.ps1      # PowerShell — Windows
   ```
3. Make your change. Keep the diff tight — no drive-by refactors.
4. Smoke-test locally:
   - `docker compose up -d --build`
   - `curl http://localhost:8001/api/health` returns `200`
     *(The community Compose file binds host port 8001 to coexist with
     other local dashboards on 8000; see `docker-compose.yml`.)*
   - Your feature works end-to-end against your own AWS / Azure / GCP
     account.
5. Open a PR. Describe what changed and why; note any `.env.example`
   keys added.

### Faster lab infra for testing

If your change touches AWS / Azure / GCP integration code, the
[sandbox bootstrappers](docs/CLOUD_SANDBOX.md) can stand up isolated
lab infra in any cloud with one command and tear it down with another.
Saves the manual VPC/IAM/SP setup you'd otherwise repeat per-PR. Both
bash (`scripts/sandbox/Linux/`) and PowerShell
(`scripts/sandbox/Windows/`) variants exist — match whichever shell
you're working in.

## What to avoid

- Adding dependencies to [web_dashboard/requirements.txt](web_dashboard/requirements.txt)
  without a clear reason — every added package is one more thing users
  install. Keep dependency floors loose (`>=`) unless there's a known
  incompatibility.
- Introducing platform-specific assumptions in the base Compose file or
  in shipping scripts. If you add an automation script, ship both a bash
  and a PowerShell variant when possible (see `scripts/sandbox/` for
  the pattern). At minimum, document the platform a script targets.
- Hard-coding account IDs, subnet IDs, AMI IDs, or cloud region
  specifics in [terraform/](terraform/). Everything flows through
  variables.
- Landing a change that only works with an enterprise integration
  enabled. If the flag is off, the feature must be cleanly absent
  (route 404s where appropriate, nav entry hidden, no warmer errors).
- Adding multi-tenant assumptions or hosted-only auth flows. Those
  belong in the SaaS codebase.

## Testing with integrations off

The community edition must run with every optional flag off. Before
merging, verify in a fresh `.env` with only `AWS_*`, `AZURE_*`,
`GCP_*` set:

- `/api/features` returns all optional flags `false`.
- Feature-gated pages 404:
  - `/vms` (vmware_enabled)
  - `/proxmox`, `/vsphere`, `/hyperv`, `/nutanix`, `/xcpng`
  - `/config-mgmt` (ansible_enabled)
- Always-on pages render: `/`, `/aws`, `/azure`, `/gcp`, `/jobs`,
  `/users`, `/groups`, `/settings`, `/secrets`, `/containers`.
  *(`/containers` shows On-Premises / Cloud tabs; the on-prem
  Portainer tab self-gates on `portainer_enabled` per-call.)*
- No `ImportError` or warmer errors in `docker compose logs app`
  except cloud-auth failures (expected with placeholder creds).

## Reporting issues

Open a GitHub issue with:

- The onboarder output (`./scripts/onboard.sh` or
  `.\scripts\Onboard-Dashboard.ps1`).
- Last 100 lines of `docker compose logs app`.
- OS / Docker / shell version (`docker version`, `bash --version` or
  `$PSVersionTable`).
- Expected vs. actual behaviour.

**Do not paste `.env` contents** — they contain cloud credentials.
