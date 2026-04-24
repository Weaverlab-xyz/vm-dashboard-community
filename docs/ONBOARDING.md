# Onboarding Guide — Infrastructure Management Dashboard (Community Edition)

This guide walks you from a fresh Windows machine to a running dashboard
deploying resources into your own AWS and Azure accounts. Target time:
**under 30 minutes**.

- [Part A — AWS setup](#part-a--aws-setup)
- [Part B — Azure setup](#part-b--azure-setup)
- [Part C — Run the dashboard](#part-c--run-the-dashboard)
- [Part D — Feature-test checklist](#part-d--feature-test-checklist)
- [Part E — Troubleshooting](#part-e--troubleshooting)
- [Appendix A — VMware Workstation integration](#appendix-a--vmware-workstation-integration)
- [Appendix B — Sign in with Microsoft (Entra OAuth)](#appendix-b--sign-in-with-microsoft-entra-oauth)
- [Appendix C — MCP server (AI client integration)](#appendix-c--mcp-server-ai-client-integration)
- [Appendix D — BeyondTrust integration](#appendix-d--beyondtrust-integration)

---

## Prerequisites (one-time)

Install these once per machine:

| Tool            | Why                                | Where                                                     |
|-----------------|------------------------------------|-----------------------------------------------------------|
| Docker Desktop  | Runs the dashboard and Postgres    | <https://www.docker.com/products/docker-desktop/>         |
| PowerShell 7+   | Runs the Windows onboarder (Windows only) | <https://aka.ms/powershell>                        |
| git             | Clone the repo                     | macOS: `xcode-select --install`; Windows: <https://git-scm.com/download/win>; Linux: your package manager |
| AWS CLI (v2)    | Create the IAM user and access key | <https://aws.amazon.com/cli/>                             |
| Azure CLI       | Create the Azure service principal | <https://learn.microsoft.com/cli/azure/install-azure-cli> |

Start Docker Desktop and wait for the whale/whale-like icon to settle
before continuing.

Clone the repo:

```bash
git clone <repo-url> vm-dashboard-community
cd vm-dashboard-community
```

---

## Part A — AWS setup

The dashboard deploys EC2 instances into **your** AWS account using an IAM
user dedicated to the dashboard.

### 1. Create the IAM user

```powershell
aws iam create-user --user-name dashboard-dev
aws iam attach-user-policy --user-name dashboard-dev --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess
aws iam attach-user-policy --user-name dashboard-dev --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
aws iam attach-user-policy --user-name dashboard-dev --policy-arn arn:aws:iam::aws:policy/IAMReadOnlyAccess
```

> **Why these policies:** `AmazonEC2FullAccess` for launching/terminating
> instances and creating AMIs; `AmazonS3ReadOnlyAccess` for reading OVA
> upload buckets when you import images; `IAMReadOnlyAccess` for looking
> up instance profiles during deploy.

### 2. Create the access key

```powershell
aws iam create-access-key --user-name dashboard-dev
```

Copy the `AccessKeyId` and `SecretAccessKey` from the output — you will
paste them into `.env` in Part C.

### 3. Pick a default region

The dashboard uses `AWS_REGION` as the default for all deploys. Common
picks: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`.

---

## Part B — Azure setup

The dashboard deploys Azure VMs into **your** Azure subscription using a
service principal (SP) with the Contributor role.

### 1. Log in and pick a subscription

```powershell
az login
az account show --query id -o tsv
```

Copy the subscription id.

### 2. Create the service principal

```powershell
az ad sp create-for-rbac `
  --name "dashboard-dev" `
  --role Contributor `
  --scopes /subscriptions/<your-subscription-id>
```

The output includes `appId`, `password`, and `tenant` — you need all three
(plus the subscription id) for `.env`.

> **Security note:** the client secret (`password`) rotates. Azure will
> warn you when it nears expiry; create a new one and update `.env`.

### 3. Pick a resource group and region

The `AZURE_RESOURCE_GROUP` value in `.env` becomes the default RG for
deployed VMs. It will be created on first deploy if it doesn't already
exist. Set `AZURE_LOCATION` to your preferred Azure region (e.g.
`centralus`, `eastus`, `westeurope`).

---

## Part C — Run the dashboard

Pick the onboarder that matches your host OS. Both do the same thing:
preflight checks, bootstrap `.env` (JWT and DB secrets only), bring up
Compose, poll `/api/health`, open the browser.

### 1. Run the onboard script (one command)

**Windows** (PowerShell 7):

```powershell
.\scripts\Onboard-Dashboard.ps1
```

**macOS / Linux / Raspberry Pi** (bash):

```bash
./scripts/onboard.sh
```

The script:

- Verifies Docker is running and `docker compose` is available.
- Copies `.env.example → .env` if missing (only bootstrap secrets needed — no cloud credentials in this file).
- Auto-generates `JWT_SECRET_KEY` and `POSTGRES_PASSWORD` if they're still at defaults.
- Brings up the Compose stack (`db` + `app`).
- Waits for `http://localhost:8000/api/health` to respond.
- Opens your browser.

### 2. Complete the setup wizard

Your browser opens to the **setup wizard**. It appears automatically on
first visit because no credentials are stored yet.

| Step | What to fill in |
|------|-----------------|
| **1 — Admin account** | Username and password you'll use to log in |
| **2 — AWS** | Access Key ID, Secret Access Key, and default region from Part A |
| **3 — Azure** | Service principal credentials from Part B. Optionally expand **Sign in with Microsoft** to add Entra OAuth (see Appendix B) |
| **4 — Feature flags** | Enable optional integrations — all default off (see Appendix A, C) |

Click **Complete setup**. Credentials are encrypted with AES-256 and
stored in the application database — not in any file on disk.

### 3. Log in

The wizard redirects to `/login`. Sign in with the username and password
you created in wizard Step 1.

### 4. Stopping and restarting

```bash
docker compose down              # stop the stack
./scripts/onboard.sh             # bring it back up (Windows: .\scripts\Onboard-Dashboard.ps1)
```

Postgres data persists in the `pgdata` Docker volume across restarts. The
wizard won't appear again — your credentials are already in the database.

### Reconfiguring credentials after first run

To update credentials or toggle feature flags after setup, navigate to
`/setup` in your browser while logged in as admin. The wizard reopens in
reconfigure mode: existing values are pre-filled, and leaving a secret
field blank keeps the stored value unchanged.

### Mac / Linux notes

- On Apple Silicon (M1/M2/M3/M4) the Docker images build natively as
  `linux/arm64` — no platform flag needed. The same applies to
  Raspberry Pi 5 (ARM64).
- The **VMware** feature flag (Appendix A) is Windows-only; do not enable
  it on macOS or Linux.
- The optional **MCP server** (Appendix C) needs no extra containers —
  it runs inside the main app. On Macs / Raspberry Pi that block is a
  no-op. Enable the chat profile; Ollama runs on CPU/Metal without it.

---

## Part D — Feature-test checklist

Run through this checklist after first login to confirm the stack is
healthy end-to-end.

- [ ] **Login.** Log in as `admin`. The dashboard page loads without
      browser console errors.
- [ ] **Change password.** Settings → Security → Change Password. Log
      out, log back in with the new password.
- [ ] **AWS: list AMIs.** AWS tab → the community-AMI gallery populates
      (Ubuntu, Amazon Linux, etc.). No 5xx errors.
- [ ] **AWS: deploy an instance.** Pick the smallest AMI, `t3.micro`,
      default VPC. Submit. Watch the Jobs page. Within ~90 seconds the
      instance appears in your AWS Console under the selected region.
- [ ] **AWS: terminate.** Back on the AWS tab, terminate the instance
      you just deployed. Confirm it disappears from both the dashboard
      and the AWS Console.
- [ ] **Azure: list images.** Azure tab → Marketplace tab shows the
      hardcoded Ubuntu/RHEL/Debian images. Private Images tab lists any
      managed images or Shared Image Gallery entries in your
      subscription (empty is fine).
- [ ] **Azure: deploy a VM.** Pick a Marketplace image,
      `Standard_B1s`, default networking. Submit. Within ~3 minutes the
      VM appears in your Azure Portal → Virtual Machines.
- [ ] **Azure: stop/delete.** Stop and delete the VM from the Azure tab.
      Confirm it disappears from both the dashboard and the portal.
- [ ] **Jobs.** The Jobs page lists all actions you just took with
      timestamps, durations, and status.

If any step fails, skip to [Part E](#part-e--troubleshooting).

---

## Part E — Troubleshooting

### Onboarding script exits at preflight

- **"PowerShell 7+ is required"** — you're running Windows PowerShell 5.
  Install PS7 (<https://aka.ms/powershell>) and rerun with `pwsh`.
- **"docker not found"** — Docker Desktop isn't installed or `docker`
  isn't on PATH. Reinstall Docker Desktop and restart your terminal.
- **"Docker daemon is not responding"** — Docker Desktop is installed
  but not running. Launch it and wait for the whale icon to settle.

### Stack starts but `/api/health` doesn't respond

```powershell
docker compose logs --tail 100 app
```

Common causes:

| Symptom in logs                                   | Likely cause                                      | Fix                                                                                          |
|---------------------------------------------------|---------------------------------------------------|----------------------------------------------------------------------------------------------|
| `InvalidClientTokenId` / `InvalidSignature`       | AWS access key wrong or rotated                   | Rerun `aws iam create-access-key`, then update via the reconfigure wizard (`/setup`)         |
| `AuthenticationFailed` from Azure                 | Azure SP secret wrong or expired                  | Regenerate with `az ad sp credential reset`, then update via the reconfigure wizard (`/setup`) |
| `connection refused` on port 5432                 | Postgres container not healthy                    | `docker compose ps`; check `db` container logs                                               |
| `Address already in use` on 8000                  | Another process is bound to 8000                  | Stop it, or change the port mapping in `docker-compose.yml`                                  |

### Login fails with "Invalid credentials"

- The admin account is created in **Step 1 of the setup wizard** on
  first run. Use the username and password you entered there.
- If you've forgotten the password, change it from **Settings → Security**
  while logged in, or reset the entire stack:
  ```bash
  docker compose down -v   # ⚠ wipes the database and all stored credentials
  ./scripts/onboard.sh     # brings it back up; wizard appears again on first visit
  ```

### Where to file issues

Open a GitHub issue with:

1. The output of `.\scripts\Onboard-Dashboard.ps1` (copy the terminal)
2. The last 100 lines of `docker compose logs app`
3. Your OS / Docker Desktop / PowerShell versions
4. What you expected vs. what happened

**Do not paste `.env` contents** — they contain your cloud credentials.

---

## Appendix A — VMware Workstation integration

Windows-only. Enables the dashboard to list, start, and stop VMware
Workstation VMs on your local machine by SSHing from the container to
the host.

### Prerequisites

- VMware Workstation Pro installed
- OpenSSH server enabled on Windows:
  ```powershell
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
  Start-Service sshd
  Set-Service -Name sshd -StartupType Automatic
  ```
- An SSH key for the container to authenticate with (use
  `scripts/Setup-DevSsh.ps1` if provided, or generate manually:
  `ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\dev_dashboard_key` and
  append the `.pub` to `%USERPROFILE%\.ssh\authorized_keys`).

### Enable the integration

1. Copy the Windows override example:
   ```powershell
   Copy-Item docker-compose.override.windows.yml.example docker-compose.override.windows.yml
   ```
2. Edit `.env`:
   ```
   VMWARE_ENABLED=true
   SSH_USER=<your Windows username>
   ```
3. Edit the override file and set `VM_CLI_WRAPPER_PATH` to the absolute
   path of `vm_cli_api_wrapper.ps1` on your host.
4. Restart the stack with both files:
   ```powershell
   docker compose -f docker-compose.yml -f docker-compose.override.windows.yml up -d
   ```
5. The "VMs" nav entry should now appear.

---

## Appendix B — Sign in with Microsoft (Entra OAuth)

Optional. Lets users log in with their work Microsoft account instead of
a local password.

### Create a second Azure app registration

This is a **different** registration from the resource-management service
principal in Part B.

1. Azure Portal → **App registrations** → **New registration**.
   - Name: `Dashboard OAuth (dev)`
   - Supported account types: single-tenant
2. **Authentication** → **Add platform** → **Web**.
   - Redirect URI: `http://localhost:8000/api/auth/oauth/azure/callback`
3. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated** → `openid`, `profile`, `email`.
4. **Certificates & secrets** → **New client secret**. Copy the value.

### Wire it up

**During initial setup:** In the setup wizard, go to Step 3 (Azure) and
expand the **Sign in with Microsoft — optional** panel. Enter the Client
ID, Client Secret, and Tenant ID, then complete the wizard as normal.

**After initial setup:** Navigate to `/setup` in your browser (admin
login required). The wizard reopens in reconfigure mode. Go to Step 3
and expand the OAuth panel — the Client ID and Tenant ID will be
pre-filled if already configured; leave the secret field blank to keep
the stored value.

The redirect URI is derived automatically from your browser's host —
you do not set it in the dashboard. Register the same URI that appears
in the wizard hint (`{your-host}/api/auth/oauth/azure/callback`) in the
Azure app registration under **Authentication**.

Once saved, the login page shows a **Sign in with Microsoft** button
without a restart.

Optional: map Entra group object IDs to dashboard workgroups from
**Settings → Groups** — users in a mapped group are auto-created and
assigned workgroups on first OAuth login.

---

## Appendix D — BeyondTrust integration

Optional. Enables two things when both are configured:

- **Secret retrieval (pscli / ps-cli)** — the dashboard checks out SSH
  keys and passwords from BeyondTrust Password Safe on demand, so
  credentials never need to be stored locally.
- **Session context (btapi)** — used to pass session metadata back to
  BeyondTrust PRA during remote access operations.

Both tools are separate binaries that must be present inside the container.
The dashboard feature flag controls both; configure the credentials that
apply to your deployment.

### Prerequisites

- A **BeyondTrust Password Safe** (Secrets Safe) tenant
- The **ps-cli** binary (`pscli`) accessible inside the container at the
  path set in `PSCLI_EXECUTABLE` (default: the system PATH)
- Optionally the **btapi** binary if your PRA tenant is separate from
  Password Safe

### Part 1 — Password Safe OAuth application (pscli)

pscli authenticates to Password Safe with an OAuth 2.0 client credentials
grant. Create the application in Password Safe:

1. **Password Safe** → **Configuration** → **API Registration** →
   **Add API Registration**.
   - Authentication type: **Client Credentials**
   - Copy the **Client ID** and **Client Secret**.
2. Grant the registration access to the secrets and managed accounts the
   dashboard needs. At minimum: **Secrets > Read**, **Requests > Create**,
   **Credentials > Read**.

### Part 2 — btapi credentials (if using PRA session recording)

btapi authenticates to the BeyondTrust PRA API with its own client
credentials. Obtain them from:

**BeyondTrust PRA** → **Configuration** → **API Configuration** →
**Add API Account**. Copy the **Client ID** and **Client Secret**.

The API host is the hostname of your PRA appliance
(e.g. `https://pra.company.com`). If your PRA and Password Safe are the
same appliance, the host and credentials may be the same as Part 1.

### Enable and configure

1. **Settings → Integrations** → toggle **BeyondTrust PRA** on.
   The configuration panel opens automatically.

2. Fill in the **Password Safe** section:

   | Field | Value |
   |-------|-------|
   | Password Safe URL | Base URL of your Password Safe instance, e.g. `https://ps.company.com` |
   | OAuth Client ID | From the API Registration you created in Part 1 |
   | OAuth Client Secret | From the API Registration |

3. Fill in the **btapi** section (leave blank if not using PRA session recording):

   | Field | Value |
   |-------|-------|
   | API Host | Your PRA appliance URL, e.g. `https://pra.company.com` |
   | Client ID | From the PRA API Account |
   | Client Secret | From the PRA API Account |

4. Click **Save**. No restart required.

> **Secret note:** Client secrets are encrypted with AES-256 in the
> application database. Leaving a secret field blank on a subsequent save
> keeps the stored value — you only need to re-enter secrets when rotating
> them.

---

## Appendix C — MCP server (AI client integration)

The dashboard exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io)
server at `/mcp`. Any compatible AI client — Claude Desktop, Claude Code,
Cursor, Continue, or any MCP-capable tool — can connect to it with read-only
access to jobs, VMs, EC2 instances, and Azure VMs.

No extra containers or services are needed — the server runs inside the main
`app` container.

### Step 1 — Create a Personal Access Token

1. Open the dashboard → **Settings** (top-right avatar or `/settings`).
2. Scroll to **Security Keys → API Tokens** (or go directly to `/tokens`).
3. Click **New Token**, give it a name (e.g. `claude-desktop`), set an
   expiry if desired, and click **Create**.
4. Copy the token — it looks like `vmcli_<64 hex characters>`.
   It is shown only once.

### Step 2 — Configure your AI client

#### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "vm-dashboard": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer vmcli_<your-token>"
      }
    }
  }
}
```

Restart Claude Desktop. A new **vm-dashboard** entry appears in the tool
picker.

#### Claude Code (CLI)

```bash
claude mcp add --transport http vm-dashboard http://localhost:8000/mcp \
  --header "Authorization: Bearer vmcli_<your-token>"
```

#### Other clients

Point the client at `http://<host>:8000/mcp` with the
`Authorization: Bearer vmcli_<token>` header. The server uses the
HTTP Streamable transport (SSE).

### Available tools

| Tool | Description |
|------|-------------|
| `dashboard_summary` | Active jobs, today's failures, enabled integrations |
| `list_jobs` | Recent jobs — filter by status and/or workgroup |
| `get_job` | Full detail for one job by UUID |
| `list_vms` | VMware VMs (requires VMware integration enabled) |
| `list_ec2_instances` | EC2 instances deployed via this dashboard |
| `list_amis` | Available AMIs from AWS |
| `list_azure_vms` | Azure VMs deployed via this dashboard |

All tools are **read-only**. Deploy, start, and stop actions must be
performed through the web UI.
