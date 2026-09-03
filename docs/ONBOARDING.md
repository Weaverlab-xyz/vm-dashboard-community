# Onboarding Guide — Infrastructure Management Dashboard (Community Edition)

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are setting the dashboard up for the first time and want the shortest path to a running instance.

This guide walks you from a fresh machine to a running dashboard deploying
resources into your own AWS, Azure, and GCP accounts. Target time:
**under 30 minutes**.

Supported hosts: **Windows** (PowerShell 7), **macOS**, **Linux**, and
**WSL** (Windows Subsystem for Linux — Docker Engine in WSL, no Docker
Desktop required).

> **Two ways to configure** (once the container is up — Part D):
> **Path A — the setup wizard:** paste your own cloud credentials and click
> through `/setup` (Parts A–C show how to obtain them; or skip a cloud to
> explore). **Path B — one script, no wizard:** the [Quick path: cloud
> sandbox](#quick-path-cloud-sandbox) `onboard-sandbox` script provisions a
> throwaway lab *and* pushes the config straight into the dashboard, so you never
> open `/setup`. Neither is required if you only want to explore the UI.

---

| Step | Page |
|---|---|
| Give it a cloud to deploy into | [AWS](onboarding/aws.md) · [Azure](onboarding/azure.md) · [GCP](onboarding/gcp.md) |
| Run it | [Prerequisites](#prerequisites-one-time), then [Part D](#part-d--run-the-dashboard) below |
| Check what works | [Feature-test checklist](onboarding/feature-test.md) |
| It will not start | [Troubleshooting](onboarding/troubleshooting.md) |
| It is running | [After the first run](onboarding/after-first-run.md) |

The fastest route to all three clouds at once is the [cloud sandbox](#quick-path-cloud-sandbox)
below, which bootstraps isolated lab infrastructure for you.

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

**Optional — only if you don't already have cloud credentials.** If you're
labbing this up — testing, demoing, or running training environments — these
scripts provision a disposable, **fully isolated** sandbox in AWS, Azure, and
GCP and print a credential block you paste into the wizard (Part D), letting you
skip the manual Parts A–C. The repo ships bash scripts (WSL / Linux / macOS) and
PowerShell equivalents (Windows).

**Fastest — skip the wizard too:** `onboard-sandbox` provisions *and* pushes the
config into the dashboard for you (creating your admin), so you never open `/setup`:

```bash
./scripts/sandbox/Linux/onboard-sandbox.sh --cloud all
# Windows:  .\scripts\sandbox\Windows\Onboard-Sandbox.ps1 -Cloud all
```

Or run the per-cloud scripts and paste their printed config into the wizard:

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
  Gateway container (internet egress so it can phone home to PRA), one
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

## Part D — Run the dashboard

Pick the onboarder that matches your host OS. Both do the same thing:
preflight checks, generate the JWT key file and bootstrap `.env` (DB
secret only), bring up Compose, poll `/api/health`, open the browser.

> **Wizard or script?** Step 1 starts the container — **both** paths need it.
> Step 2 is **Path A** (walk the wizard). For **Path B** (no wizard), do Step 1,
> then run the [Quick path: cloud sandbox](#quick-path-cloud-sandbox)
> `onboard-sandbox` script — it provisions a lab and configures the dashboard for
> you, and you log in directly.

### 1. Run the onboard script (one command)

By default the onboarder **builds the image from source**. Add `--hub` (bash)
/ `-Hub` (PowerShell) to **pull the prebuilt multi-arch image** from Docker Hub
(`chrweav/infra-dashboard`) instead — no local build, and it runs on both
AMD64 and ARM64 (Apple Silicon, AWS Graviton, Raspberry Pi 5).

**Windows** (PowerShell 7):

```powershell
.\scripts\Onboard-Dashboard.ps1 -Hub      # pull prebuilt image (drop -Hub to build from source)
```

**macOS / Linux / WSL / Raspberry Pi** (bash):

```bash
./scripts/onboard.sh --hub                # pull prebuilt image (drop --hub to build from source)
```

The script:

- Verifies Docker is running and `docker compose` is available.
- Copies `.env.example → .env` if missing (only bootstrap secrets needed — no cloud credentials in this file).
- Generates `.jwt_secret_key` (owner-read-only on disk — this is the root of trust
  for all encrypted credentials stored in the database). The file is excluded from git
  and from the Docker build context; it is mounted into the container at runtime via
  Docker Secrets and is never written to `.env`.
- Auto-generates `POSTGRES_PASSWORD` in `.env` if it's still at the placeholder value.
- Brings up the Compose stack (`db` + `app` + `worker`, the last at
  `WORKER_REPLICAS` replicas, default 3) — with `--hub`, from `docker-compose.hub.yml`
  using the published image (a `docker compose pull` runs first so reruns pick up new
  releases); otherwise it builds the `app` image locally.
- Waits for `http://localhost:8001/api/health` to respond.
- Opens your browser.

> **Behind a TLS-inspecting corporate proxy?** The `--hub` path skips the image
> build entirely, so the corp-CA build steps and the `ONBOARD_SKIP_DEBIAN_UPDATES`
> escape hatch are **not needed**. You do still need Docker to trust your corporate
> root CA for the `docker pull` itself — see
> [WSL: `docker pull` fails with a certificate error](onboarding/troubleshooting.md#wsl-docker-pull-fails-with-a-certificate-error)
> below. And if you use features that make outbound TLS calls *from inside the
> container* (Terraform-backed provisioning, e.g. cloud databases), add
> `--corp-ca` so the published image trusts your proxy's CA at runtime —
> step 5 of the same section.

### 2. Complete the setup wizard

Your browser opens to the **setup wizard**. It appears automatically on
first visit because no credentials are stored yet.

| Step | What to fill in |
|------|-----------------|
| **Admin account** | Username and password you'll use to log in |
| **AWS** | Access Key ID, Secret Access Key, and default region from Part A |
| **Azure** | Service principal credentials from Part B. Optionally expand **Sign in with Microsoft** to add Entra OAuth (see Appendix B) |
| **GCP** | Project ID, region/zone, and service account JSON key from Part C. Expand **Advanced** to set the SSH key secret name |
| **OCI** | Tenancy/user OCID, key fingerprint, private key and region. Every field may be left blank to skip OCI |
| **Feature flags** | Enable optional integrations — all default off (see Appendices A–F for on-prem hypervisors; Appendix J for MCP). Toggles only; the per-integration fields live in **Settings → Integrations** |

Steps are listed by name rather than number on purpose: the wizard's step list is data, and
citing an ordinal is how this table came to omit OCI and call Feature flags "step 5".

Click **Complete setup**. Credentials are encrypted with AES-256 and
stored in the application database — not in any file on disk.

### 3. Log in

The wizard redirects to `/login`. Sign in with the username and password
you created in wizard Step 1.

### 4. Stopping and restarting

```bash
docker compose down              # stop the stack (add -f docker-compose.hub.yml if you used --hub)
./scripts/onboard.sh             # bring it back up (Windows: .\scripts\Onboard-Dashboard.ps1)
```

If you started the stack with `--hub` / `-Hub`, keep that flag when you bring it
back up (`./scripts/onboard.sh --hub`) so it targets the published image.

Postgres data persists in the `pgdata` Docker volume across restarts. The
wizard won't appear again — your credentials are already in the database.

## Optional integrations

Every integration is enabled independently in **Settings → Integrations**, and each has
its own guide with that system's prerequisites, config keys and failure modes. This page
used to carry a summary of each; they drifted, so they are gone in favour of the page
that is maintained.

| Integration | Guide |
|---|---|
| VMware Workstation | [integrations/vmware.md](integrations/vmware.md) |
| VMware vSphere / ESXi | [integrations/vsphere.md](integrations/vsphere.md) |
| Proxmox VE | [integrations/proxmox.md](integrations/proxmox.md) |
| Microsoft Hyper-V | [integrations/hyperv.md](integrations/hyperv.md) |
| Nutanix AHV | [integrations/nutanix.md](integrations/nutanix.md) |
| XCP-ng / XenServer | [integrations/xcpng.md](integrations/xcpng.md) |
| BeyondTrust (Password Safe, PRA, EPM-L) | [integrations/beyondtrust.md](integrations/beyondtrust.md) |
| Entitle | [integrations/entitle.md](integrations/entitle.md) |
| Portainer CE | [integrations/portainer.md](integrations/portainer.md) |
| Remote Worker (Ansible + k8s runners) | [integrations/ansible.md](integrations/ansible.md) |
| Cloud Functions (preview) | [integrations/cloud-functions.md](integrations/cloud-functions.md) |
| MCP server (AI clients) | [integrations/mcp-server.md](integrations/mcp-server.md) |
| Single sign-on (any IdP) | [integrations/oidc.md](integrations/oidc.md) |
| Sign in with Microsoft (legacy Entra) | [integrations/entra-oauth.md](integrations/entra-oauth.md) |

Anything the dashboard cannot route to — a hypervisor on another network, a database
behind NAT — is reached through an agent instead: see [Remote Agents](remote-agents.md).
