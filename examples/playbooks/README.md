# Sample Ansible playbooks (cloud VM starters)

Ready-to-adapt playbooks for configuring **Linux** and **Windows** cloud VMs via
the dashboard's **Config Management** feature
(see [docs/integrations/ansible.md](../../docs/integrations/ansible.md)). They are
the Ansible counterpart to [`examples/compose/`](../compose/) — upload one, edit
the placeholders, and run.

## How to run

1. **Upload** the `.yml` to a storage backend — Storage page, or `POST /api/storage/upload`.
2. **Run** — Config Management (`/config-mgmt`) → pick the asset → choose a target
   (a cloud VM's IP + its cloud, or an on-prem group) → optionally set extra vars → Run.
3. **Watch** the job on the Jobs page; output (and CloudWatch/Cloud Logging logs for
   cloud runners) is linked from there.

## Linux (`linux/`)

`- hosts: all`, `become: yes`, generic modules so they span Debian/Ubuntu and
RHEL/Rocky/Alma. These run cleanly via the **cloud runner** (it SSHes to the VM IP
as the per-cloud user with the key the dashboard injected at deploy) or the local runner.

| File | Purpose |
|---|---|
| `patch-and-reboot.yml` | Update all packages; reboot only if required |
| `ssh-hardening.yml` | Disable root login + password auth, tighten sshd (validated reload) |
| `create-admin-user.yml` | Create a sudo user + authorized key (params via extra_vars) |
| `install-docker.yml` | Install Docker Engine from the official repos, enable the service |
| `node-exporter.yml` | Install Prometheus node_exporter as a systemd unit (:9100) |
| `nginx-web.yml` | Install + enable nginx, serve a sample page (:80) |

## Windows (`windows/`)

WinRM playbooks using `ansible.windows` / `community.windows`. The static
connection settings live in each play's `vars:`; you supply the credentials as
**extra vars** at run time:

```
ansible_user: azureuser
ansible_password: <the Windows admin password stored at deploy time>
```

| File | Purpose |
|---|---|
| `win-updates.yml` | Install security/critical updates, reboot |
| `win-firewall-baseline.yml` | Ensure firewall profiles enabled; sample allow rule |
| `win-install-software.yml` | Install packages via Chocolatey (git, 7zip, …) |
| `win-create-local-admin.yml` | Create a local user + add to Administrators |
| `win-feature-iis.yml` | Install the IIS web server role |

### Run Windows samples via the **local runner**

Two constraints make the local runner the path for Windows today:

- **The cloud runner is SSH-only and does not forward `extra_vars`.** Only the
  local runner forwards extra vars, which is how the WinRM `ansible_password`
  reaches the play. (Linux samples don't need this — they authenticate with the
  injected SSH key.)
- **Windows cloud VMs are Azure-only and password-based.** Ensure **WinRM is
  reachable** (ports 5985/5986; open it in the NSG) and pass the admin password —
  stored in your secrets backend at deploy — as `ansible_password`.

So: set `ansible_runner = local`, target the Windows VM's IP, and pass
`ansible_user` / `ansible_password` as extra vars. (On-prem Hyper-V Windows hosts
work the same way and are already wired in the dashboard inventory.)

## Notes

- These are starting points — review and adapt before running against real hosts.
  `ssh-hardening.yml` disables SSH password auth, so confirm key access first.
- Playbooks use fully-qualified module names; the runner image
  (`willhallonline/ansible`) ships the `ansible.builtin`, `ansible.posix`,
  `ansible.windows`, and `community.windows` collections.
- `tests/test_playbook_samples.py` validates every file here is a well-formed play
  list, so a malformed sample can't ship.
