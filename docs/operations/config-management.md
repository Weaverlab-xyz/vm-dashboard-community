# Config Management

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are about to run an Ansible job and want to know how the runner handles secrets and isolation.

Part of [Operations](../operations.md).

This document explains how the dashboard does config management — the
philosophy that drives the design, the best practices the codebase
encodes, and how the on-premises and cloud paths fit together.

> **If your dashboard runs in a cloud** (ECS / ACI / Container Apps), the "Local
> Docker" runner below cannot serve your on-premises targets: it is a sibling
> container on the *dashboard's* host, which has neither a Docker socket nor a
> route to your LAN. Those targets are reached by a
> [remote agent](../platform/spiffe/remote-agents/config-runs.md#agent-executed-config-management) instead, which
> runs the same one-shot container inside your network. Everything else in this
> document — the asset types, the secret handling, the drift tracking — is
> unchanged.

The companion docs:

- [Infrastructure as Code](../clouds/infrastructure-as-code.md) — how the dashboard
  stands the infra up in the first place
- [Image Management](image-management.md) — what's *on* the VMs
  before config-management runs against them
- [Secrets Management](../admin/secrets-management.md) — where credentials live
- [Storage Management](storage-management.md) — where playbooks and
  assets live

## The page

`/config-mgmt` has three tabs:

- **Run**: the run form. Pick an asset and a target, optionally a change window,
  then **Run**.
- **Targets**: every target the form can pick from, grouped by family (on-premises,
  each cloud, Kubernetes clusters, databases, agent-reached resources).
- **Recent runs**: the latest Ansible jobs, each linking to its job log.

The URL fragment selects a tab, so `/config-mgmt#runs` opens straight on Recent runs.
A bulk selection handed over from the Inventory page always opens on **Run**.

Inside Run, the **Use a secret** section is collapsed by default. It holds the named
secret variables, become method and password, SSH key, EPM-L token variable and the
managed-account picker. It opens by itself for a bulk run and as soon as any of those
fields has a value, and its header shows an **in use** badge while one does. A
collapsed section still applies: everything set in it goes with the run.

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

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [How a Config Management run executes](config-management/execution-paths.md) | you want to know which path a run takes (local runner, cloud task or remote agent) and what each one isolates |
| [Bulk runs from the inventory](config-management/bulk-runs.md) | you want one playbook run against a selection of hosts from the inventory |
| [Config Management runners](config-management/runners.md) | you are choosing or configuring the runner a job executes on |

---

## Running inside a change window

A run does not have to start now. The **When to run** control on the run form can book it
for a specific time, or for the next occurrence of a named change window — and a booked run
that misses its window is marked missed rather than run late. Bulk runs book as a unit, so
a batch is never split across a window boundary.

Nothing about the run itself changes: the job row is identical, the same validation happens
at submit time, and credentials are still resolved at execution time rather than stored. See
[Change Windows](scheduling/change-windows.md).

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

Need a starting point? Ready-to-adapt Linux, Windows, Kubernetes, and
database playbooks live in [`examples/playbooks/`](../../examples/playbooks/).

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
[Secrets Management](../admin/secrets-management.md)) or Ansible Vault, and reference it
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
[Secrets Management](../admin/secrets-management.md), not embedded in YAML or
shell scripts. Reference secrets via Ansible's `lookup('env', ...)`,
the `ansible-vault` integration, or fetched-at-runtime variables that
the runner reads from the cloud secret store.

**Use a secret without seeing it.** The run form's **Use a secret** section injects
a Secrets-Management secret (as a named var, become password, or SSH key) — or a
**BeyondTrust Password Safe managed account** checked out just-in-time — straight
into the run. The operator never sees the value; it's scrubbed from job output and
the use is audited. Requires the `secrets:use` permission. See
[Using a Secrets-Management secret in a run](../platform/remote-worker/secrets.md#using-a-secrets-management-secret-in-a-run).

**Escalate through a privilege broker, not just sudo.** The run form's **Become method**
picker sets how a play escalates when it says `become: true`. Leave it on the default and
Ansible uses `sudo`; pick `pbrun` (or `pmrun`) when the account is entitled through
BeyondTrust Privilege Management for Unix & Linux, `dzdo` for Delinea/Centrify, `pfexec`
on Solaris, and so on.

Worth setting deliberately, because the sudo failure does not name itself: an account with
no sudoers entry makes sudo print its lecture and password prompt onto the module's stdout,
and Ansible reports `Module result deserialization failed: No start of json char found`
rather than "this account cannot sudo". Only the method *name* is ever sent — the
escalation binary and its flags are not configurable from the dashboard, and the remote
agent re-checks the name against its own allow-list before using it.

Available on the **local runner** and on **remote agents**. The transient cloud runners
(ECS / ACI / Cloud Run) take a playbook, an SSH key and a secret channel with no
plain-variable argument, so a run that sets a become method against one is refused rather
than quietly run under sudo.

> **On a PMUL host, check what the policy actually permits.** Under `become`, every
> escalated Ansible task arrives at `pbrun` as the *same* command — a `BECOME-SUCCESS`
> wrapper invoking `python3 AnsiballZ_<module>.py`. PBUL never sees `dnf install` or
> `systemctl restart`, so per-command role entitlements do not constrain an Ansible run:
> permitting the wrapper permits everything Ansible chooses to do. If you want PMUL to
> gate each action, skip `become` and have tasks invoke `pbrun <cmd>` explicitly.
>
> [`examples/playbooks/linux/pmul-ansible-entitlement.yml`](../../examples/playbooks/linux/pmul-ansible-entitlement.yml)
> adds the pbrun-shaped probe to a role idempotently, with a backup and a dry-run mode.
> Run it against the **policy server**, with an account that already has root — it is the
> play that repairs the pbrun escalation path, so it cannot rely on that path working.

The **SPIRE** page's build form offers the same two choices — a managed account or an
SSH-key secret — reading the same `/api/config-mgmt/managed-accounts` and
`/api/config-mgmt/secret-options` endpoints, refusing a run for the same reasons, and
requiring the same `secrets:use` permission. One mental model covers both. It differs in
two ways, both because a lab is one host running four fixed playbooks: the account must
be **pinned** from that host's own list rather than matched by name, and a single
checkbox reuses it as the sudo credential instead of offering a separate become picker.

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
priorities — see [docs/editions/comparison.md](../editions/comparison.md) for the
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

The ephemeral-runner property in [Why one-shot runners](config-management/runners.md#why-one-shot-runners-the-security-argument)
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
`/var/run/docker.sock:/var/run/docker.sock:ro`. The app runs as uid 10001 and the
entrypoint gives it the socket's group automatically; if you start the container with
`-u` / `runAsUser` yourself, add that group with `group_add`. See
[Container User](../platform/container-user.md).

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
