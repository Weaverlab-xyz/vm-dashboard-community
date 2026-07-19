# BeyondTrust-ready Packer provisioner scripts

Scripts that prepare a freshly-built cloud image so it can be picked up by **BeyondTrust PRA** and live as a managed asset with a conservative baseline of hygiene. Designed to be loaded into a Packer build via the dashboard's `/storage` → "Load from storage" flow on the AWS / Azure / GCP build pages.

| Script | Targets |
|---|---|
| [`bt-ready-debian.sh`](bt-ready-debian.sh) | Debian, Ubuntu |
| [`bt-ready-rpm.sh`](bt-ready-rpm.sh) | RHEL, Rocky, CentOS Stream, AlmaLinux, Amazon Linux 2 / 2023 |
| [`bt-ready-windows.ps1`](bt-ready-windows.ps1) | Windows Server 2022 (incl. Server Core) — Azure |
| [`bt-ready-windows11-vdi.ps1`](bt-ready-windows11-vdi.ps1) | Windows 11 multi-session (24H2 AVD) VDI desktops — Azure |

The two `*.sh` scripts are POSIX `/bin/sh`; the Windows ones are PowerShell (see their own sections — they behave differently enough to warrant separate notes).

## What the scripts do

Same shape in both files, divergent only where the package manager / unit names differ:

1. **OS-family gate** — abort if run on the wrong family (Debian script refuses RPM systems and vice-versa).
2. **Resolve the BT target user** — `$BT_TARGET_USER` env override, else autodetect the cloud-default user from a known list (`ubuntu`/`debian`/`admin` on Debian-family, `ec2-user`/`rocky`/`centos`/`almalinux`/`cloud-user` on RPM-family), else fall back to `$SUDO_USER`.
3. **System updates** — `apt-get dist-upgrade` (Debian) / `dnf --security upgrade` (RPM). Skippable with `BT_SKIP_UPDATES=1`.
4. **sshd hardening for PRA Shell Jump** — writes `/etc/ssh/sshd_config.d/99-bt-ready.conf` enforcing key-only auth, no root password login, sensible client-alive timers. Validated with `sshd -t` before exit.
5. **Sudoers** — writes `/etc/sudoers.d/90-bt-ready` granting the resolved user passwordless sudo. Validated with `visudo -c` before keeping.
6. **Time sync** — `systemd-timesyncd` (Debian) / `chronyd` (RPM). BeyondTrust auth fails on skewed clocks.
7. **Baseline hygiene** — persistent journald; opt-in unattended security updates via `BT_AUTOPATCH=1`.
8. **Image cleanup for re-launch** — strip SSH host keys, machine-id, cloud-init state, shell history, and log contents so each instance launched from the AMI/image starts fresh. Skippable with `BT_SKIP_CLEANUP=1` while debugging.

## Optional CIS / STIG hardening

Opt-in with `BT_APPLY_CIS=1`. The script installs OpenSCAP + SCAP Security Guide and runs `oscap xccdf eval --remediate` against a per-distro profile, then re-applies the BT sshd + sudoers drop-ins so PRA Shell Jump still works after compliance remediation. Report HTML lands at `/var/log/bt-ready/cis-report.html` on the built image.

| Distro family | Default profile | Override via `BT_CIS_PROFILE` |
|---|---|---|
| Ubuntu | `xccdf_org.ssgproject.content_profile_cis_level1_server` | `cis_level2_server`, `stig` (community-maintained) |
| RHEL / Rocky / AlmaLinux / CentOS Stream | `xccdf_org.ssgproject.content_profile_cis_server_l1` | `cis_server_l2`, `cis_workstation_l1`, `cis_workstation_l2`, `stig`, `stig_gui` |
| Amazon Linux 2 | `xccdf_org.ssgproject.content_profile_cis` | (only one profile available) |
| Debian (proper) | _no SSG CIS profile shipped_ — set `BT_CIS_PROFILE=xccdf_org.ssgproject.content_profile_anssi_np_nt28_minimal` to opt into ANSSI | |
| Amazon Linux 2023 | _SSG coverage incomplete_ — script warns and skips | |

Caveats worth knowing before enabling:

