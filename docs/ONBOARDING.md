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
- [Appendix C — Local chat assistant (Ollama)](#appendix-c--local-chat-assistant-ollama)

---

## Prerequisites (one-time)

Install these once per machine:

| Tool            | Why                                  | Where                                                     |
|-----------------|--------------------------------------|-----------------------------------------------------------|
| Docker Desktop  | Runs the dashboard and Postgres      | <https://www.docker.com/products/docker-desktop/>         |
| PowerShell 7+   | Runs `scripts/Onboard-Dashboard.ps1` | <https://aka.ms/powershell>                               |
| git             | Clone the repo                       | <https://git-scm.com/download/win>                        |
| AWS CLI (v2)    | Create the IAM user and access key   | <https://aws.amazon.com/cli/>                             |
| Azure CLI       | Create the Azure service principal   | <https://learn.microsoft.com/cli/azure/install-azure-cli> |

Start Docker Desktop and wait for the whale icon to settle before
continuing.

Clone the repo:

```powershell
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

### 1. First run

```powershell
.\scripts\Onboard-Dashboard.ps1
```

On first run the script copies `.env.example` to `.env` and opens it in
Notepad. Fill in the **Required: AWS** and **Required: Azure** sections
with the credentials you gathered in Parts A and B. Save and close
Notepad.

### 2. Second run

```powershell
.\scripts\Onboard-Dashboard.ps1
```

This time the script:

- Generates `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, and
  `FIRST_RUN_ADMIN_PASSWORD` (the values stay in `.env`).
- Validates that no `REPLACE_ME_*` placeholders remain.
- Brings up the Compose stack (`db` + `app`).
- Waits for `http://localhost:8000/api/health` to respond.
- Opens your browser at <http://localhost:8000>.

### 3. Log in

Username: `admin`. Password: the `FIRST_RUN_ADMIN_PASSWORD` value from
`.env`. Change the password from **Settings → Security** after your first
login — the `FIRST_RUN_ADMIN_PASSWORD` env var is ignored once any user
exists.

### 4. Stopping and restarting

```powershell
docker compose down          # stop the stack
.\scripts\Onboard-Dashboard.ps1  # bring it back up
```

Postgres data persists in a named Docker volume (`pgdata`) across
restarts.

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

| Symptom in logs                                   | Likely cause                                      | Fix                                                                    |
|---------------------------------------------------|---------------------------------------------------|------------------------------------------------------------------------|
| `InvalidClientTokenId` / `InvalidSignature`       | AWS access key wrong or rotated                   | Rerun `aws iam create-access-key`, update `.env`, `docker compose up`  |
| `AuthenticationFailed` from Azure                 | Azure SP secret wrong or expired                  | Regenerate with `az ad sp credential reset`, update `.env`             |
| `connection refused` on port 5432                 | Postgres container not healthy                    | `docker compose ps`; check `db` container logs                          |
| `Address already in use` on 8000                  | Another process is bound to 8000                  | Stop it, or change the port mapping in `docker-compose.yml`            |

### Login fails with "Invalid credentials"

- The admin account is only bootstrapped on the **very first** startup
  when the users table is empty. If you've changed
  `FIRST_RUN_ADMIN_PASSWORD` after first boot, it has no effect. Use the
  password as it was when you first started the stack, then change it
  from Settings.
- To reset: `docker compose down -v` (⚠ wipes the database) then rerun
  the onboarding script.

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

Set in `.env`:

```
AZURE_OAUTH_CLIENT_ID=<the app's Application (client) ID>
AZURE_OAUTH_CLIENT_SECRET=<the secret you just copied>
AZURE_OAUTH_TENANT_ID=<your tenant id>
```

Restart the stack. The login page now shows a "Sign in with Microsoft"
button.

Optional: map Entra group object IDs to dashboard workgroups using
`AZURE_OAUTH_GROUP_MAP` in `.env` — users in a listed group are
auto-created on first login.

---

## Appendix C — Local chat assistant (Ollama)

Optional. Runs a local LLM in a sibling container for a natural-language
dashboard assistant.

### Enable

1. In `.env`, set `CHAT_ENABLED=true`.
2. Start the chat profile alongside the main stack:
   ```powershell
   docker compose --profile chat up -d
   ```
3. The first request after startup pulls the model (~5 GB for the default
   `llama3.1:8b`). Expect a 2–5 minute delay on cold start.
4. A "Chat" nav entry appears in the header.

### GPU acceleration

The `deploy` block in the `ollama` service requests an NVIDIA GPU. If
your host has no NVIDIA GPU, comment that block out in
`docker-compose.yml` before starting the profile, otherwise Compose will
refuse to start the service.
