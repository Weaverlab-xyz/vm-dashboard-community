# BeyondTrust Integrations

## What is it?

The dashboard integrates with four BeyondTrust products. Each has its own page and its
own feature flag; this page is the map, plus the parts that belong to no single product.

- **Password Safe / Secrets Safe** — on-demand checkout of SSH keys and passwords.
  Target credentials (AWS keys, Azure service principal secrets, SSH private keys) are
  fetched at the moment the dashboard needs them and discarded after use, rather than
  stored in the dashboard's encrypted database. It also **onboards** resources the
  dashboard builds — VMs, cloud databases, Kubernetes ServiceAccount tokens — as managed
  systems + accounts. Driven by `ps-cli`. `password_safe_enabled`. See
  [Password Safe](password-safe.md).
- **Privileged Remote Access (PRA/SRA)** — brokered access to everything the dashboard
  builds. The dashboard creates and tears down **Shell Jump**, **Web Jump**, **Remote
  RDP** and **Protocol Tunnel** jump items, plus PRA Vault accounts, so operators reach
  VMs, container UIs, databases and Kubernetes API servers through PRA rather than
  direct network exposure. Driven by the `sra` Terraform provider plus a small REST
  client for the few calls the provider can't make. It also runs the **Gateway hosts**
  those jumps are brokered through. `pra_enabled`. See
  [Privileged Remote Access](privileged-remote-access.md) and
  [Gateway hosts](gateways.md).
- **Endpoint Privilege Management for Linux (EPM-L)** — agent package builds, sync to
  asset storage, and installation tokens, through the BeyondTrust Pathfinder API.
  `epml_enabled`. See [EPM-L](epml.md).
- **Entitle** — just-in-time cloud identity and access requests. `entitle_enabled`. See
  [Entitle](entitle.md).

---

## Four products, four feature flags

| Product | Flag | Settings panel | Page |
|---|---|---|---|
| Password Safe / Secrets Safe | `password_safe_enabled` | Settings → Integrations → **Password Safe** | [password-safe.md](password-safe.md) |
| Privileged Remote Access | `pra_enabled` | Settings → Integrations → **Privileged Remote Access** | [privileged-remote-access.md](privileged-remote-access.md) |
| EPM for Linux | `epml_enabled` | Settings → Integrations → **EPM for Linux** | [epml.md](epml.md) |
| Entitle | `entitle_enabled` | Settings → Integrations → **Entitle** | [entitle.md](entitle.md) |

Password Safe, PRA and EPM-L used to share a single `beyondtrust_enabled` flag and a
single Settings panel. They are now independent, because customers routinely license one
or two of the three: a Password Safe deployment with no PRA no longer renders Gateway
tabs and jump-item options it cannot use, and a PRA deployment needs no Secrets Safe
licence to provision jump items.

Upgrading an existing install carries your old setting forward — the first boot after the
upgrade copies whatever `beyondtrust_enabled` said into all three new flags, so nothing
turns on or off by surprise. The legacy key is left in place, not deleted, so rolling
back to a previous image is a no-op.

These are all **Settings keys**, not environment variables. The dashboard's config store
reads from the database only; exporting an equivalently-named env var has no effect.

