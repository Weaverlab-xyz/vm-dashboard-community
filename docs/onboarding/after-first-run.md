# After the first run

> **Audience:** operator · **Profile:** `both` · **Read this when:** the dashboard is running and you need to scale the worker, change credentials, or protect the JWT key.

Part of the [Onboarding Guide](../ONBOARDING.md). The things you do once the
dashboard is up and serving.

### Scaling the job worker

Long jobs — Kubernetes and cloud-database provisions, Packer image builds, and
image export/promote — run in the dedicated `worker` container, not the web app.
**Each worker runs one such job at a time**, so if quick jobs pile up behind a
long one (e.g. an export waiting on a 25-minute cluster provision), run more
workers. The job queue claims each job atomically, so extra workers never
double-run a job.

The stack starts **3 workers** by default (the onboard scripts migrate installs
still pinned at the old default of 1). Change the count in `.env`, then bring the
stack up as usual:

```bash
WORKER_REPLICAS=5          # in .env — number of long jobs that can run at once
./scripts/onboard.sh       # (keep --hub if you used it)
```

Or scale ad-hoc without editing `.env`:

```bash
docker compose up -d --scale worker=5          # add -f docker-compose.hub.yml if you used --hub
```

`WORKER_CPU_LIMIT` / `WORKER_MEM_LIMIT` (also in `.env`) cap each worker's CPU and
memory so several concurrent heavy jobs can't exhaust the host — tune them to your
machine. Defaults: 3 workers, 2 CPUs and 2 GB each (3 busy workers can use up to
~3× those limits, so lower `WORKER_REPLICAS` on small hosts).


### Reconfiguring credentials after first run

To update credentials or toggle feature flags after setup, navigate to
`/setup` in your browser while logged in as admin. The wizard reopens in
reconfigure mode: existing values are pre-filled, and leaving a secret
field blank keeps the stored value unchanged.


### Protect and back up the JWT key

`.jwt_secret_key` is the root of trust for the entire application — every
integration credential stored in the database is encrypted with a key derived
from it. The onboard script protects it with owner-only filesystem permissions
and mounts it into the container as a Docker secret.

**In the community edition this key cannot be migrated to a cloud vault.** It's
the bootstrap key that decrypts the encrypted database — *including* the
credentials the dashboard would need to reach any vault — so there's no startup
ordering that lets it live in a vault (see
[Why the JWT root key cannot be migrated](../secrets-management.md#why-the-jwt-root-key-cannot-be-migrated)).
At startup the dashboard reads it from `JWT_SECRET_KEY_FILE` → the
`/run/secrets/jwt_key` Docker secret → the `JWT_SECRET_KEY` env var, in that order.

So, for the community edition:

- **Back it up** somewhere safe (password manager, encrypted drive). Lose it and
  every stored credential is unrecoverable and the app won't start — see
  [JWT key file: backup and loss recovery](troubleshooting.md#jwt-key-file-backup-and-loss-recovery) below.
- **Don't commit it** — it's gitignored and excluded from the image build context.
- On shared or long-lived hosts, restrict OS access to the file; the host's
  filesystem permissions (or the Docker secret mount) are the security boundary.

> *Integration credentials* (your AWS/Azure/GCP keys) **can** be moved into an
> external vault — AWS Secrets Manager, Azure Key Vault, GCP Secret Manager, or
> BeyondTrust Secrets Safe — from **Settings → Secrets Backend** (`/secrets`).
> That's a separate feature from the root key; see
> [`docs/secrets-management.md`](../secrets-management.md). Removing the on-disk root
> key entirely (fetched at boot via cloud workload identity) is on the
> **SaaS-edition roadmap** — see [`docs/saas-comparison.md`](../saas-comparison.md).


### Platform notes

- **WSL (Windows Subsystem for Linux):** Docker Desktop is not required.
  Install Docker Engine inside your WSL distro, start it with
  `sudo service docker start`, then run `./scripts/onboard.sh`. The
  script detects WSL automatically: it prints WSL-specific hints if the
  daemon isn't running, and opens the dashboard in your Windows-side
  browser (via `wslview` if installed, otherwise `cmd.exe /c start`).
  Ports from WSL2 are automatically forwarded to Windows, so
  `http://localhost:8001` works in your Windows browser without any extra
  configuration.
- **Apple Silicon (M1/M2/M3/M4):** Docker images build natively as
  `linux/arm64` — no platform flag needed. The same applies to
  Raspberry Pi 5 (ARM64).
- The **VMware Workstation** feature flag
  ([guide](../integrations/vmware.md)) is Windows host-only;
  do not enable it on macOS, Linux, or WSL. The **VMware vSphere / ESXi**
  flag ([guide](../integrations/vsphere.md)) connects to a remote vCenter/ESXi host and
  works on any
  OS.
- The optional **MCP server** ([guide](../integrations/mcp-server.md)) needs no extra
  containers —
  it runs inside the main app and is always available once the stack is up.
- **Portainer**, **Remote Worker** (Ansible + Kubernetes runners),
  **Proxmox VE**, **VMware vSphere / ESXi**,
  **Microsoft Hyper-V**, **Nutanix AHV**, **XCP-ng / XenServer**, and
  **Entitle** are optional integrations with their own backing infrastructure.
  See the detailed guides in [`docs/integrations/`](../integrations).
- **Secrets management** — how credentials are encrypted, how to migrate to an
  external vault, and security best practices: [`docs/secrets-management.md`](../secrets-management.md).
- **Storage management** — where playbooks and asset files live, and how to
  migrate between cloud object stores or a corporate file share:
  [`docs/storage-management.md`](../storage-management.md).
- **Config management** — philosophy, best practices, and how the
  dashboard's ephemeral-runner approach reduces secret sprawl:
  [`docs/config-management.md`](../config-management.md).
- **Infrastructure as code** — how cloud VMs, Shell Jumps, and images
  are provisioned through Terraform/Packer modules with per-job state:
  [`docs/infrastructure-as-code.md`](../infrastructure-as-code.md).
- **Image management** — the build-once-promote-many lifecycle: build
  a portable image artefact, store it in your storage backend, then
  promote it to AWS / Azure / GCP:
  [`docs/image-management.md`](../image-management.md).
- **Hosting it in a cloud instead of on this host** — Azure Container Apps,
  Cloud Run or ECS, with a gateway sidecar that keeps the agent endpoint
  public and the UI private. Note that a cloud-hosted dashboard has no route
  to on-premises hypervisors and cannot use the local Ansible runner:
  [`docs/cloud-hosting.md`](../cloud-hosting.md).
- **Moving configuration to a second instance** — every Settings value, without
  re-typing it: [`docs/config-migration.md`](../config-migration.md).

---
