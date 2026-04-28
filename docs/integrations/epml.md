# BeyondTrust EPM for Linux (EPM-L)

## What is it?

**BeyondTrust Endpoint Privilege Management for Linux (EPM-L)** is a SaaS
product that manages privilege elevation on Linux endpoints. The dashboard
integrates with the EPM-L cloud API (`app.beyondtrust.io`) to:

- **List and build agent packages** — view available `.rpm` / `.deb` client
  packages and trigger a build when none are ready yet
- **Sync packages to S3** — download agent packages from BeyondTrust and
  upload them directly to your Ansible asset bucket with one click
- **Issue installation tokens** — generate short-lived tokens used to
  register new endpoints with EPM-L (passed as an Ansible extra var)

The EPM-L routes (`/api/epml/*`) are gated behind the **BeyondTrust**
feature flag — enable it in Settings → Integrations → BeyondTrust to
activate them.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust EPM for Linux subscription | Access to `app.beyondtrust.io` |
| Personal Access Token (PAT) | Generated in the EPM-L portal with at minimum `packages:read`, `packages:build`, and `tokens:create` scopes |
| **For package sync:** Ansible asset storage | Any one of S3, Azure Blob Storage, or GCS configured — the sync uses whichever backend is active (S3 > Azure Blob > GCS priority). See [docs/integrations/ansible.md](ansible.md) Step 1 for storage setup. |

---

## Step 1 — Generate a Personal Access Token

1. Log in to `app.beyondtrust.io`
2. Navigate to **Account settings → API access → Personal access tokens**
3. Create a new token with the following scopes (grant only what you need):
   - `epml:packages:read` — list available packages
   - `epml:packages:build` — trigger a build
   - `epml:tokens:create` — issue installation tokens
4. Copy the token value — it is shown only once

---

## Step 2 — Configure in the dashboard

**Settings → Integrations → BeyondTrust → EPM-L PAT**

Paste your PAT into the **EPM-L Personal Access Token** field and save.
The token is encrypted with AES-256 and stored in the database — it never
touches disk in plaintext.

The `epml_base_url` defaults to `https://app.beyondtrust.io`. Change it
only if BeyondTrust instructs you to use a different tenant URL.

---

## Syncing packages to asset storage

The **Config Mgmt → AWS → Sync from BeyondTrust** button runs a background
job that:

1. Calls `GET /api/epml/clientpkg` to list available packages
2. If no packages are available, triggers a build and polls
   `GET /api/epml/clientpkg/status` every 10 seconds until complete
   (up to 15 minutes)
3. Downloads each `.rpm` and `.deb` package
4. Uploads them to your configured asset storage backend — the same backend
   that Ansible uses for all other assets (S3, Azure Blob Storage, or GCS)

The active backend is determined automatically (S3 takes priority if both
`ANSIBLE_S3_BUCKET` and `ANSIBLE_AZURE_STORAGE_ACCOUNT` are set, etc.). You
only need one backend configured; whichever is active receives the packages.

After the sync job completes, the packages appear in the Config Mgmt asset
list and can be selected as Ansible targets. You can trigger this manually
whenever BeyondTrust releases a new agent version.

---

## Getting an installation token

Installation tokens are used to register a new Linux endpoint with EPM-L.
They are typically passed as an Ansible extra variable when running the
agent-install playbook.

**Via the API:**

```bash
# Default expiry: 480 minutes (8 hours)
curl -H "Authorization: Bearer $DASHBOARD_JWT" \
     http://localhost:8000/api/epml/token

# Custom expiry (minutes, 30 – 525600)
curl -H "Authorization: Bearer $DASHBOARD_JWT" \
     "http://localhost:8000/api/epml/token?expiry_minutes=1440"
```

**As an Ansible extra var:**

In the Config Mgmt **Run** dialog, set:

```json
{ "epml_token": "<paste token here>" }
```

Your install playbook would then reference `{{ epml_token }}`:

```yaml
- hosts: all
  become: yes
  tasks:
    - name: Install EPM-L agent
      ansible.builtin.package:
        name: /tmp/epml-client.x86_64.rpm
        state: present
    - name: Register endpoint
      ansible.builtin.command:
        cmd: "epmlagent register --token {{ epml_token }}"
```

---

## Manual build trigger

If packages are stale or a new version exists, trigger a fresh build:

```bash
curl -X POST -H "Authorization: Bearer $DASHBOARD_JWT" \
     http://localhost:8000/api/epml/trigger-build
```

Check build progress:

```bash
curl -H "Authorization: Bearer $DASHBOARD_JWT" \
     http://localhost:8000/api/epml/build-status
```

---

## Troubleshooting

**"EPM-L PAT is not configured"** — set the PAT in Settings → Integrations
→ BeyondTrust → EPM-L Personal Access Token.

**`list_packages` returns HTTP 401** — the PAT has expired or the scopes are
insufficient. Regenerate a token in the EPM-L portal with the correct scopes.

**`list_packages` returns HTTP 403** — your BeyondTrust account does not have
access to the EPM-L module. Confirm your subscription includes EPM for Linux.

**Sync job reports "no new packages uploaded"** — either no packages are ready
yet (check build status) or the packages have already been uploaded to S3 (the
job is idempotent — it re-uploads regardless, but the message appears when both
`rpm_uploaded` and `deb_uploaded` are `False`). Check
`GET /api/epml/build-status` to see if a build is in progress.

**Build poll times out (15 minutes)** — BeyondTrust builds can take longer
during high-demand periods. Re-run the sync job; `ensure_packages` will pick
up the completed build on the next poll cycle.

**"No asset storage configured"** — the sync requires at least one Ansible
storage backend. Set `ANSIBLE_S3_BUCKET`, `ANSIBLE_AZURE_STORAGE_ACCOUNT`, or
`ANSIBLE_GCS_BUCKET` in Settings → Integrations → Ansible before running the
sync. See [docs/integrations/ansible.md](ansible.md) Step 1 for setup instructions.

**Packages appear in the list but are wrong architecture** — EPM-L builds both
`.rpm` (x86_64) and `.deb` (amd64) variants. Confirm the correct package is
selected in the Config Mgmt target picker based on your target OS family.