> **Independently *gated* is not independently *behaved*.** Some paths genuinely need two
> products live, and enabling only one leaves them inert rather than broken:
> [Kubernetes token rotation](password-safe.md#kubernetes-serviceaccount-token-rotation)
> rotates a token whose ServiceAccount PRA injects; a database tunnel is PRA, while the
> credential it carries can be Password Safe-managed; and
> `bt_ps_deploy_key_title` stores a PRA Gateway's Docker deploy key in Password Safe.
> Each page says so where it applies.

---

## Which page do I want?

| I want to… | Page |
|---|---|
| Check out a secret or managed-account credential at runtime | [Password Safe](password-safe.md) |
| Onboard a VM or cloud database as a managed system | [Password Safe](password-safe.md#password-safe-vm-onboarding-managed-systems) |
| Rotate a Kubernetes ServiceAccount token | [Password Safe](password-safe.md#kubernetes-serviceaccount-token-rotation) |
| Create Shell Jump / Web Jump / RDP / tunnel jump items | [Privileged Remote Access](privileged-remote-access.md) |
| Mint PRA Vault accounts for tunnel credentials | [Privileged Remote Access](privileged-remote-access.md) |
| Deploy or inventory more Gateway hosts | [Gateway hosts](gateways.md) |
| Build EPM-L agent packages or mint installation tokens | [EPM-L](epml.md) |
| Request just-in-time cloud access | [Entitle](entitle.md) |
| Pre-condition an image for any of the above | [below](#preparing-images-for-bt-management) |

---

## Preparing images for BT management

This section stays on the hub because it is keyed to an **artifact, not a product**: one
run of `bt-ready-debian.sh` hardens sshd for PRA Shell Jump, creates the Password Safe
bootstrap account, and installs the EPM-L package — and its env vars all go into the same
Packer build form.

Images built by the dashboard's Packer flow (`/images/aws`, `/images/azure`, `/images/gcp`) can be pre-conditioned for BeyondTrust pickup using the provisioner scripts under [`provisioners/beyondtrust/`](../../provisioners/beyondtrust/):

| Script | Targets | Packer provisioner |
|---|---|---|
| [`bt-ready-debian.sh`](../../provisioners/beyondtrust/bt-ready-debian.sh) | Debian, Ubuntu | `shell` |
| [`bt-ready-rpm.sh`](../../provisioners/beyondtrust/bt-ready-rpm.sh) | RHEL, Rocky, CentOS Stream, AlmaLinux, Amazon Linux 2 / 2023 | `shell` |
| [`bt-ready-windows.ps1`](../../provisioners/beyondtrust/bt-ready-windows.ps1) | Windows Server, incl. Server Core | `powershell` |
| [`bt-ready-windows11-vdi.ps1`](../../provisioners/beyondtrust/bt-ready-windows11-vdi.ps1) | Windows 11 multi-session (AVD SKU), for PRA VDI desktops | `powershell` |

### What the Linux scripts prepare

- **PRA Shell Jump connectivity** — sshd hardened (key-only, no root password, sensible client-alive), passwordless sudo wired to the cloud-default user via a `/etc/sudoers.d/90-bt-ready` drop-in, host clock synced. The sshd drop-in is written as `00-bt-ready.conf` so it loads lex-first and wins against any later compliance drop-ins (sshd is first-occurrence-wins). On OpenSSH &lt; 8.2 — no `Include` for `sshd_config.d` — the directives are written into `/etc/ssh/sshd_config` directly instead.
- **A Password Safe / Entitle SSH bootstrap account** — `adminuser` by default (`BT_ADMIN_USER`). This is the account the Azure and GCP [Password Safe onboarding paths](password-safe.md#password-safe-vm-onboarding-managed-systems) expect to exist: their plugins write a key *to* it, they don't create it.
- **Optional EPM-L package install** — set `BT_EPML_URL` to a presigned URL for the `.deb` / `.rpm`.
- **Optional PRA SSH certificate login** — see below.
- **Conservative baseline hygiene** — security updates applied, persistent journald, opt-in unattended security updates (`BT_AUTOPATCH=1`), image cleaned for re-launch (host keys + machine-id + cloud-init state stripped).
- **Optional CIS / STIG remediation** (`BT_APPLY_CIS=1`) — installs OpenSCAP + SCAP Security Guide and applies a per-distro profile (default CIS L1 Server). Override via `BT_CIS_PROFILE=stig` or `cis_level2_server` (short names auto-expand to the SSG namespace). Report HTML lands at `/var/log/bt-ready/cis-report.html` on the built image. Debian-proper has no SSG CIS profile shipped, and Amazon Linux 2023's SSG coverage is incomplete — both warn and skip.

### PRA SSH certificate authority (optional)

Set `BT_PRA_CA_PUBKEY` to your PRA Vault SSH CA **public** key and the script pins it as
a `cert-authority` line in each PRA account's `authorized_keys`, so those accounts trust
certificates PRA issues instead of needing a static key. Unset ⇒ the feature is entirely
off.

`BT_PRA_USERS` is the comma-separated list of accounts to create for it — and these must
match the **usernames of the PRA Vault SSH-CA accounts** targeting this host, because a
certificate is scoped to its vault account. It is not a list of people.
`BT_PRA_PRINCIPAL` overrides the required principal; `BT_PRA_SUDO=1` grants those
accounts NOPASSWD sudo (default: none).

### What the scripts deliberately don't do

- **No Password Safe onboarding.** They *create* `adminuser`; registering it as a Managed
  Account (Smart Rule / rotation) is out-of-band — or done by the dashboard's own
  [VM onboarding](password-safe.md#password-safe-vm-onboarding-managed-systems).
- **No EPM-L activation.** Package install only. `pbactivate` runs post-deploy with a
  short-lived token from the EPM-L integration, because registration tokens expire 8h
  after issue and can't be baked into an image. See [EPM-L](epml.md).
- **No host firewall.** Cloud security groups / NSGs / GCP firewall rules are the source
  of truth; layering `ufw` / `firewalld` on top risks lockouts.

### Operator-overridable env vars

Set these as Packer build env on the build page. Full detail and a smoke-test recipe in
[`provisioners/beyondtrust/README.md`](../../provisioners/beyondtrust/README.md).

| Var | Notes |
|---|---|
| `BT_TARGET_USER` | Force the sudoers-target user; default autodetects the cloud-default (`ubuntu`/`debian`/`admin`, `ec2-user`/`rocky`/`centos`/`almalinux`/`cloud-user`) |
| `BT_ADMIN_USER` | The Password-Safe-managed bootstrap account (default `adminuser`) |
| `BT_SEED_ADMIN_KEY=1` | Seed `adminuser`'s `authorized_keys` with a throwaway key **so the AWS Systems Manager plugin has one to rotate** — the private half is discarded. Relevant to the [AWS SSM path](password-safe.md#aws--aws-systems-manager-custom-plugin-cloud-native-default), where the credential is minted on first change |
| `BT_ADMIN_NOPASSWD_ALL=1` | Full `NOPASSWD: ALL` sudo for `adminuser` instead of the scoped set — **required for Ansible Config-Management `become`**, which runs sudo's `/bin/sh` |
| `BT_PRA_CA_PUBKEY` | PRA Vault SSH CA public key (enables certificate login) |
| `BT_PRA_USERS` | Accounts to create for certificate login; names must match the PRA vault accounts |
| `BT_PRA_PRINCIPAL` | Require a specific principal instead of the default (principal == username) |
| `BT_PRA_SUDO=1` | NOPASSWD sudo for those accounts |
| `BT_EPML_URL` | Presigned URL to the EPM-L package; set to install it |
| `BT_AUTOPATCH=1` | Enable `unattended-upgrades` / `dnf-automatic` on the built image |
| `BT_SKIP_UPDATES=1` | Skip the upgrade step (faster iteration builds) |
| `BT_SKIP_CLEANUP=1` | Keep host keys / machine-id / logs while debugging |
| `BT_APPLY_CIS=1` | Run OpenSCAP remediation |
| `BT_CIS_PROFILE` | Override the profile id (default CIS L1 Server) |

**Cross-cloud constraint**: Azure's Packer builder invokes shell scripts as `sudo -E sh '{{ .Path }}'`, forcing `/bin/sh` regardless of shebang. Both `.sh` scripts are strict POSIX `sh` (verified with `dash -n`) so they behave identically on AWS, Azure, and GCP. They also self-elevate via `sudo -E`, since the AWS and GCP templates invoke the provisioner as the cloud-default user rather than root.

### Using them

**Linux** — upload the script to your active storage backend via `/storage`, then on the AWS / Azure / GCP build page pick it from the **Load from storage** dropdown above the Provisioner Script textarea.

**Windows** — `bt-ready-windows.ps1` runs on the Azure Windows build (`os_type=Windows`) before the template's windows-restart + Sysprep finisher. It bakes OpenSSH + RDP into the *output* image so VMs deployed from it are reachable by SSH like Linux ones, plus agentless RDP through the Gateway.

> **Azure cannot inject SSH public keys into Windows VMs** — that is a Linux-only deploy feature. So the key is authorized *in the image*. Use the public half of the keypair the dashboard holds in Key Vault (`azure_ssh_keypair_secret_name`) so the private half stays retrievable from the VMs tab exactly as it is for Linux. With no key set, password SSH still works using the admin password the deploy generates and vaults.

`bt-ready-windows11-vdi.ps1` is the VDI counterpart: multi-session RDP, NLA + firewall for agentless PRA Remote RDP, conservative VDI optimizations, and optional **first-boot** staging of the Remote Support jump client — never baked installed, so each clone registers a distinct client (cloning an installed one produces a confused rep-console entry, KB0017470).

---

## Where else BeyondTrust shows up

| Doc | What it covers |
|---|---|
| [Gateway hosts](gateways.md) | The managed-vs-requested Gateway lifecycle, placement, naming and node firewalls |
| [Databases](../databases.md) | Cloud-DB provisioning, PRA tunnels, and Password Safe database onboarding |
| [Kubernetes](../kubernetes.md) | Cluster provisioning, PRA k8s tunnels, and access identity |
| [Cloud VMs](../cloud-vms.md) | The full VM deploy story — provisioning, Shell Jump, onboarding, Entitle |
| [Config management](../config-management.md) | Ansible runs, including managed-account checkout as the login identity |
| [Secrets management](../secrets-management.md) | Where Password Safe sits among the dashboard's secret backends |
| [`provisioners/beyondtrust/README.md`](../../provisioners/beyondtrust/README.md) | The image-prep scripts in depth, with a smoke-test recipe |
