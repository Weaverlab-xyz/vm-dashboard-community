# BeyondTrust Password Safe

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want credentials vaulted and rotated rather than stored by this dashboard.

## What is it?

**BeyondTrust Password Safe / Secrets Safe** is on-demand checkout of SSH keys and
passwords. Target credentials (AWS keys, Azure service principal secrets, SSH private
keys) are fetched at the moment the dashboard needs them and discarded after use, rather
than stored in the dashboard's encrypted database. Driven by `ps-cli`.

It also **onboards** the resources the dashboard builds — VMs, cloud databases, and
Kubernetes ServiceAccount tokens — as managed systems + accounts, so their credentials
rotate on the tenant's schedule instead of living forever as whatever the deploy minted.

Gated by `password_safe_enabled`. This is one of three independently-gated BeyondTrust
products; see [BeyondTrust Integrations](../beyondtrust.md) for the map, and
[Privileged Remote Access](privileged-remote-access.md) for the jump-item and Gateway
half of the story.

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
  BeyondTrust Gateway container retrieve SSH keys from Password Safe managed
  accounts, so the private key never touches the host filesystem.
- **In-playbook secret lookup (Ansible)** — a config-management playbook can fetch
  its own secrets/managed-account passwords from Password Safe at runtime via the
  `beyondtrust.secrets_safe` Galaxy collection. The dashboard reuses this same OAuth
  client (`pscli_*`) — auto-injecting it into the runner as `PASSWORD_SAFE_*` — so no
  separate credential is needed. See
  [Ansible secrets](../platform/ansible/secrets.md#in-playbook-password-safe-lookup-beyondtrustsecrets_safe)
  and [examples/playbooks/password-safe/](../../examples/playbooks/password-safe/).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust Password Safe | Secrets Safe licence; hosted or on-prem |
| `ps-cli` | Ships as the `beyondtrust-bips-cli` **pip dependency** (`web_dashboard/requirements.txt`), so it is present in any image built from this repo — there is no separate binary install step |

---

## Setup

### Step 1 — Password Safe OAuth application (ps-cli)

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

### Step 2 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

The wizard's **Feature Flags** step lists the optional integrations. Toggle **Password
Safe** on there — that step carries toggles only, so fill in the fields from **Settings →
Integrations** once you have logged in.

**Option B — Settings → Integrations (after first run)**

1. Open **Settings** → **Integrations** → **Password Safe** → toggle on
   (`password_safe_enabled`).
2. Click **Configure →**. The panel has four tabs: **Connection** (API connection and
   authentication), **VMs** (VM registration, PRA Vault key sync, the OT cell checkout),
   **Databases** (import and onboarding; the Azure and GCP onboarding sections are
   collapsed until you open them) and **Kubernetes** (token rotation). Every tab is
   saved together, so you can fill in several tabs and click **Save** once.
3. On the **Connection** tab, fill in the **API connection** section:

   | Field | Example |
   |---|---|
   | Password Safe URL | `https://ps.company.com` |
   | OAuth Client ID | (from API Registration) |
   | OAuth Client Secret | (from API Registration) |
   | API run-as account | The BeyondInsight user the OAuth client runs as — required by the Password Safe Terraform provider for VM onboarding |

4. Click **Save**. No container restart is required.

> These are **Settings keys**, not environment variables. The dashboard's config store
> reads from the database only; exporting an equivalently-named env var has no effect.

---

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [What Password Safe enables in the dashboard](password-safe/what-it-enables.md) | you want to know everything turning Password Safe on changes: checkout, onboarding, rotation and the pages that use it |
| [Password Safe workstream attributes](password-safe/workstream-attributes.md) | you want what a workstream builds tagged with Password Safe attributes, and filtered by them |
| [Troubleshooting Password Safe](password-safe/troubleshooting.md) | sign-in, checkout, onboarding or rotation through Password Safe fails |
| [What the Password Safe public API can and cannot do](password-safe/public-api.md) | you need to know what the dashboard can do through Password Safe's public API, and what it has to leave to the console |

---

## Password Safe VM onboarding (managed systems)

Onboarding the Linux and Windows VMs the dashboard builds, per cloud, and syncing their keys into the PRA Vault: see [Onboarding VMs](password-safe/vm-onboarding.md).

## Kubernetes ServiceAccount token rotation

Making the ServiceAccount token a PRA Kubernetes tunnel injects a rotating Password Safe managed account: see [Kubernetes token rotation](password-safe/kubernetes-tokens.md#kubernetes-serviceaccount-token-rotation).

## Databases

Handing database credentials to Password Safe, and importing databases Password Safe already
manages. The shared model is on [Password Safe: databases](password-safe/databases.md), and
each cloud's channel has its own page: [AWS (`dbssm`)](password-safe/databases-aws.md),
[Azure (`dbazure`)](password-safe/databases-azure.md) and
[GCP Cloud SQL (`dbgcp`)](password-safe/databases-gcp.md).

---

## Advanced configuration

`bt_ps_deploy_key_title` — the title of the Password Safe secret holding the Gateway Docker
deploy key — is documented with the Gateway keys it belongs to, on
[Privileged Remote Access → Advanced configuration](privileged-remote-access.md#advanced-configuration).
Password Safe is only the storage mechanism there; starting a Gateway is a PRA concern.

---

## Workload identity

Instead of the OAuth client credentials, the dashboard's REST calls to Password Safe can present the dashboard's
own workload identity: set **Authentication → Workload identity** on this panel, after
setting up the shared identity and adding Password Safe to its Pathfinder registration's
**Product Scopes**. `ps-cli`, the Secrets Safe backend, the Ansible runner and the `passwordsafe` Terraform provider still use the client credentials, so keep them set. Not yet verified live — see
[BeyondTrust platform identity](platform-identity.md).

