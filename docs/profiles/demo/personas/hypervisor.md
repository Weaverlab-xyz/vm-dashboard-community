# Hypervisor Admin

Owns the virtualisation layer — vSphere, Proxmox, Hyper-V, Nutanix, XCP-ng — and with it the
most concentrated privilege in the datacentre. A hypervisor root account can read the disk of
every guest on the host, which makes it more powerful than any single server's administrator
and considerably less likely to be under management.

Their access problem is that **the console is the tool**. Unlike a server, where you can
argue for SSH-with-a-broker, the day-to-day work happens in a web UI that expects a username
and a password, and the account is usually one shared credential known to the whole team.

## Why they care

Three facts, usually all true at once: the root password has not changed since the cluster
was built, it is in a password manager several people can open, and there is no record of who
used it or what they did. None of that is negligence — there was no mechanism that made the
alternative practical.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Guests on the hypervisor, deployed and torn down from one place. |
| **PRA** | The centre of gravity: Web Jump into the console with the credential injected and the session recorded, and Shell Jump to guests on an isolated management network. |
| **Password Safe** | Owns the root credential and rotates it, so "shared password" stops being true. |
| **Entitle** | Time-boxed elevation for the work that genuinely needs root. |

## Use cases

### Web Jump into the hypervisor console

Open the vSphere or Proxmox web console with the root credential injected and the whole
session recorded. The administrator does the work and never learns the password — which is
the single most convincing thing you can show this buyer.

**Guide:** [Privileged Remote Access](../../../integrations/privileged-remote-access.md) ·
[vSphere](../../../integrations/vsphere.md) · [Proxmox](../../../integrations/proxmox.md)

### Onboard and rotate the hypervisor root account

Bring an ESXi or Proxmox root credential under management and rotate it. Worth doing live,
because the objection is always "what breaks when it changes" and the answer is visible.

**Guide:** [Password Safe](../../../integrations/password-safe.md)

### Reach a guest VM with no inbound firewall rule

Shell Jump to a VM on an isolated management network through a Gateway that only ever makes
outbound connections.

**Guide:** [Privileged Remote Access](../../../integrations/privileged-remote-access.md)

### Stand up a Gateway and watch it register

Build the gateway, see it appear, and use it. This is the piece that makes everything above
possible, and showing it built rather than pre-existing answers "how much work is this to
deploy".

**Guide:** [Gateways](../../../integrations/gateways.md)

### Discover an on-prem estate from the outside

An agent inside the datacentre polls outward and reports what is there — no inbound access to
the management network, and no credentials stored anywhere the dashboard can reach.

**Guide:** [Remote Agents](../../../remote-agents.md)

## What to enable

**Privileged Remote Access**, plus whichever hypervisors are real for the customer
([vSphere](../../../integrations/vsphere.md), [Proxmox](../../../integrations/proxmox.md),
[Hyper-V](../../../integrations/hyperv.md), [Nutanix](../../../integrations/nutanix.md),
[XCP-ng](../../../integrations/xcpng.md)). Add **Password Safe** for rotation and **Remote agents**
for the discovery card.

**This focus needs a demo instance.** Every hypervisor integration is demo-owned, because
their deploys and Web Jumps resolve the global BeyondTrust tenant — so on a
[POV instance](../../../pov-instance.md) most of these cards report as unavailable. That is a
deliberate tenancy decision rather than a gap, and the POV plan floats reusing a
Proxmox/vSphere connection as an on-premises lab platform later; it needs the tenancy question
answered first.

## Talking to this buyer

They are protective of the console, and rightly — it is where they work. Do not present this
as taking it away. Present it as the console *without* the shared password: same screens, same
speed, and a recording that protects them as much as it audits them.
