# BeyondTrust-ready Packer provisioner scripts

Two POSIX `/bin/sh` scripts that prepare a freshly-built cloud image so it can be picked up by **BeyondTrust PRA Shell Jump** and live as a managed asset with a conservative baseline of hygiene. Designed to be loaded into a Packer build via the dashboard's `/storage` → "Load from storage" flow on the AWS / Azure / GCP build pages.

| Script | Targets |
|---|---|
| [`bt-ready-debian.sh`](bt-ready-debian.sh) | Debian, Ubuntu |
| [`bt-ready-rpm.sh`](bt-ready-rpm.sh) | RHEL, Rocky, CentOS Stream, AlmaLinux, Amazon Linux 2 / 2023 |

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

## What the scripts deliberately do *not* do

- **No new accounts.** Sudoers grants are wired to whatever user the source image already ships with.
- **No Password Safe onboarding.** Password Safe Managed Accounts must be registered out-of-band; the image just needs a sudo-capable SSH-keyed user, which step 5 provides.
- **No EPM-L agent install.** Registration tokens last 8 hours, so baking them at build time is hostile; a first-boot hook reading the token from cloud user-data is the right approach and is a separate effort.
- **No host firewall.** Cloud security groups / NSGs / GCP firewall rules are the source of truth; layering `ufw` / `firewalld` on top of them is redundant and risks lockouts.

## Operator-overridable env vars

Set these in the Packer build environment before launching the build. The dashboard does not currently wire them through the build form — they're consumed directly by Packer's shell provisioner.

| Var | Default | Effect |
|---|---|---|
| `BT_TARGET_USER` | autodetect | Force the sudoers-target username instead of the cloud-default detection. |
| `BT_AUTOPATCH` | `0` | When `1`, enable `unattended-upgrades` (Debian) / `dnf-automatic` (RPM) for ongoing security updates. |
| `BT_SKIP_UPDATES` | `0` | When `1`, skip the dist-upgrade in step 3. Useful for iteration. |
| `BT_SKIP_CLEANUP` | `0` | When `1`, skip the image-reuse cleanup in step 8. Useful when SSHing into the build VM to debug. |
| `BT_APPLY_CIS` | `0` | When `1`, install OpenSCAP + SCAP Security Guide and apply a CIS/STIG profile. See the "Optional CIS/STIG hardening" section above. |
| `BT_CIS_PROFILE` | per-distro CIS L1 Server | Override the SCAP profile id. Short names get the `xccdf_org.ssgproject.content_profile_` prefix prepended automatically — e.g. `BT_CIS_PROFILE=stig` works. Common values: `cis_server_l2`, `stig`, `stig_gui` (RPM); `cis_level2_server`, `stig` (Ubuntu). |

## Cross-cloud constraint

Azure's Packer template invokes scripts as `sudo -E sh '{{ .Path }}'`, which forces `/bin/sh` (dash on Debian) **regardless of shebang**. AWS and GCP honor the shebang. To work across all three, both scripts are strict POSIX `sh` — no `[[ ]]`, no arrays, no `<<<` here-strings. If you fork them, run `dash -n yourscript.sh` to keep them honest.

## Using the scripts in a Packer build

1. **Upload to your active storage backend.** Open `/storage` in the dashboard. Pick a backend (S3 / Azure Blob / GCS / local) and upload `bt-ready-debian.sh` and/or `bt-ready-rpm.sh`. They land under the `config-mgmt/` prefix by default; the storage service tags `.sh` files as type `script`.
2. **Start a Packer build.** Navigate to `/images/aws`, `/images/azure`, or `/images/gcp`. Fill in the usual source-image / instance-type / SSH-username fields.
3. **Load the script.** Click the **Load from storage** dropdown above the Provisioner Script textarea, pick the appropriate `bt-ready-*.sh`. The textarea populates; the blue subtitle confirms which backend the script came from.
4. **Submit the build.** Watch the job stream for `[bt-ready]` log lines — every step prints one.

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
