# BeyondTrust EPM for Linux (EPM-L)

## What is it?

**BeyondTrust Endpoint Privilege Management for Linux (EPM-L)** is a SaaS
product that manages privilege elevation on Linux endpoints. The dashboard
integrates with the EPM-L public API on the BeyondTrust Pathfinder gateway to:

- **List and build agent packages** — view available `.rpm` / `.deb` client
  packages and trigger a build when none are ready yet
- **Sync packages to asset storage** — download agent packages from
  BeyondTrust and upload them to your Ansible asset backend with one API call
- **Issue installation tokens** — generate short-lived tokens used to
  register new endpoints with EPM-L (passed as an Ansible extra var)

The EPM-L routes (`/api/epml/*`) are gated behind the **BeyondTrust**
feature flag — enable it in Settings → Integrations → BeyondTrust to
activate them.

### How the API is addressed

The EPM-L OpenAPI spec writes endpoint paths as `/api/<rest>` with an empty
`servers` block. On the deployed gateway, that `/api` prefix is **replaced**
by a site- and product-scoped base:

```
spec /api/<rest>   →   https://api.beyondtrust.io/site/<site-id>/epm/linux/<rest>
```

Authentication is a Pathfinder **Personal Access Token** sent as a standard
Bearer header (`Authorization: Bearer PAT_...`) — no token exchange. Note
that `app.beyondtrust.io` is the *browser portal only*: it authenticates
with session cookies and returns 401 for every Bearer request.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust EPM for Linux subscription | EPM-L enabled on your Pathfinder site |
| Personal Access Token (PAT) | Created in Pathfinder (see Step 1). PAT support may require org-level enablement by BeyondTrust. |
| Your Pathfinder Site ID | A UUID — see Step 2 |
| **For package sync:** Ansible asset storage | Any one of S3, Azure Blob Storage, or GCS configured — the sync uses whichever backend is active. See [docs/integrations/ansible.md](ansible.md) Step 1 for storage setup. |

---

## Step 1 — Create a Personal Access Token

1. Sign in to your Pathfinder site at `app.beyondtrust.io`
2. Click the **profile icon** in the header and select **Manage Profile**
3. Scroll to the **Personal Access Tokens** section
4. Choose an expiry, then click **Create Token**
5. Click **Copy Token** — the value (it starts with `PAT_`) is shown only once

Two things to know about PATs:

- They are **bound to the site that is active when you create them** and
  cover the products available to you on that site. Multi-site orgs: use the
  site selector in the top navigation to switch to your EPM-L site *first*.
- If the Personal Access Tokens section is missing, the feature may need to
  be enabled for your organization — contact your BeyondTrust representative.

## Step 2 — Find your Site ID

While signed in to Pathfinder, open this URL in the same browser tab and
copy the **`site_id`** field from the JSON response:

```
https://app.beyondtrust.io/api/platform/currentSite
```

## Step 3 — Configure the dashboard

**Settings → Integrations → BeyondTrust → EPM for Linux (EPM-L)**

Paste the **Site ID** and the **Personal Access Token** and save. The PAT is
encrypted with AES-256 and stored in the database — it never touches disk in
plaintext.

`.env` equivalents (used as fallbacks when the DB values are unset):

```bash
EPML_SITE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
EPML_PAT=PAT_...
# Advanced — gateway host only; the dashboard appends /site/<id>/epm/linux:
EPML_BASE_URL=https://api.beyondtrust.io
```

---

## Syncing packages to asset storage

`POST /api/epml/sync-packages` runs a background job that:

1. Calls `GET …/epml/clientpkg` to list available packages
2. If no packages are available, triggers a build and polls
   `GET …/epml/clientpkg/status` every 10 seconds until it reports
   `{"building": false}` *and* packages appear (up to 15 minutes)
3. Downloads each `.rpm` and `.deb` package
4. Uploads them to your configured asset storage backend — the same backend
   that Ansible uses for all other assets (S3, Azure Blob Storage, or GCS)

```bash
curl -X POST -H "Authorization: Bearer $DASHBOARD_JWT" \
     http://localhost:8001/api/epml/sync-packages
# → {"job_id": "...", "status": "queued"} — watch progress at /jobs/<job_id>
```

