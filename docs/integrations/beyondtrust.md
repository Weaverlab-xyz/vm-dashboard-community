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