- **CIS L2 and STIG are aggressive.** They install `auditd`, AIDE (with daily integrity scans), set sysctl knobs that change networking behavior, and disable kernel modules (`usb-storage`, `dccp`, `sctp`, `cramfs`). Pre-prod test before rolling to fleet.
- **Build time grows substantially** (10-30 min added depending on profile + distro).
- **First-boot reboot may be required** for certain remediations (e.g. unloading kernel modules). Cloud images that boot from this AMI/image will pick them up cleanly.
- **Precedence**: CIS writes to `/etc/ssh/sshd_config.d/00-complianceascode-hardening.conf`. Our drop-in `/etc/ssh/sshd_config.d/00-bt-ready.conf` is alphabetically earlier so its directives win (sshd uses first-occurrence-wins semantics). If you see auth failures from PRA, inspect both files and confirm the BT one is being loaded first.

## adminuser + EPM-L

Beyond the PRA Shell Jump prereqs, the scripts also prepare the image for
**Password Safe management** and **EPM-L**:

- **`adminuser`** (override with `BT_ADMIN_USER`) — a dedicated account created to be
  **Password-Safe-managed** (onboarded + key/password rotated out-of-band).
- **Scoped sudo (default).** `adminuser` gets NOPASSWD sudo limited to exactly the commands an
  SSH "ephemeral accounts" workflow needs — `cat chmod chown mkdir mv rm sed tee
  useradd userdel` — written to `/etc/sudoers.d/91-bt-adminuser` and validated with
  `visudo -c`. Not blanket `ALL`.
- **Full passwordless sudo for Ansible (opt-in, `BT_ADMIN_NOPASSWD_ALL=1`).** Use this when
  running the dashboard's config-mgmt (Ansible) as `adminuser`. Ansible `become` runs
  `sudo /bin/sh -c '… python3 AnsiballZ_*.py'`, so sudo's target is the shell, not the
  package tool — the scoped whitelist above can't satisfy it and `become` tasks fail with
  `Missing sudo password`. `BT_ADMIN_NOPASSWD_ALL=1` writes `ALL=(ALL) NOPASSWD: ALL` to the
  same sudoers file instead. Off by default (keeps the scoped set); enable per image.
- **Seed key for the AWS Systems Manager Custom Plugin (opt-in, `BT_SEED_ADMIN_KEY=1`).**
  That plugin **rotates an existing key in place** — it does not bootstrap
  `~adminuser/.ssh/authorized_keys`, so with nothing seeded it has nothing to rotate and
  the account is never SSH-reachable. With `BT_SEED_ADMIN_KEY=1` the provisioner generates
  a throwaway keypair, installs the **public** half in `adminuser`'s `authorized_keys`, and
  **discards the private half** (it never leaves the build — no standing access), giving the
  plugin a placeholder to replace on its first Change Password. Off by default so Entitle /
  cloud-default-user images are unaffected. (The Windows script's equivalent is
  `BT_AUTHORIZED_KEY`.)

> **Entitle SSH integration — no key baked here.** The dashboard's Entitle
> SSH-ephemeral-accounts registration connects as the **cloud-default user** with the
> VM's **own launch keypair** (the key cloud-init injects at boot) — so the provisioner
> no longer installs a separate Entitle public key. Point `entitle_ssh_sudo_user` at the
> cloud-default user; see [`docs/integrations/entitle.md`](../../docs/integrations/entitle.md).

- **EPM-L package install (opt-in).** When `BT_EPML_URL` is set — a presigned URL
  to the OS-appropriate package, obtained from the dashboard's EPM-L integration
  (which syncs the latest RPM/DEB to storage) — the script downloads + installs the
  package. **Install only:** EPM-L *activation* (`pbactivate -t <token>`) happens
  post-deploy using a short-lived installation token from the dashboard's **EPM-L
  integration** (`/api/epml/token`) — tokens must not be baked into an image.

## Windows (`bt-ready-windows.ps1`)

The Windows script prepares a **Windows Server 2022** image (including **Server Core**) for PRA access. It's the Windows analogue of the `*.sh` scripts but differs in important ways:

