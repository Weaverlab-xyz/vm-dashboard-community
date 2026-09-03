# Playbook structure

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are writing or adapting a playbook for the dashboard to run.

Part of [Remote Worker](../ansible.md). What the runner expects of a playbook, and the samples to start from.

## Ansible playbook structure

### On-premises hypervisor playbook

Target the `proxmox`, `vsphere`, `hyperv`, `nutanix`, or `xcpng` group
(whichever is configured). Or use `on_premises` to hit all of them.

```yaml
# harden-proxmox.yml
- hosts: proxmox
  become: yes
  tasks:
    - name: Ensure auditd is running
      service:
        name: auditd
        state: started
        enabled: true
```

```yaml
# restart-hyperv-service.yml
- hosts: hyperv
  tasks:
    - name: Restart the dashboard service
      win_service:
        name: DashboardSvc
        state: restarted
```

### Cloud VM playbook (single-host, ad-hoc)

For cloud targets the dashboard passes the IP as `-i <host>,` to Ansible:

```yaml
# hardening.yml
- hosts: all
  become: yes
  tasks:
    - name: Ensure sshd is running
      service:
        name: sshd
        state: started
        enabled: true
```

### Provisioning asset examples

**Script (install-agent.sh)** — upload a `.sh` file; the dashboard wraps it
automatically:

```bash
#!/bin/bash
set -euo pipefail
curl -fsSL https://packages.example.com/agent.sh | bash
systemctl enable --now example-agent
```

**RPM package (my-agent-1.0.rpm)** — upload the `.rpm` directly. The dashboard
generates:

```yaml
- hosts: all
  become: yes
  tasks:
    - name: Copy my-agent-1.0.rpm to remote
      ansible.builtin.copy:
        src: /ansible/assets/my-agent-1.0.rpm
        dest: /tmp/my-agent-1.0.rpm
    - name: Install my-agent-1.0.rpm
      ansible.builtin.dnf:
        name: /tmp/my-agent-1.0.rpm
        state: present
        disable_gpg_check: true
```

### Sample playbooks

Ready-to-adapt starters for Linux and Windows cloud VMs live in
[`examples/playbooks/`](../../../examples/playbooks) — patching, SSH hardening,
admin-user creation, Docker, node_exporter, nginx (Linux); Windows updates,
firewall, Chocolatey, local admin, and IIS (Windows). See
[examples/playbooks/README.md](../../../examples/playbooks/README.md) for how to run
each. There are also two **cluster-building** sets for on-prem hosts — **Docker Swarm**
([`examples/playbooks/swarm/`](../../../examples/playbooks/swarm)): init, join, open
ports, stack deploy, status, leave; and **k3s**
([`examples/playbooks/k3s/`](../../../examples/playbooks/k3s)): server-init, join, open
ports, kubeconfig, status, uninstall. Both work the same way: because a run targets one
host at a time, the cluster is built node-by-node with the join token relayed between
runs, so they need the **local runner** (see that README for the walkthrough and the
token-visibility caveat — either token can be routed through Password Safe instead of
job output).

The k3s set closes the loop with the section below: `k3s-kubeconfig.yml` rewrites k3s's
loopback API address to the node's real one and prints a registration-ready payload, so
the cluster you just built can be registered (`cloud = local`) and then become a
Config-Management target itself.

**Linux** samples run via the cloud or local runner;
**Windows** (WinRM)
samples run via the **local runner**, which forwards the `ansible_password` extra
var the WinRM connection needs (the cloud runner is SSH-only and doesn't forward
extra vars).

---
