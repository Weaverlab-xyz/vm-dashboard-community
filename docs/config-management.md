# Config Management

This document explains how the dashboard does config management — the
philosophy that drives the design, the best practices the codebase
encodes, and how the on-premises and cloud paths fit together.

The companion docs:

- [Infrastructure as Code](infrastructure-as-code.md) — how the dashboard
  stands the infra up in the first place
- [Image Management](image-management.md) — what's *on* the VMs
  before config-management runs against them
- [Secrets Management](secrets-management.md) — where credentials live
- [Storage Management](storage-management.md) — where playbooks and
  assets live

---

## Philosophy

Config management lives or dies on three principles. We try to bake all
three into the dashboard rather than leave them as user discipline.

**1. Declarative, not procedural.** You describe the state you want; the
runner figures out whether work is needed. A playbook that says
"`nginx` should be installed and running" is right whether nginx is
already there or not. A shell script that says "`apt install nginx &&
systemctl start nginx`" is wrong on the second run, on a yum system, on
a host that's already serving traffic.

**2. Version-controlled assets.** Playbooks and scripts are code. They
go in source control, get reviewed, get rolled back when wrong. The
dashboard's storage layer is the runtime distribution channel — your
git history is still authoritative. Enable versioning on the underlying
bucket (S3, Azure Blob, GCS all support it) so the runtime store is
also recoverable.

**3. Separation of *what* from *where*.** The asset describes the desired
state. The inventory says which hosts to apply it to. Mixing the two
("install nginx on web-01") couples the two and turns a 20-host fleet
into 20 unique playbooks.

---

## How the dashboard implements these

| Principle | Where it shows up |
|---|---|
| Declarative | `.yml` / `.yaml` playbooks run as-is via Ansible's idempotent modules. `.sh` / `.ps1` / `.rpm` / `.deb` are auto-wrapped in a generated playbook that uses idempotent built-in modules (`copy`, `script`, `dnf`, `apt`, `win_script`). |
| Version-controlled | Assets are uploaded to the [storage backend](storage-management.md) you select. Bucket versioning + your own git remote together give you history. The dashboard never overwrites blindly — every upload is a new object at the same key. |
| Separation of what/where | The Config Management page (`/config-mgmt`) picks **what** (asset) and **where** (inventory group or cloud target) independently. The same playbook can target the on-prem `proxmox` group, an EC2 instance by IP, or both. |

---

## The two paths

The dashboard surfaces two distinct execution paths, both reaching the
same `Config Management` page.

### On-premises hypervisors

If you've enabled any of the on-prem hypervisor integrations (Proxmox VE,
vSphere/ESXi, Hyper-V, Nutanix AHV, XCP-ng, VMware Workstation), the
dashboard auto-builds an Ansible inventory from each integration's
configured host list. Targets appear in the run-asset dropdown as group
keys (e.g. `proxmox`, `vsphere`).

Behind the scenes (see [`services/ansible_local_service.py`](../web_dashboard/services/ansible_local_service.py)):

- `build_inventory()` returns a JSON inventory grouped by hypervisor type,
  with hostvars wired for the right Ansible connection plugin
  (`ansible_connection=ssh` or `winrm` depending on the hypervisor).
- `get_configured_targets()` powers the UI dropdown — only enabled +
  configured hypervisors appear, no empty groups.
- The Local Docker runner (default) executes playbooks against this
  inventory directly from the dashboard host.

This is where contributors with on-prem labs help most — see
[CONTRIBUTING.md → Where the community can help most](../CONTRIBUTING.md#where-the-community-can-help-most).

### Cloud providers (AWS / Azure / GCP)

Cloud VMs you've deployed via the dashboard appear in the same target
dropdown, prefixed with `aws:`, `azure:`, or `gcp:`. Picking one tells
the runner three things:

- **Where the target is** — the bare IP or hostname (the prefix is
  stripped before the runner sees it).
- **Whose SSH key opens the door** — fetched from the matching cloud's
  secret store at run time. AWS Secrets Manager, Azure Key Vault, or
  GCP Secret Manager. The fetch is JSON-aware: it handles either a raw
  PEM string or a `{public_key, private_key}` envelope.
- **Which user to log in as** — the per-cloud SSH user (`ansible_aws_user`,
  `ansible_azure_user`, `ansible_gcp_user`) drives the default; the run
  form lets the operator override per job. Stock-image conventions
  (`ec2-user` / `azureuser` / `gcp-user`) are pre-populated.

Cloud runs can use any of the four runners — the choice mostly affects
*where the Ansible process executes*, not the playbook semantics. See
the runner section below.

---

## Asset types

| Extension | Type | How the runner handles it |
|---|---|---|
| `.yml`, `.yaml` | Ansible playbook | Run as-is. Full Ansible feature surface available. |
| `.sh` | Shell script | Auto-wrapped: `ansible.builtin.script` against the target with `executable: /bin/bash`. The script itself runs once on the remote and exits. |
| `.ps1` | PowerShell script | Auto-wrapped: `ansible.windows.win_script`. Targets must have `ansible_connection=winrm` in their inventory hostvars (Hyper-V hostvars already do this). |
| `.rpm` | RPM package | Auto-wrapped: copy to `/tmp` + `ansible.builtin.dnf` install. |
| `.deb` | DEB package | Auto-wrapped: copy to `/tmp` + `ansible.builtin.apt` install. |

The auto-wrap path is a **convenience for one-off operations**, not a
substitute for proper playbook authoring. If you find yourself writing
the same `.sh` script three times with different targets, that's a
signal to write a real `.yml` playbook with `vars` and `when` clauses.

Need a starting point? Ready-to-adapt Linux and Windows playbooks live in
[`examples/playbooks/`](../examples/playbooks/).

---

## Runners

Where the Ansible process actually runs. Picked in
**Settings → Ansible → Runner**.

| Runner | Where it runs | Best for |
|---|---|---|
| **Local Docker** | Inside the dashboard container's Docker context. Uses a side-car `willhallonline/ansible` container per run. | On-prem hypervisor targets; corporate-network targets; anything reachable from the dashboard host. |
| **AWS ECS Fargate** | A Fargate task launched per run in your VPC. | EC2 targets in private subnets without a path back to the dashboard host. |
| **Azure ACI** | An Azure Container Instance per run, in your VNet. | Azure VMs in private subnets. |
| **GCP Cloud Run Jobs** | A Cloud Run Job per run, in your project. | GCE instances. |

The cloud runners exist because connecting from a dashboard sitting on
a corporate LAN to a deeply-private cloud subnet is often impossible
without a VPN. Running the playbook *inside* the cloud avoids that
network problem at the cost of one Fargate task / ACI / Cloud Run
invocation per run.

**The runner choice constrains the storage choice.** The runner has to
fetch the asset before executing it, which means the runner needs network
reachability to the storage backend. Two combinations don't work:

| Storage | Runner | Outcome |
|---|---|---|
| Local Filesystem / UNC | ECS / ACI / Cloud Run | Refused (the cloud runner has no path back to the corporate file server). The dashboard surfaces this as a disabled radio + 400 on the API. |
| Cloud bucket (S3 / Blob / GCS) | Local Docker | Works fine. Fetches go out over the dashboard host's normal egress. |

Most teams pair Local Docker + Local Filesystem (or a cloud bucket) for
on-prem labs, and one of the cloud runners + a cloud bucket for cloud
fleets.

### Why one-shot runners (the security argument)

Every runner — Local Docker, ECS Fargate, ACI, Cloud Run Jobs — is
**ephemeral by design**. A new container is spawned per run; it
executes the playbook; it exits and is destroyed. Nothing persists
between runs.

This is deliberate, and it matters. Long-lived runners are a known
weakness in CI/CD and config-management estates: they accumulate
secrets in environment variables, cached SSH keys in `~/.ssh`,
remembered hosts in `known_hosts`, leftover state from previous runs.
A single compromise of a long-lived runner can yield credentials that
have been touched by every job that ever ran on it. The community
edition's design refuses to be that target:

- **Secrets are fetched just-in-time.** The cloud secret store is
  consulted at the start of each run; the SSH private key lives in the
  side-car container's tmpfs for the duration of the playbook; the
  container is destroyed when the playbook finishes. There's no place
  for the key to leak to.
- **No process or filesystem outlives the run.** The Ansible binary,
  the playbook bytes, the asset cache, the inventory JSON — all of it
  goes away with the container. The next run starts from zero.
- **No shared user namespace between runs.** Two operators running
  back-to-back jobs against the same target cannot accidentally read
  each other's variables, output, or temp files. Each run is its own
  Linux process tree.
- **The dashboard process itself never holds the secret long.** The
  playbook bytes pass through the dashboard's memory once on the way
  to the runner; nothing is cached on disk on the dashboard host.

The compliance angle: regulations like SOC 2 CC6.1, NIST SP 800-53
AC-6 (least privilege) and SC-39 (process isolation), and CIS Controls
4.1 / 4.7 all point at "minimise persistent privileged surface". An
ephemeral runner satisfies them by construction — there's nothing
persistent to harden, audit, or rotate. Auditors generally accept
"the runner has a 90-second lifespan and zero state at rest" with less
friction than "here's our hardening baseline for the long-running
worker fleet."

Compared to common alternatives:

| Approach | Persistent attack surface | Secret-at-rest in runner | Per-run isolation |
|---|---|---|---|
| **Dedicated CI worker** (e.g. self-hosted GitHub runner) | The whole VM | Cached creds, SSH known_hosts, build artefacts | Best-effort cleanup scripts |
| **Always-on Ansible Tower / AWX** | Platform process | Vault-decrypted secrets in process memory | Within the platform's job isolation |
| **Dashboard's one-shot runner** | None — container is gone | Tmpfs, lifetime of run | Container per run; no shared FS |

This isn't unique to the dashboard at the technical level — the
underlying primitives (Fargate / ACI / Cloud Run Jobs / `docker run
--rm`) have been around for years. What's notable is the design
decision to *only* offer ephemeral runners. There's no escape hatch in
the codebase for "give me a long-lived worker for performance reasons."
You pay a one-second startup penalty per run; you never have to defend
a fleet of long-lived runners to a security review.

---

## Secret scanning (advisory)

Uploaded assets are scanned for hard-coded secrets and you're **warned** — it's
advisory: the upload always succeeds, the finding is a heads-up. The point is to
catch an AWS key or a plaintext password *before* it's stored in the asset backend
and shipped to a target.

- **When** — on upload, at both `/api/storage/upload` and `/api/config-mgmt/upload`.
- **What it catches** — AWS access keys, private-key blocks, GitHub / Google /
  Slack tokens, and generic `password` / `secret` / `token` / `api_key`
  assignments. Matched values are **redacted** in the finding.
- **What it ignores** (so it doesn't cry wolf) — templated values (`{{ … }}`,
  `$VAR`, `${VAR}`), placeholders (`changeme`, `<your-password>`),
  Ansible-Vault-encrypted files, and binary assets (`.rpm` / `.deb`).
- **Config** — `secret_scan_enabled` (default **on**; it only warns). Set false to
  disable.

The right fix when it fires: move the value into a vault reference (see
[Secrets Management](secrets-management.md)) or Ansible Vault, and reference it
from the playbook rather than hard-coding it.

---

## Config-drift visibility

The Ansible stream remembers each successful apply, so you can tell when a target
has drifted out of "known-good." It's **passive** — it records a fingerprint on a
successful run; it never touches a target to check (no `--check` reconciler).

- **What's recorded** — on each successful run, a per-`(target, playbook)` row with
  a content fingerprint of the applied asset and the timestamp
  (`config_drift_tracking_enabled`, default **on**).
- **Two signals** —
  - **Unverified** — the last apply is older than `config_drift_stale_days`
    (default 14): *"host X unverified since 2026-06-20."*
  - **Changed** — the playbook's *current* content in storage no longer matches
    what was applied: the target is running an **older version** than what's on
    disk now.
- **Where it shows** — `GET /api/config-mgmt/drift` returns the per-target signals;
  the dashboard's **Needs attention** panel rolls up *"N config targets need
  attention."* Re-applying the playbook clears the signal.

Fingerprints are one-way hashes — the inputs/secret values themselves are never
stored.

---

## Best practices

**Stage your changes.** Build a target group with one or two test hosts
in it before applying to the whole fleet. The inventory is just JSON —
add a `test` group in your hypervisor hostvars module to make this
trivial.

**Keep secrets out of playbooks.** Anything sensitive belongs in
[Secrets Management](secrets-management.md), not embedded in YAML or
shell scripts. Reference secrets via Ansible's `lookup('env', ...)`,
the `ansible-vault` integration, or fetched-at-runtime variables that
the runner reads from the cloud secret store.

**Use a secret without seeing it.** The run form's **Use a secret** panel injects
a Secrets-Management secret (as a named var, become password, or SSH key) — or a
**BeyondTrust Password Safe managed account** checked out just-in-time — straight
into the run. The operator never sees the value; it's scrubbed from job output and
the use is audited. Requires the `secrets:use` permission. See
[Using a Secrets-Management secret in a run](integrations/ansible.md#using-a-secrets-management-secret-in-a-run).

**Tag your runs.** The `Extra Vars` field on the run form accepts JSON
— include a `run_id` or a deployment ticket number so when something
breaks at midnight you can `grep` the logs back to the playbook
invocation.

**Read the job logs.** Every run lands in the dashboard's job tracker
(`/jobs`). Cloud-runner runs include a CloudWatch / Azure Monitor /
Cloud Logging log link. Local Docker runs include the full Ansible
output inline. Drift, rollback decisions, and post-mortems live there.

**Don't modify assets in place.** Re-upload as a new file with a date or
version stamp (`hardening-base-2026-04-12.yml`). The storage backend
preserves both. When you're sure the new one works, delete the old via
`/storage`.

**Idempotency tests.** Run the same playbook twice in a row. The second
run should report `0 changed` (or as close to it as your real workload
allows). If the second run still does work, your "declarative" playbook
is hiding procedural logic and will surprise you on partial failures.

---

## Where this is heading on SaaS

A few things the community edition does *not* try to do. They're SaaS
priorities — see [docs/saas-comparison.md](saas-comparison.md) for the
hosted-edition philosophy.

- **AI-assisted playbook generation.** "Install fail2ban with custom
  jail rules for SSH on these eight Debian hosts" → generated YAML the
  operator reviews, edits, and runs. The community edition's auto-wrap
  is a baseline; the SaaS edition's helper service is the up-leveled
  version, with the dashboard's asset schema and the active inventory
  in context so the generated playbook is opinionated about *your*
  environment, not generic.
- **Tenant-scoped asset libraries.** Multi-tenancy means each
  organisation gets its own storage namespace, its own inventory, and
  its own credentials — without per-instance dashboard deployment. The
  community edition keeps everything single-tenant by design.
- **Drift-aware runs.** SaaS keeps a per-target hash of the last
  successfully-applied asset and surfaces "the live state of host X
  hasn't been verified since 2026-04-12" without manual checks.
  Community runs leave this kind of telemetry on the table; the job
  log is the audit trail.

The ephemeral-runner property in [Why one-shot runners](#why-one-shot-runners-the-security-argument)
above carries forward to SaaS — every run still gets its own
single-purpose container and is destroyed after execution. The hosted
edition adds tenant-scoped network isolation on top, so a run in tenant
A can't reach tenant B's targets even by mistake.

You can be productive on community indefinitely with the practices in
this doc. Move to SaaS when the AI assistance, tenant separation, or
managed audit trail are worth more than self-hosting flexibility.

---

## Troubleshooting

**Run fails with "no active storage backend."**
Set up storage on `/storage` first; the Ansible feature flag depends on
it. See [storage-management.md](storage-management.md).

**Local Docker runner fails with "permission denied" mounting `/var/run/docker.sock`.**
The dashboard container needs Docker-out-of-Docker access to spawn the
side-car runner. Check `docker-compose.yml` includes
`/var/run/docker.sock:/var/run/docker.sock:ro` and that the container
user can read it.

**Cloud runner says "AccessDenied" fetching the asset from S3.**
The IAM role / service principal / GCP service account the dashboard
configured for VM deploys also needs read access to the storage bucket.
Add the `s3:GetObject` / `Storage Blob Data Reader` /
`storage.objects.get` permission for the storage bucket's resource path.

**Hyper-V / Windows targets `connection refused` on PowerShell runs.**
The target's hostvars need `ansible_connection=winrm` plus the WinRM
auth fields (`ansible_winrm_transport`, credentials). Verify by running
`docker compose exec app cat /tmp/inventory.json` after a failed run and
checking the target's hostvars block.

**Run log shows the right SSH user but `Permission denied (publickey)`.**
The cloud secret store probably has a stale key, or the VM's
`authorized_keys` doesn't include it. Confirm the secret's `public_key`
matches what's actually on the VM via `cat ~/.ssh/authorized_keys`.
