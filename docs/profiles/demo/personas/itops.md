# IT Engineer

Owns endpoints and the people using them: workstations, virtual desktops, the local
administrator account, and the support call that arrives when something breaks. Their estate
is the largest by count and the least uniform by configuration.

Their access problem has two halves that pull in opposite directions. Users need enough
privilege to do their jobs, and support needs enough reach to help them — but the traditional
answers to both are *make them a local admin* and *give support a standing route to every
machine*.

## Why they care

Local administrator rights are the most common finding in any endpoint audit, and the reason
they persist is practical: one application needs elevation, so the user gets admin, and now
every application has it. Meanwhile the support path is often a VPN plus a shared local
account with the same password on every machine imaged from the same source.

Both are solvable without making anyone's day slower, which is the only version of this story
an IT engineer will accept.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Virtual desktops on demand, so a support scenario has somewhere to happen. |
| **PRA** | Reaching a workstation that is not on the corporate network, and joining a user's session with it recorded. |
| **Password Safe** | Owns the local administrator credential and makes it *different on every machine*. |
| **Endpoint Privilege Management** | Elevates the command rather than the person. |

## Use cases

### Least privilege on a Linux endpoint

Let a user run the one command that needs elevation and nothing else — no sudoers entry, no
local admin group, and a record of what ran.

**Guide:** [EPM for Linux](../../../integrations/epml.md)

### Support a user on a virtual desktop, recorded

Stand up a virtual desktop and join the user's session to help them, with the session recorded
and no credential shared.

**Guide:** [Privileged Remote Access](../../../integrations/privileged-remote-access.md)

### Rotate a workstation local-admin password

The shared local administrator password every workstation has had since imaging, brought under
management and made unique per machine. This is the card that turns one compromised endpoint
into one compromised endpoint rather than all of them.

**Guide:** [Password Safe](../../../integrations/password-safe.md)

### Power a workstation on and off without RDP

Start, stop and reboot a machine through the agent — an operator who can manage the endpoint
without any standing access to it.

**Guide:** [Remote Agents](../../../remote-agents.md)

### Remote support with no VPN

Reach a workstation that is not on the corporate network at all, through an outbound-only
broker.

**Guide:** [Privileged Remote Access](../../../integrations/privileged-remote-access.md)

## What to enable

**Endpoint Privilege Management for Linux**, **Privileged Remote Access** and **Virtual
desktops**. Add **Password Safe** for the local-admin rotation card and **Remote agents** for
the power-control card.

The workstation and virtual-desktop features are demo-owned, so this focus is
thinner on a [POV instance](../../../pov-instance.md) — though the EPM and PRA halves are
profile-neutral and work on either.

## Talking to this buyer

Their instinct is that security controls generate tickets, and they are usually right. Every
card here should be presented in terms of tickets it *removes*: no password-reset call for the
shared admin account, no VPN troubleshooting before a support session, no elevation request
that needs a human to approve a whole account.
