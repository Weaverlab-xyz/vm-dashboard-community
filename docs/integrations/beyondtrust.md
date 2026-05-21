# BeyondTrust Integration

## What is it?

The BeyondTrust integration connects the dashboard to two BeyondTrust products:

- **Password Safe (ps-cli)** — on-demand checkout of SSH keys and passwords
  stored in BeyondTrust Secrets Safe. Target credentials (AWS keys, Azure
  service principal secrets, SSH private keys) are fetched from Password Safe
  at the moment the dashboard needs them and discarded after use, rather than
  being stored in the dashboard's encrypted database.
- **Privileged Remote Access (btapi)** — optional session-metadata callbacks
  to BeyondTrust PRA during remote access operations (Shell Jump / session
  recording context).

Both are controlled by the single `BEYONDTRUST_ENABLED` flag. You can
configure only Password Safe (ps-cli) and leave the btapi block blank if you
do not have a PRA deployment.

---

## Use cases

- **Vault-backed cloud credentials** — instead of entering AWS access keys,
  Azure service principal secrets, or SSH private keys into the dashboard
  (where they would be stored encrypted in the application database), the
  dashboard fetches them from Password Safe at runtime. Rotate credentials in
  one place; the dashboard always gets the current value.
- **Audit trail** — every secret checkout creates a Password Safe audit record.
  You know who (the dashboard service account) requested what credential and
  when.
- **SSH key checkout for cloud VMs** — the Ansible config-management runner and
  BeyondTrust Jumpoint container retrieve SSH keys from Password Safe managed
  accounts, so the private key never touches the host filesystem.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust Password Safe | Secrets Safe licence; hosted or on-prem |
| `ps-cli` binary inside the container | BeyondTrust BIPS CLI; baked into the `app` Docker image |
| BeyondTrust PRA (optional) | Required only if using session recording via btapi |
| `btapi` binary inside the container | BeyondTrust API CLI; baked into the `app` Docker image |

---

## Setup

### Part 1 — Password Safe OAuth application (ps-cli)

ps-cli authenticates to Password Safe with an OAuth 2.0 client-credentials
grant.

1. In **Password Safe** → **Configuration** → **API Registration** →
   **Add API Registration**:
   - Authentication type: **Client Credentials**
   - Copy the **Client ID** and **Client Secret** displayed after creation.

2. Assign the registration the following permissions (minimum):
   - **Secrets** → Read
   - **Requests** → Create
   - **Credentials** → Read

   Add Managed System / Managed Account scope for any accounts the dashboard
   will check out SSH keys from.

### Part 2 — btapi credentials (PRA session recording — optional)

btapi authenticates to the BeyondTrust PRA API with its own client credentials.

1. In **BeyondTrust PRA** → **Configuration** → **API Configuration** →
   **Add API Account**. Copy the **Client ID** and **Client Secret**.
2. The API host is the hostname of your PRA appliance, e.g.
   `https://pra.company.com`.

> If your PRA appliance and Password Safe are the same host, the credentials
> from Part 1 and Part 2 may be identical.

### Part 3 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

The wizard Step 5 lists optional integrations. Toggle **BeyondTrust** on and
fill in the fields.

**Option B — Settings → Integrations (after first run)**

1. Open **Settings** → **Integrations** → **BeyondTrust** → toggle on.
2. Fill in the **Password Safe** section:

   | Field | Example |
   |---|---|
   | Password Safe URL | `https://ps.company.com` |
   | OAuth Client ID | (from API Registration) |
   | OAuth Client Secret | (from API Registration) |

3. Fill in the **btapi** section (leave blank if not using PRA):

   | Field | Example |
   |---|---|
   | API Host | `https://pra.company.com` |
   | Client ID | (from API Account) |
   | Client Secret | (from API Account) |

4. Click **Save**. No container restart is required.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Vault-backed cloud credentials** | AWS, Azure, and SSH credentials resolved from Password Safe at runtime rather than stored in the application database |
| **SSH key checkout** | Ansible and BT Jumpoint tasks retrieve SSH private keys from Managed Accounts on demand |
| **PRA session context** | Shell Jump sessions opened by the dashboard are tagged with job metadata in BeyondTrust PRA |
| **Secret audit log** | Every checkout creates an immutable record in Password Safe |

---

## Preparing images for BT management

Images built by the dashboard's Packer flow (`/images/aws`, `/images/azure`, `/images/gcp`) can be pre-conditioned for BeyondTrust pickup using the provisioner scripts under [`provisioners/beyondtrust/`](../../provisioners/beyondtrust/):