The package list response looks like:

```json
{
  "clientpkg": [
    {
      "file": "epml-client.x86_64.rpm",
      "version": "26.1.1-04",
      "release": "1",
      "arch": "x86_64",
      "created": "2026-06-10 21:08:17 +0000 UTC",
      "link": "https://epml-client-packages.s3.us-east-1.amazonaws.com/..."
    }
  ]
}
```

`link` values are **pre-signed S3 URLs valid for about 30 minutes**, and the
download is made *without* the Authorization header (S3 rejects requests
that carry both a pre-signed signature and a Bearer header). If your egress
is filtered, allow `*.s3.amazonaws.com` /
`epml-client-packages.s3.us-east-1.amazonaws.com` in addition to
`api.beyondtrust.io`.

After the sync job completes, the packages appear in the Config Mgmt asset
list and can be selected as Ansible targets. Re-run the sync whenever
BeyondTrust releases a new agent version.

---

## Getting an installation token

Installation tokens register a new Linux endpoint with EPM-L (the agent is
activated with `pbactivate -t <token>`). The API returns `{"token": "<JWT>"}`.

**Via the dashboard API:**

```bash
# Default expiry: 480 minutes (8 hours)
curl -H "Authorization: Bearer $DASHBOARD_JWT" \
     http://localhost:8001/api/epml/token

# Custom expiry (minutes, 30 – 525600)
curl -H "Authorization: Bearer $DASHBOARD_JWT" \
     "http://localhost:8001/api/epml/token?expiry_minutes=1440"
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
        cmd: "pbactivate -t {{ epml_token }}"
```

---

## Manual build trigger

If packages are stale or a new version exists, trigger a fresh build:

```bash
curl -X POST -H "Authorization: Bearer $DASHBOARD_JWT" \
     http://localhost:8001/api/epml/trigger-build
```

Check build progress (`{"building": true|false}`):

```bash
curl -H "Authorization: Bearer $DASHBOARD_JWT" \
     http://localhost:8001/api/epml/build-status
```

---

## Troubleshooting

The dashboard maps the gateway's error responses to actionable messages.
What they mean:

| Gateway response | Meaning | Fix |
|---|---|---|
| `EPM-L Site ID is not configured` | No site id in Settings or `.env` | Set it (Step 2 / Step 3) |
| 401 `Access denied for this site` | The site id is wrong, **or** the PAT was created while a different site was active | Verify the Site ID; if correct, recreate the PAT on the right site (Step 1) |
| 401 `Token could not be decoded` | The PAT is malformed or truncated | Re-paste the full `PAT_...` value |
| 401 `Personal access token not found` | A just-created PAT hasn't propagated yet (~seconds), or it was revoked | Retry shortly; recreate if it persists |
| HTTP 421 (empty body) | The request path is wrong for the gateway — usually a bad base URL | `EPML_BASE_URL` should be `https://api.beyondtrust.io` (host only) |
| 403 mentioning SHA-256 / `key=value` | The request hit an AWS-IAM-signed endpoint — wrong base URL for PAT auth | Same fix as above |
| HTTP 403 on a package download | The pre-signed link expired (~30 min) | Re-run the sync — it lists fresh links |

**Sync job reports "no new packages uploaded"** — either no packages are
ready yet (check build status) or both `rpm_uploaded` and `deb_uploaded`
came back `False` because nothing matched. Check
`GET /api/epml/build-status` to see if a build is in progress.

**Build poll times out (15 minutes)** — BeyondTrust builds can take longer
during high-demand periods. Re-run the sync job; it resumes where the build
left off.

**"No asset storage configured"** — the sync requires at least one Ansible
storage backend. Set `ANSIBLE_S3_BUCKET`, `ANSIBLE_AZURE_STORAGE_ACCOUNT`, or
`ANSIBLE_GCS_BUCKET` in Settings → Integrations → Ansible before running the
sync. See [docs/integrations/ansible.md](ansible.md) Step 1 for setup instructions.

**Packages appear in the list but are wrong architecture** — EPM-L builds
`.rpm` (x86_64) and `.deb` (amd64) variants of both the standard and the
cached client. Confirm the correct package is selected in the Config Mgmt
target picker based on your target OS family.
