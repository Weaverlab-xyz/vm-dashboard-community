# Contributing

Thanks for your interest in improving the Infrastructure Management Dashboard.

## Repo model (important to know up front)

This repo (`vm-dashboard-community`) is the **upstream** for the dashboard's
shared code — the core app, AWS, Azure, auth, and feature-flag plumbing. A
separate **private** repo (`VM_DEMO_CLI`) tracks enterprise-only integrations
(BeyondTrust, Portainer, Ansible orchestration, internal approval workflows)
and our hosted-SaaS deploy code. The private repo pulls periodically from this
one.

Two practical implications:

1. **Shared code changes land here first.** Bug fixes or features in
   `web_dashboard/` (anything that would also exist in the private repo) should
   be opened as PRs against this repo. They flow downstream automatically on
   the next sync.
2. **Enterprise-only code doesn't belong here.** If a change requires a
   BeyondTrust API call, a Portainer client, an Ansible playbook invocation,
   or an Entitle approval check, it lives in the private repo, not here.
   Keep integrations behind the existing feature flags
   (`BEYONDTRUST_ENABLED`, `PORTAINER_ENABLED`, `ANSIBLE_ENABLED`,
   `ENTITLE_ENABLED`) — community code paths must function with all of them
   off.

## Making a change

1. Fork + branch from `main`.
2. Run [scripts/Onboard-Dashboard.ps1](scripts/Onboard-Dashboard.ps1) once
   so you have a working local stack to test against.
3. Make your change. Keep the diff tight — no drive-by refactors.
4. Smoke-test locally:
   - `docker compose up -d --build`
   - `curl http://localhost:8000/api/health` returns `200`
   - Your feature works end-to-end against your own AWS/Azure
5. Open a PR. Describe what changed and why; note any `.env.example` keys
   added.

## What to avoid

- Adding dependencies to [requirements.txt](requirements.txt) without a
  clear reason — every added package is one more thing users install.
- Introducing Windows-only assumptions in the base Compose file. Platform-
  specific bits go in `docker-compose.override.*.yml.example` templates.
- Hard-coding account IDs, subnet IDs, AMI IDs, or cloud region specifics
  in [terraform/](terraform/). Everything flows through variables.
- Landing a change that only works with an enterprise integration enabled.
  If the flag is off, the feature must be cleanly absent (route 404s, nav
  entry hidden, no warmer errors).

## Testing with integrations off

The community edition must run with every optional flag off. Before merging,
verify in a fresh `.env` with only `AWS_*` and `AZURE_*` set:

- `/api/features` returns all flags false
- `/api/bt/*`, `/vms`, `/containers`, `/config-mgmt`, `/images` all 404
- No `ImportError` or warmer errors in `docker compose logs app` except
  AWS/Azure auth failures (expected with dummy creds)

## Reporting issues

Open a GitHub issue with:

- Output of `.\scripts\Onboard-Dashboard.ps1`
- Last 100 lines of `docker compose logs app`
- OS / Docker Desktop / PowerShell versions
- Expected vs. actual behavior

**Do not paste `.env` contents** — they contain cloud credentials.