- [`bt-ready-debian.sh`](../../provisioners/beyondtrust/bt-ready-debian.sh) — Debian, Ubuntu.
- [`bt-ready-rpm.sh`](../../provisioners/beyondtrust/bt-ready-rpm.sh) — RHEL, Rocky, CentOS Stream, AlmaLinux, Amazon Linux 2 / 2023.

What they prepare:

- **PRA Shell Jump connectivity** — sshd hardened (key-only, no root password, sensible client-alive), passwordless sudo wired to the cloud-default user via a `/etc/sudoers.d/90-bt-ready` drop-in, host clock synced. The sshd drop-in is written as `00-bt-ready.conf` so it loads lex-first and wins against any later compliance drop-ins (sshd is first-occurrence-wins).
- **Conservative baseline hygiene** — security updates applied, persistent journald, opt-in unattended security updates (`BT_AUTOPATCH=1`), image cleaned for re-launch (host keys + machine-id + cloud-init state stripped).
- **Optional CIS / STIG remediation** (`BT_APPLY_CIS=1`) — installs OpenSCAP + SCAP Security Guide and applies a per-distro profile (default CIS L1 Server). Override via `BT_CIS_PROFILE=stig` or `cis_level2_server` (short names auto-expand to the SSG namespace). Report HTML lands at `/var/log/bt-ready/cis-report.html` on the built image. Debian-proper has no SSG CIS profile shipped, and Amazon Linux 2023's SSG coverage is incomplete — both warn and skip.

What they deliberately don't do (covered in the script README):

- No new local accounts (sudoers wired to the existing cloud-default user).
- No Password Safe Managed Account creation (those are admin-managed in PS).
- No EPM-L agent install (registration tokens expire 8h after issue; needs a first-boot hook, separate effort).
- No host firewall (cloud security groups are the source of truth).

**Cross-cloud constraint**: Azure's Packer builder invokes scripts as `sudo -E sh '{{ .Path }}'`, forcing `/bin/sh` regardless of shebang. Both scripts are strict POSIX `sh` (verified with `dash -n`) so they behave identically on AWS, Azure, and GCP.

**Using them**: upload the appropriate script to your active storage backend via `/storage`, then on the AWS / Azure / GCP build page pick it from the **Load from storage** dropdown above the Provisioner Script textarea. Full smoke-test recipe and the operator-overridable env vars (`BT_TARGET_USER`, `BT_AUTOPATCH`, `BT_SKIP_UPDATES`, `BT_SKIP_CLEANUP`, `BT_APPLY_CIS`, `BT_CIS_PROFILE`) are in [`provisioners/beyondtrust/README.md`](../../provisioners/beyondtrust/README.md).

---

## Advanced configuration

### Jump group and policy (PRA Shell Jump routing)

If your PRA deployment uses multiple jump groups, set:

```
BT_JUMP_GROUP_NAME=us-east-2
BT_GROUP_POLICY_NAME=BeyondTrust IT User
BT_JUMPOINT_ID=7
```

`BT_JUMPOINT_ID` is the numeric ID of the Jumpoint the dashboard will use for
cloud instances (visible in PRA → **Jump** → **Jumpoints** → edit a jumpoint).

### Azure-specific jump group override

```
AZURE_BT_JUMP_GROUP_NAME=azure-east
AZURE_BT_GROUP_POLICY_NAME=BeyondTrust IT User
AZURE_JUMPOINT_ID=9
```

Leave blank to fall back to the global `BT_JUMP_GROUP_NAME` / `BT_GROUP_POLICY_NAME`.

### Password Safe secret titles

The dashboard looks up secrets by **title** in Password Safe. The defaults work
for a standard deployment; override in **Settings → Integrations → BeyondTrust**
if your titles differ:

```
BT_PS_DEPLOY_KEY_TITLE=Docker Deploy Key
```

---

## Troubleshooting

**"ps-cli not found"** — the `ps-cli` binary must be on `PATH` inside the
container. Verify the Dockerfile includes the BIPS CLI installation step and
rebuild the image.

**"Authentication failed" from ps-cli** — verify the Client ID and Client Secret
in **Settings → Integrations → BeyondTrust** match the API Registration in
Password Safe and that the registration has not expired. Run
`docker compose exec app ps-cli --version` to confirm the binary is present.

**"btapi command failed"** — confirm `BT_API_HOST` is reachable from inside the
container: `docker compose exec app curl -Is "$BT_API_HOST"`. If the host uses
a self-signed certificate you may need to add it to the container's CA store.

**Secrets retrieved are empty** — check that the API Registration has **Secrets →
Read** and **Credentials → Read** permissions, and that the specific secret is
in scope for the registration.
