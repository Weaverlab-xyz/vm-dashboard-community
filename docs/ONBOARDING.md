# Onboarding Guide — Infrastructure Management Dashboard (Community Edition)

This guide walks you from a fresh machine to a running dashboard deploying
resources into your own AWS, Azure, and GCP accounts. Target time:
**under 30 minutes**.

Supported hosts: **Windows** (PowerShell 7), **macOS**, **Linux**, and
**WSL** (Windows Subsystem for Linux — Docker Engine in WSL, no Docker
Desktop required).

- [Quick path: cloud sandbox](#quick-path-cloud-sandbox) — automated, isolated lab infra in any/all of AWS/Azure/GCP
- [Part A — AWS setup](#part-a--aws-setup)
- [Part B — Azure setup](#part-b--azure-setup)
- [Part C — GCP setup](#part-c--gcp-setup)
- [Part D — Run the dashboard](#part-d--run-the-dashboard)
- [Part E — Feature-test checklist](#part-e--feature-test-checklist)
- [Part F — Troubleshooting](#part-f--troubleshooting)
- [Appendix A — VMware Workstation integration](#appendix-a--vmware-workstation-integration)
- [Appendix B — VMware vSphere / ESXi integration](#appendix-b--vmware-vsphere--esxi-integration)
- [Appendix C — Proxmox VE integration](#appendix-c--proxmox-ve-integration)
- [Appendix D — Microsoft Hyper-V integration](#appendix-d--microsoft-hyper-v-integration)
- [Appendix E — Nutanix AHV integration](#appendix-e--nutanix-ahv-integration)
- [Appendix F — XCP-ng / XenServer integration](#appendix-f--xcp-ng--xenserver-integration)
- [Appendix G — Sign in with Microsoft (Entra OAuth)](#appendix-g--sign-in-with-microsoft-entra-oauth)
- [Appendix H — BeyondTrust integration](#appendix-h--beyondtrust-integration)
- [Appendix I — Entitle approval workflows](#appendix-i--entitle-approval-workflows)
- [Appendix J — MCP server (AI client integration)](#appendix-j--mcp-server-ai-client-integration)
- [Appendix K — Portainer CE integration](#appendix-k--portainer-ce-integration)
- [Appendix L — Ansible config management](#appendix-l--ansible-config-management)

---

## Prerequisites (one-time)

Install these once per machine:

| Tool            | Why                                | Where                                                     |
|-----------------|------------------------------------|-----------------------------------------------------------|
| Docker          | Runs the dashboard and Postgres    | **Windows/Mac:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) · **Linux/WSL:** [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) |
| PowerShell 7+   | Runs the Windows onboarder (Windows only) | <https://aka.ms/powershell>                        |
| git             | Clone the repo                     | macOS: `xcode-select --install`; Windows: <https://git-scm.com/download/win>; Linux/WSL: your package manager |
| AWS CLI (v2)    | Create the IAM user and access key | <https://aws.amazon.com/cli/>                             |
| Azure CLI       | Create the Azure service principal | <https://learn.microsoft.com/cli/azure/install-azure-cli> |
| gcloud CLI      | Create the GCP service account (optional) | <https://cloud.google.com/sdk/docs/install> |

**Windows / macOS:** Start Docker Desktop and wait for the whale icon to
settle before continuing.

**Linux / WSL:** Start Docker Engine with `sudo service docker start` (or
`sudo systemctl start docker` if your distro uses systemd). Add your user
to the `docker` group once so you don't need `sudo` on every command:

```bash
sudo usermod -aG docker $USER
# then open a new terminal (or run: newgrp docker)
```

Clone the repo:

```bash
git clone <repo-url> vm-dashboard-community
cd vm-dashboard-community
```

---

## Quick path: cloud sandbox

If you're labbing this up — testing the dashboard, demoing it, or running
training environments — there's a faster alternative to Parts A, B, and C
below. The repo ships bash scripts (WSL / Linux / macOS) and PowerShell
equivalents (Windows) that provision **fully isolated** sandbox
infrastructure in AWS, Azure, and GCP with a single command each:

```bash
# Bash (WSL / Linux / macOS)
./scripts/sandbox/Linux/00-prereqs.sh        # one-time prereq check
./scripts/sandbox/Linux/setup-aws.sh         # provision AWS sandbox
./scripts/sandbox/Linux/setup-azure.sh       # provision Azure sandbox
./scripts/sandbox/Linux/setup-gcp.sh         # provision GCP sandbox
./scripts/sandbox/Linux/rollback.sh --cloud all -y   # tear it all down
```

```powershell
# PowerShell (Windows)
.\scripts\sandbox\Windows\Test-SandboxPrereqs.ps1
.\scripts\sandbox\Windows\Setup-AwsSandbox.ps1
.\scripts\sandbox\Windows\Setup-AzureSandbox.ps1
.\scripts\sandbox\Windows\Setup-GcpSandbox.ps1
.\scripts\sandbox\Windows\Rollback-Sandbox.ps1 -Cloud all -Yes
```

Both variants are functionally equivalent — same resources, same tags,
same idempotency, same printed config block. Pick whichever fits your
shell.

What you get per cloud:

- A dedicated VPC/VNet with two subnets — one for the BeyondTrust SRA
  Jumpoint container (internet egress so it can phone home to PRA), one
  for your lab VMs (**no** internet egress).
- A managed-by-dashboard service principal / IAM role / service account
  with the minimum permissions needed.
- An SSH key pair stored as `{public_key, private_key}` JSON in the
  cloud's secret manager.
- Tagged resources so the rollback script can clean up reliably.

Each setup script ends with a config block to paste into the dashboard's
`/setup` wizard or **Settings → Integrations** panels — those values
replace the manual setup in Parts A/B/C below.

**When to use the sandbox path:**
- ✅ Repeatable, isolated lab environments for testing or demos.
- ✅ One-command tear-down at the end of a session.
- ✅ Network isolation between deployed VMs and the public internet.
- ❌ Production or shared dashboards with existing cloud infra — the
  scripts create new VPCs/VNets and assume they own them.

**See [docs/CLOUD_SANDBOX.md](CLOUD_SANDBOX.md)** for the full walkthrough:
topology diagrams per cloud, cost estimates, verification, customisation
hooks, and troubleshooting. The [`scripts/sandbox/README.md`](../scripts/sandbox/README.md)
also has a one-line summary per file if you want a quick orientation
before reading the long doc.

After running the sandbox scripts, **skip ahead to
[Part D — Run the dashboard](#part-d--run-the-dashboard)** — Parts A, B,
and C are the manual alternative.

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
paste them into `.env` in Part D.

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

## Part C — GCP setup

The dashboard deploys Compute Engine instances into **your** GCP project using
a service account. GCP is optional — AWS and Azure work without it.

### 1. Prerequisites

Install the Google Cloud CLI (gcloud) if you haven't already:
<https://cloud.google.com/sdk/docs/install>

```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
```

### 2. Enable required APIs

```bash
gcloud services enable compute.googleapis.com secretmanager.googleapis.com
```

### 3. Create a service account and download a key

```bash
# Create the service account
gcloud iam service-accounts create dashboard-sa \
  --display-name "VM Dashboard SA"

# Grant Compute Admin and Secret Manager accessor
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role "roles/compute.admin"

gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role "roles/secretmanager.secretAccessor"

# Download the JSON key
gcloud iam service-accounts keys create sa-key.json \
  --iam-account "dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com"
```

Keep `sa-key.json` safe. You'll paste its entire contents into the wizard.

### 4. (Optional) Store an SSH key pair in Secret Manager

If you want the dashboard to inject SSH keys automatically:

```bash
# Create a JSON secret with your public key
echo '{"public_key":"ssh-rsa AAAA... user@host"}' | \
  gcloud secrets create my-ssh-keypair \
    --data-file=- \
    --replication-policy=automatic

# Grant the service account access (if not already inherited from secretAccessor above)
gcloud secrets add-iam-policy-binding my-ssh-keypair \
  --member "serviceAccount:dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role "roles/secretmanager.secretAccessor"
```

Note the secret name (`my-ssh-keypair`) — you'll enter it in the wizard.

### 5. Enter credentials in the wizard

When you run the onboard script, the wizard Step 4 (GCP) asks for:

| Field | Where to get it |
|-------|-----------------|
| Project ID | `gcloud config get project` |
| Region | Your preferred GCP region (e.g. `us-central1`) |
| Zone | A zone in that region (e.g. `us-central1-a`) |
| Service Account JSON | Full contents of `sa-key.json` |
| SSH Key Secret Name | Name of the Secret Manager secret from step 4 |

---

## Part D — Run the dashboard

Pick the onboarder that matches your host OS. Both do the same thing:
preflight checks, generate the JWT key file and bootstrap `.env` (DB
secret only), bring up Compose, poll `/api/health`, open the browser.

### 1. Run the onboard script (one command)

**Windows** (PowerShell 7):

```powershell
.\scripts\Onboard-Dashboard.ps1
```

**macOS / Linux / WSL / Raspberry Pi** (bash):

```bash
./scripts/onboard.sh
```

The script:

- Verifies Docker is running and `docker compose` is available.
- Copies `.env.example → .env` if missing (only bootstrap secrets needed — no cloud credentials in this file).
- Generates `.jwt_secret_key` (owner-read-only on disk — this is the root of trust
  for all encrypted credentials stored in the database). The file is excluded from git
  and from the Docker build context; it is mounted into the container at runtime via
  Docker Secrets and is never written to `.env`.
- Auto-generates `POSTGRES_PASSWORD` in `.env` if it's still at the placeholder value.
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
| **4 — GCP** | Project ID, region/zone, and service account JSON key from Part C. Expand **Advanced** to set the SSH key secret name |
| **5 — Feature flags** | Enable optional integrations — all default off (see Appendices A–F for on-prem hypervisors; Appendix J for MCP) |

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

### Migrate the JWT key to a cloud secrets manager (strongly recommended)

`.jwt_secret_key` is the root of trust for the entire application — every
integration credential stored in the database is encrypted with a key derived
from it. While the onboard script protects it with owner-only filesystem
permissions, it remains a plaintext file on disk, which is not appropriate for
long-lived or shared deployments.

**After completing the setup wizard, migrate the key to your secrets manager:**

1. Log in as admin and go to **Settings → Secrets Backend** (`/secrets`).
2. Choose your provider and enter the target secret name:
   - **AWS Secrets Manager** — requires AWS credentials configured in the wizard
   - **Azure Key Vault** — requires the Azure SP and Key Vault URL configured in the wizard
   - **GCP Secret Manager** — requires GCP service account configured in the wizard
   - **BeyondTrust Secrets Safe** — requires BeyondTrust configured under feature flags
3. Click **Migrate** — the dashboard uploads the key to your vault, updates its
   internal reference, and on all future startups reads the key from the cloud
   rather than the local file.
4. Once migration is confirmed, delete `.jwt_secret_key` from the repo directory.

After migration the local file is no longer needed. Your vault's access controls,
audit logging, and key rotation capabilities become the security boundary instead
of filesystem permissions.

### Platform notes

- **WSL (Windows Subsystem for Linux):** Docker Desktop is not required.
  Install Docker Engine inside your WSL distro, start it with
  `sudo service docker start`, then run `./scripts/onboard.sh`. The
  script detects WSL automatically: it prints WSL-specific hints if the
  daemon isn't running, and opens the dashboard in your Windows-side
  browser (via `wslview` if installed, otherwise `cmd.exe /c start`).
  Ports from WSL2 are automatically forwarded to Windows, so
  `http://localhost:8000` works in your Windows browser without any extra
  configuration.
- **Apple Silicon (M1/M2/M3/M4):** Docker images build natively as
  `linux/arm64` — no platform flag needed. The same applies to
  Raspberry Pi 5 (ARM64).
- The **VMware Workstation** feature flag (Appendix A) is Windows host-only;
  do not enable it on macOS, Linux, or WSL. The **VMware vSphere / ESXi**
  flag (Appendix B) connects to a remote vCenter/ESXi host and works on any
  OS.
- The optional **MCP server** (Appendix C) needs no extra containers —
  it runs inside the main app and is always available once the stack is up.
- **Portainer**, **Ansible**, **Proxmox VE**, **VMware vSphere / ESXi**,
  **Microsoft Hyper-V**, **Nutanix AHV**, **XCP-ng / XenServer**, and
  **Entitle** are optional integrations with their own backing infrastructure.
  See the detailed guides in [`docs/integrations/`](integrations/).
- **Secrets management** — how credentials are encrypted, how to migrate to an
  external vault, and security best practices: [`docs/secrets-management.md`](secrets-management.md).
- **Storage management** — where playbooks and asset files live, and how to
  migrate between cloud object stores or a corporate file share:
  [`docs/storage-management.md`](storage-management.md).
- **Config management** — philosophy, best practices, and how the
  dashboard's ephemeral-runner approach reduces secret sprawl:
  [`docs/config-management.md`](config-management.md).
- **Infrastructure as code** — how cloud VMs, Shell Jumps, and images
  are provisioned through Terraform/Packer modules with per-job state:
  [`docs/infrastructure-as-code.md`](infrastructure-as-code.md).

---

## Part E — Feature-test checklist

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

If any step fails, skip to [Part F](#part-f--troubleshooting).

---

## Part F — Troubleshooting

### Onboarding script exits at preflight

- **"PowerShell 7+ is required"** — you're running Windows PowerShell 5.
  Install PS7 (<https://aka.ms/powershell>) and rerun with `pwsh`.
- **"docker not found"** — Docker isn't installed or isn't on `PATH`.
  Windows/Mac: reinstall Docker Desktop. Linux/WSL: install Docker Engine
  (`sudo apt install docker.io`) and restart your terminal.
- **"Docker daemon is not responding"** — Windows/Mac: Docker Desktop is
  installed but not running — launch it and wait for the whale icon to
  settle. Linux/WSL: run `sudo service docker start` (or
  `sudo systemctl start docker`) then rerun the script.

### WSL: `docker pull` fails with a certificate error

**Symptom:** `docker pull postgres:16-alpine` (or any image) fails with:
```
x509: certificate signed by unknown authority
```

**Cause:** Your network uses an SSL-inspection proxy (Zscaler, Palo Alto, etc.)
that re-signs outbound TLS traffic with a corporate root CA. WSL does not
inherit Windows' trusted root store, so Docker inside WSL rejects the
intercepted certificate.

**Fix — run once per WSL distro install:**

**Step 1 — identify and export the proxy root CA (PowerShell on Windows):**

```powershell
# List trusted roots — look for your security vendor (Zscaler, etc.)
Get-ChildItem Cert:\LocalMachine\Root | Select-Object Subject, Thumbprint | Sort-Object Subject

# Export the relevant cert (replace <Thumbprint> with the value above)
$cert = Get-ChildItem Cert:\LocalMachine\Root\<Thumbprint>
Export-Certificate -Cert $cert -FilePath "$env:TEMP\corp-root.cer" -Type CERT
```

If you are unsure which cert to export, export them all and let WSL sort it out:

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\roots" | Out-Null
Get-ChildItem Cert:\LocalMachine\Root | ForEach-Object {
    Export-Certificate -Cert $_ `
        -FilePath "$env:TEMP\roots\$($_.Thumbprint).cer" -Type CERT
}
```

**Step 2 — import into WSL and update the system trust store:**

```bash
# Single cert
openssl x509 -inform DER \
    -in /mnt/c/Users/$(cmd.exe /c echo %USERNAME% 2>/dev/null | tr -d '\r')/AppData/Local/Temp/corp-root.cer \
    -out /tmp/corp-root.pem
sudo cp /tmp/corp-root.pem /usr/local/share/ca-certificates/corp-root.crt
sudo update-ca-certificates
```

If you exported all certs, convert and import them in a loop:

```bash
WINTEMP="/mnt/c/Users/$(cmd.exe /c echo %USERNAME% 2>/dev/null | tr -d '\r')/AppData/Local/Temp/roots"
sudo mkdir -p /usr/local/share/ca-certificates/windows-roots
for f in "$WINTEMP"/*.cer; do
    name=$(basename "$f" .cer)
    openssl x509 -inform DER -in "$f" \
        -out "/usr/local/share/ca-certificates/windows-roots/$name.crt" 2>/dev/null || true
done
sudo update-ca-certificates
```

**Step 3 — add the cert to Docker's registry trust store:**

```bash
sudo mkdir -p /etc/docker/certs.d/registry-1.docker.io
sudo cp /tmp/corp-root.pem /etc/docker/certs.d/registry-1.docker.io/ca.crt
sudo service docker restart
```

**Verify:**

```bash
docker pull hello-world
```

If that succeeds, rerun `./scripts/onboard.sh`.

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

### JWT key file: backup and loss recovery

`.jwt_secret_key` at the repo root is the **root of trust** for all credentials
you store through the setup wizard. The app uses it to encrypt every integration
secret (AWS keys, Azure SP credentials, etc.) in the database.

**Migrate it as soon as possible** — see [Migrate the JWT key to a cloud secrets
manager](#migrate-the-jwt-key-to-a-cloud-secrets-manager-strongly-recommended)
above. Once migrated, the local file can be deleted and the cloud vault becomes
the security boundary.

**Until you migrate:** back it up somewhere safe (password manager, encrypted
drive). Do not commit it to git (it's in `.gitignore`).

**If you lose it**, every stored credential is unrecoverable and the app will
refuse to start (the key file is required). Recovery procedure:

```bash
# 1. Stop the stack
docker compose down

# 2. Remove the old key and database volume (⚠ wipes all stored credentials)
rm .jwt_secret_key
docker volume rm vm-dashboard-community_pgdata   # adjust prefix to match 'docker volume ls'

# 3. Rerun the onboard script — it regenerates the key and the wizard reappears
./scripts/onboard.sh
```

**Rotating the key** is not currently supported without clearing the database.

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

> **Full guide:** [docs/integrations/vmware.md](integrations/vmware.md)

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

## Appendix B — VMware vSphere / ESXi integration

> **Full guide:** [docs/integrations/vsphere.md](integrations/vsphere.md)

Optional. Connects to a vCenter Server or standalone ESXi host via the
vSphere Web Services API (pyVmomi). Supports VM power operations and status
listing. Works with vCenter 6.7+ and ESXi 6.7+. Works on any host OS —
not Windows-only.

### Enable the integration

1. **Settings → Integrations** → toggle **VMware vSphere / ESXi** on.
2. Fill in the vCenter or ESXi hostname, a read/write user (e.g. a dedicated
   service account with the VM power-user role), and the datacenter name.
   Standalone ESXi uses `ha-datacenter`.
3. Click **Save**. No restart required.

---

## Appendix C — Proxmox VE integration

> **Full guide:** [docs/integrations/proxmox.md](integrations/proxmox.md)

Optional. Connects to a Proxmox VE node or cluster via the Proxmox REST API.
Supports VM and LXC container start, stop, reboot, and status listing. Works
with Proxmox VE 6.x and later.

### Enable the integration

1. **Settings → Integrations** → toggle **Proxmox VE** on.
2. Fill in your Proxmox host URL, a user with API access (e.g.
   `root@pam` or a dedicated API user), and the corresponding API token or
   password.
3. Click **Save**. No restart required.

See the full guide for creating a least-privilege API token on the Proxmox
side.

---

## Appendix D — Microsoft Hyper-V integration

> **Full guide:** [docs/integrations/hyperv.md](integrations/hyperv.md)

Optional. Manages Hyper-V VMs on a Windows Server or Windows 10/11 Pro host
via WinRM (Windows Remote Management) and remote PowerShell. No agent is
required on the host.

### Prerequisites

- Windows Server 2016–2025 or Windows 10/11 Pro / Enterprise with Hyper-V
  enabled on the target host.
- WinRM enabled and reachable from the container:
  ```powershell
  Enable-PSRemoting -Force
  ```
- A Windows user account (local or domain) with Hyper-V Administrator rights
  on the target host.

### Enable the integration

1. **Settings → Integrations** → toggle **Microsoft Hyper-V** on.
2. Fill in the Hyper-V host address, username, and password.
3. Click **Save**. No restart required.

---

## Appendix E — Nutanix AHV integration

> **Full guide:** [docs/integrations/nutanix.md](integrations/nutanix.md)

Optional. Connects to **Prism Central** (or Prism Element) via the Nutanix
REST API v3. Supports VM start, graceful ACPI shutdown, force stop, reboot,
and status listing. Graceful shutdown requires Nutanix Guest Tools to be
installed in the VM.

### Enable the integration

1. **Settings → Integrations** → toggle **Nutanix AHV** on.
2. Fill in the Prism Central hostname, a Prism user account (with VM power
   operation permissions), and the password.
3. Click **Save**. No restart required.

---

## Appendix F — XCP-ng / XenServer integration

> **Full guide:** [docs/integrations/xcpng.md](integrations/xcpng.md)

Optional. Connects to an XCP-ng or XenServer host or pool master via the
XAPI XML-RPC API. Supports VM start, clean shutdown, force shutdown, reboot,
and status listing.

### Enable the integration

1. **Settings → Integrations** → toggle **XCP-ng / XenServer** on.
2. Fill in the host URL (e.g. `https://xcp-host.local`), username (typically
   `root`), and password.
3. Click **Save**. No restart required.

---

## Appendix G — Sign in with Microsoft (Entra OAuth)

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

## Appendix H — BeyondTrust integration

Optional. Enables two things when both are configured:

> **Full guide:** [docs/integrations/beyondtrust.md](integrations/beyondtrust.md)

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

## Appendix I — Entitle approval workflows

> **Full guide:** [docs/integrations/entitle.md](integrations/entitle.md)

Optional. Gates sensitive dashboard actions (VM deploy, termination, etc.)
behind an **Entitle** approval workflow. Entitle is a BeyondTrust company.
Requires an active Entitle tenant.

### Enable the integration

1. **Settings → Integrations** → toggle **Entitle approval workflows** on.
2. Fill in your Entitle API endpoint and API key from the Entitle admin
   console.
3. Click **Save**. No restart required.

---

## Appendix J — MCP server (AI client integration)

> **Full guide:** [docs/integrations/mcp-server.md](integrations/mcp-server.md)

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

---

## Appendix K — Portainer CE integration

> **Full guide:** [docs/integrations/portainer.md](integrations/portainer.md)

Optional. Connects to a self-hosted **Portainer CE** instance to manage
Docker containers from the dashboard.

### Enable the integration

1. **Settings → Integrations** → toggle **Portainer CE** on.
2. Fill in the Portainer base URL and an API access token (created in
   Portainer under **Account → Access Tokens**).
3. Click **Save**. No restart required.

---

## Appendix L — Ansible config management

> **Full guide:** [docs/integrations/ansible.md](integrations/ansible.md)

Optional. Enables the **Config Mgmt** tab for running Ansible playbooks
against managed VMs. Playbooks can run via local Docker or a remote
AWS ECS Fargate cluster.

### Enable the integration

1. **Settings → Integrations** → toggle **Ansible config management** on.
2. Configure your playbook runner (local Docker socket or ECS cluster ARN)
   in the configuration panel.
3. Click **Save**. No restart required.