- **PowerShell, not sh**, and **Azure-only** for now (the dashboard's Windows Packer builder is Azure; AWS/GCP Windows builds are a later follow-up). Select a **Windows Server 2022** or **2022 Core** preset on the Build Image tab (`os_type=Windows`); the build runs the script as Packer's `powershell` provisioner before the `windows-restart` + Sysprep `/generalize` finisher.
- **Build connects over WinRM; the image is reached over SSH.** Marketplace Windows base images have no SSH at first boot, so Packer uses WinRM during the build. The script installs **OpenSSH Server** (in-box Feature-on-Demand, with a Win32-OpenSSH MSI fallback) into the *output* image, so VMs deployed from it are reachable with `ssh` — "not all that different than how Linux cloud VMs are accessed today" — plus **agentless RDP** through the PRA Jumpoint (RDP + NLA + firewall are enabled).
- **The SSH key is baked into the image, by necessity.** Azure cannot inject SSH public keys into Windows VMs at deploy time (that's Linux-only — `WindowsConfiguration` has no SSH field). So set `$AuthorizedKey` at the top of the script to the **public** half of the keypair the dashboard keeps in Key Vault (`azure_ssh_keypair_secret_name`); the matching private key is then retrievable from the VMs tab / `ssh-key` endpoint exactly like Linux. Leave it blank and password-auth SSH still works using the admin password the deploy generates and vaults (**Azure → VMs → Password**). The `$env:BT_*` reads in the script are for CLI/Packer use — the dashboard's PowerShell provisioner does not forward `environment_vars` yet, so when driving it through the GUI, edit the inline values.
- **Access model after deploy:** retrieve the vaulted admin password (**Azure → VMs → Password**) → `ssh azureuser@<ip>` (password), or use your Key Vault private key if you baked the public key → or RDP via the Jumpoint. The OpenSSH default shell is set to PowerShell so `ssh` lands in a familiar prompt.
- **No image-reuse cleanup step.** On Windows, Sysprep `/generalize` (the build template's finisher) owns generalization — the script deliberately does *not* strip host keys / SIDs / logs the way the `*.sh` cleanup step does.
- **Optional toggles** (set as `$env:BT_*` for CLI builds, or edit inline): `BT_AUTHORIZED_KEY`, `BT_ADMIN_USER` (default `azureuser`), `BT_SSH_KEY_ONLY=1` (harden sshd to key-only), `BT_ENABLE_RDP=0` (skip RDP).

The Linux-centric sections below (`adminuser` / EPM-L, CIS via OpenSCAP, the cross-cloud `/bin/sh` constraint, the self-elevation privilege model) **do not apply** to the Windows script.

## Windows 11 multi-session VDI (`bt-ready-windows11-vdi.ps1`)

The VDI analogue of `bt-ready-windows.ps1`, for the **Windows 11 multi-session (24H2 AVD)** build preset (which publishes a Trusted Launch Compute Gallery image). A desktop, not a server — so it's **RDP-first**, multi-session-capable, lightly VDI-optimized, and stages the RS jump client for **first-boot** install:

- **Multi-session + RDP.** Sets `fSingleSessionPerUser=0` (concurrent sessions), enables RDP + NLA + the Remote Desktop firewall group + time-zone redirection. Agentless **Remote RDP** jump items reach 3389 from the Jumpoint subnet; pool VMs are private + brokered. The multi-session SKU provides multi-*user* concurrency without an RDSH role — but the dashboard does **not** install the AVD agent or register an AVD host pool, so VMs are used **1-per-seat over PRA-RDP** and the multi-session capability is latent.
- **Conservative VDI optimizations.** Disables hibernation, telemetry, Windows consumer features, and Store auto-update — a safe subset. For heavier tuning, run Microsoft's [Virtual Desktop Optimization Tool (VDOT)](https://github.com/The-Virtual-Desktop-Team/Virtual-Desktop-Optimization-Tool) as your provisioner instead.
- **RS jump client at FIRST BOOT, never baked installed.** Set `$JumpClientUrl` (or `BT_JUMP_CLIENT_URL`) to the RS **mass-deployment installer** (`.msi`) URL. The script stages it into the image and writes `C:\Windows\Setup\Scripts\SetupComplete.cmd`, which runs `msiexec /i … /quiet` **once per clone** after Sysprep OOBE (as SYSTEM, before logon) — so each VDI VM registers a **distinct** jump client. Baking an *installed* client into a golden image makes every clone phone home with the same identity → a "confused entry in the rep console" (KB0017470). Optional `BT_JUMP_GROUP` / `BT_JUMP_TAG` map to `jc_jump_group` / `jc_tag`. **Caveats:** mass-deploy installers **expire** and are **invalidated by appliance upgrades** — rebuild the image after appliance updates; and the deployed (private) VM needs **outbound 443 to the appliance** to register at first boot.
- **Optional OpenSSH.** Off by default (RDP is primary). `BT_INSTALL_OPENSSH=1` installs OpenSSH Server and authorizes `BT_AUTHORIZED_KEY` for admin SSH, the same as `bt-ready-windows.ps1`.
- **No cleanup step** — Sysprep `/generalize` owns generalization.

## What the scripts deliberately do *not* do

- **No Password Safe onboarding.** The scripts *create* `adminuser`; registering it
  as a PS Managed Account (Smart Rule / rotation) is an out-of-band step.
- **No EPM-L activation.** Package install only; `pbactivate` runs post-deploy
  with a short-lived token from the EPM-L integration (token freshness — see above).
- **No host firewall.** Cloud security groups / NSGs / GCP firewall rules are the source of truth; layering `ufw` / `firewalld` on top of them is redundant and risks lockouts.

## Operator-overridable env vars

These are consumed directly by Packer's shell provisioner. The dashboard build
form surfaces the common ones for you — a **BeyondTrust provisioner options**
panel (Admin user → `BT_ADMIN_USER`, Install EPM-L → `BT_EPML_URL`) plus a generic
**Environment variables** table for the rest (`BT_APPLY_CIS`, `BT_SKIP_UPDATES`, …); see
[Image Management → Passing environment variables to the provisioner](../../docs/image-management.md#passing-environment-variables-to-the-provisioner).
You can still set any of them directly in the build environment when scripting
Packer outside the dashboard.

| Var | Default | Effect |
|---|---|---|
| `BT_TARGET_USER` | autodetect | Force the sudoers-target username instead of the cloud-default detection. |
| `BT_ADMIN_USER` | `adminuser` | Name of the Password-Safe-managed bootstrap account the script creates. |
| `BT_SEED_ADMIN_KEY` | `0` | When `1`, seed `adminuser`'s `authorized_keys` with a throwaway public key (private half generated + discarded at build) so the AWS Systems Manager Custom Plugin has an existing key to rotate. Required for that plugin (it rotates in place, doesn't bootstrap). Leave `0` for Entitle / cloud-default-user images. |
| `BT_ADMIN_NOPASSWD_ALL` | `0` | When `1`, give `adminuser` full passwordless sudo (`ALL=(ALL) NOPASSWD: ALL`) instead of the scoped Entitle command set. Needed for **Ansible config-mgmt** as this account: Ansible `become` runs `sudo /bin/sh -c '… python3 …'`, so sudo's target is the shell — a per-package whitelist can't cover it. Leave `0` to keep the least-privilege scoped set. |
| `BT_EPML_URL` | (unset) | Presigned URL to the OS-appropriate EPM-L package (`.deb` for Debian, `.rpm` for RPM). Set = download + install at build; unset = skip. **Activation runs post-deploy via the EPM-L integration**, not at build. The dashboard's Install EPM-L dropdown fills this in for you. |
| `BT_AUTOPATCH` | `0` | When `1`, enable `unattended-upgrades` (Debian) / `dnf-automatic` (RPM) for ongoing security updates. |
| `BT_SKIP_UPDATES` | `0` | When `1`, skip the dist-upgrade in step 3. Useful for iteration. |
| `BT_SKIP_CLEANUP` | `0` | When `1`, skip the image-reuse cleanup in step 8. Useful when SSHing into the build VM to debug. |
| `BT_APPLY_CIS` | `0` | When `1`, install OpenSCAP + SCAP Security Guide and apply a CIS/STIG profile. See the "Optional CIS/STIG hardening" section above. |
| `BT_CIS_PROFILE` | per-distro CIS L1 Server | Override the SCAP profile id. Short names get the `xccdf_org.ssgproject.content_profile_` prefix prepended automatically — e.g. `BT_CIS_PROFILE=stig` works. Common values: `cis_server_l2`, `stig`, `stig_gui` (RPM); `cis_level2_server`, `stig` (Ubuntu). |
| `BT_PRA_CA_PUBKEY` | (unset) | PRA Vault's SSH CA **public** key. Set = enable certificate login; unset = feature entirely off. See below. |
| `BT_PRA_USERS` | (unset) | Comma-separated accounts to create for certificate login, e.g. `Pathfinder,svc-app`. |
| `BT_PRA_PRINCIPAL` | (unset) | Require this principal instead of the default (principal must equal the username). |
| `BT_PRA_SUDO` | `0` | When `1`, give those accounts `NOPASSWD: ALL` sudo. Default is **no sudo**. |

## PRA SSH certificate authority (`BT_PRA_*`)

PRA's Vault can act as an SSH CA: it issues a short-lived certificate scoped to a
specific **vaulted account**, and the host trusts it by pinning the CA's *public*
key. That replaces a shared, long-lived `authorized_keys` entry — revocation and
rotation happen in PRA rather than by touching every VM.

**The account names are not yours to choose.** A certificate is bound to its vault
account, so each name in `BT_PRA_USERS` must match the **username of the PRA Vault
SSH-CA account** that will target these hosts — including capitalisation (vault
accounts are often capitalised, e.g. `Pathfinder`). A mismatch fails at login as
"no such user", not as an auth error, which is a confusing way to find out.

The CA key is pinned **per account**, as a `cert-authority` line in that account's
`~/.ssh/authorized_keys`:

```
cert-authority ssh-rsa AAAAB3Nza… pf50b242.beyondtrustcloud.com bt-ready-pra-ca
```

Trust is therefore scoped to exactly those accounts, rather than host-wide via
`TrustedUserCAKeys`. Paste the value straight from PRA — the script accepts it
with or without the leading `cert-authority `, and preserves the tenant hostname
comment. Supply it as a **secret reference** on the build form's Environment
variables table (tick *secret*), so it reaches Packer as a sensitive variable and
never lands in the template or the archived copy.

```
BT_PRA_CA_PUBKEY = aws_sm://pra/vault-ssh-ca      (secret ✓)
BT_PRA_USERS     = Pathfinder
```

### Principals — read this before relying on it

By default OpenSSH requires the certificate's principal to equal the login
username, which matches the vault-account model and needs no extra config.

**But a certificate carrying *no* principals is valid for _any_ user.** That is
OpenSSH behaviour, not a bug here, and it is verified in our tests: in default mode
a principal-less cert signed by the pinned CA logs in successfully. If your tenant
issues such certificates, set `BT_PRA_PRINCIPAL` — it adds `principals="…"` to the
`cert-authority` line, which constrains the account to certs bearing that principal
and rejects principal-less ones outright.

Check which case you are in with one command against a real PRA-issued cert:

```
ssh-keygen -L -f <cert>     # read the Principals: field
```

The same output line also shows the signing algorithm (`Signing CA: RSA … (using
rsa-sha2-512)`). Modern OpenSSH excludes legacy SHA-1 `ssh-rsa` from its default
`CASignatureAlgorithms`, so a CA signing with SHA-1 would be rejected on current
distros — worth a glance, though current PRA versions sign with rsa-sha2-*.

### Notes

- The image-cleanup step strips `authorized_keys` from build/default users, but
  **exempts PRA accounts** — the `cert-authority` line is a reference to a public
  CA key, not a credential, and shipping it in the image is the point.
- Re-running replaces the pinned line rather than appending, so rotating the CA is
  just another build.
- The CA key is a **root of trust**: any certificate it signs can log into these
  accounts. Anyone who can set `BT_PRA_CA_PUBKEY` on a build can grant themselves
  access to every image built from it.
- This is bake-time only. Certificates reach **newly built images**; existing hosts
  need the equivalent applied post-deploy (e.g. via Ansible).

## Cross-cloud constraint

Azure's Packer template invokes scripts as `sudo -E sh '{{ .Path }}'`, which forces `/bin/sh` (dash on Debian) **regardless of shebang**. AWS and GCP honor the shebang. To work across all three, both scripts are strict POSIX `sh` — no `[[ ]]`, no arrays, no `<<<` here-strings. If you fork them, run `dash -n yourscript.sh` to keep them honest.

## Privilege model

The scripts **self-elevate to root** via `sudo -E "$0" "$@"` at the very top, before `set -eu`. Azure's Packer template already wraps invocations with `sudo -E sh`; AWS and GCP templates run the shell provisioner as the cloud-default SSH user (`ubuntu` / `ec2-user`). The self-elevation makes the scripts indifferent to which template invoked them. `-E` preserves your `BT_*` env-var overrides through the elevation.

Prerequisite: the cloud-default user has passwordless sudo (true on stock Ubuntu, Debian, Amazon Linux, RHEL, Rocky, Alma, CentOS Stream AMIs). If you've forked an image that requires a sudo password, set `BT_TARGET_USER=root` and invoke Packer with `ssh_username=root` so the script is already root when it starts and the self-elevation no-ops.

## Using the scripts in a Packer build

1. **Upload to your active storage backend.** Open `/storage` in the dashboard. Pick a backend (S3 / Azure Blob / GCS / local) and upload `bt-ready-debian.sh` and/or `bt-ready-rpm.sh`. They land under the `config-mgmt/` prefix by default; the storage service tags `.sh` files as type `script`.
2. **Start a Packer build.** Navigate to `/images/aws`, `/images/azure`, or `/images/gcp`. Fill in the usual source-image / instance-type / SSH-username fields.
3. **Load the script.** Click the **Load from storage** dropdown above the Provisioner Script textarea, pick the appropriate `bt-ready-*.sh`. The textarea populates; the blue subtitle confirms which backend the script came from.
4. **Set the BeyondTrust options.** With a script loaded, the **BeyondTrust provisioner options** panel appears: set the **Admin user** (`adminuser`) and choose **Install EPM-L** (deb/rpm — the dashboard resolves the presigned `BT_EPML_URL` for you at launch). Add any other knobs (`BT_APPLY_CIS=1`, `BT_SKIP_UPDATES=1`, …) as rows in the **Environment variables** table below it; flip **secret ref** on a row to pull its value from your secrets backend instead of inlining it.
5. **Submit the build.** Watch the job stream for `[bt-ready]` log lines — every step prints one.

## AWS smoke-test recipe (first ship)

The first end-to-end run. Repeat the equivalent on Azure and GCP afterward — the script body is unchanged.

1. Upload `bt-ready-debian.sh` to the active storage backend.
2. `/images/aws` build form:
   - **Source AMI**: a current Ubuntu 22.04 LTS AMI for your region.
   - **Instance type**: `t3.micro`.
   - **SSH username**: `ubuntu`.
   - **Image name**: `bt-ready-ubuntu-22-04-test`.
   - **Provisioner script**: Load from storage → `bt-ready-debian.sh`.
3. Submit. Wait for the AMI to appear in the dashboard's Images tile.
4. Launch a `t3.micro` from the new AMI in the same region with your usual SSH key. SSH in as `ubuntu`.
5. Validate:
   ```sh
   sudo -n true                              # passwordless sudo
   sudo cat /etc/sudoers.d/90-bt-ready       # the NOPASSWD line for ubuntu
   sudo cat /etc/ssh/sshd_config.d/99-bt-ready.conf
   systemctl is-active ssh                   # active
   timedatectl                               # System clock synchronized: yes
   sudo journalctl --list-boots | head       # persistent journald
   ```
6. (Optional, needs PRA console access) Register the instance under your Jumpoint as a Shell Jump host. Connect through PRA. Confirm sudo escalation works through the PRA session.

## Iteration loop

While developing changes, set `BT_SKIP_UPDATES=1` in the Packer env to skip the dist-upgrade — that shaves the slowest step. Re-upload the modified script to storage; the dropdown picks up the new version on the next page load. The Packer template doesn't cache stored scripts between builds.
