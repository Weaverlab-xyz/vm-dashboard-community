# Network / firewall admin

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to whoever holds the enable password and signs off the change window.

Owns the routers, the firewalls and the switches — the boxes every other demo in this
catalog quietly assumes are already reachable. They are also the last estate in most
organisations where the credential is still a shared secret: memorised by four people,
written down by a fifth, and rotated the week after somebody leaves, if at all.

Their access problem is not that access is hard to get. It is that **the moment access
matters most is the moment the controls come off.** Traffic from a subnet has to be
blocked *now*; the person who can do it is the person who remembers the password; and
the record of what happened is a config diff with nobody's name on it.

## Why this story lands

Network gear is the oldest privileged-access problem in the building and the least
likely to have been solved, because the usual answers do not fit it. You cannot install
an agent on a firewall. You cannot put a jump box in front of it without becoming the
next thing that needs managing. And the device's own logs record *what* changed, never
*who* was at the keyboard.

What a demo has to show, concretely:

- a real device, with a real `configure` mode, taking a **real rule change**;
- the person making it **never seeing the credential**;
- the device carrying **no inbound rule and no VPN**, and still being reachable;
- the whole thing **recorded**, so "who changed this on the third?" has an answer.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Stands up a [network cell](../net-demo-cell.md) (**preview**) — a VyOS router/firewall from a baked image, with no external IP, in the sandbox's private subnet. It is a real network OS: `configure`, `set firewall`, `commit`, `save`. |
| **PRA** | The only way in, and the whole of the access layer. A Shell Jump over SSH, recorded. No Web Jump and no protocol tunnel — a firewall needs a shell and nothing else, which is exactly why this cell is so much smaller than the OT one. |
| **Password Safe** | Vaults the device's administrator credential and injects it, so the admin never sees it. Read [what this does and does not do](../net-demo-cell.md#what-password-safe-does-here) before you promise rotation. |
| **Entitle** | **Not part of this cell.** Its SSH integration creates ephemeral accounts with `useradd`, which is not how VyOS manages users. The expiring-window beat is told with Password Safe's checkout instead. |

## Use cases

### The emergency rule change, recorded end to end

The flagship. Block a subnet on a live firewall the way it would really happen —
`configure`, `set firewall … action drop`, `commit`, `save` — then stop and play the
recording back. Every keystroke, timestamped, with a name against it.

**Guide:** [Network Demo Cell](../net-demo-cell.md)

### Nobody is handed the firewall password

The same session, told from the credential's side. The admin requests access, the
credential is injected rather than displayed, and nothing has to be rotated afterwards
because nobody learned anything to forget.

**Guide:** [Network Demo Cell](../net-demo-cell.md)

### Reach the device with no inbound rule and no VPN

The cell has no public address and no port open to anything. The Gateway dials *out*,
so the path in exists without an attack surface — the answer to "so we open 22 to your
cloud?", which is the question that usually ends these conversations.

**Guide:** [Gateways](../../../integrations/gateways.md)

### The credential dies when the window closes

Check the credential out for the change window and let it lapse. Password Safe rotates
it shut, so whatever the contractor wrote down stops working — without anyone having to
remember to revoke it, which is the step that never happens.

**Guide:** [Network Demo Cell](../net-demo-cell.md)

### What changed, and the session it changed in

Close the loop an auditor actually asks about: from a rule that exists today, back to
the change that made it, the session it happened in, and the person who was there.

**Guide:** [Audit log](../../../audit-log.md)

## What to enable

| | Why |
|---|---|
| **`netcell_enabled`** | Required, and a **preview** toggle — off by default. Settings → Preview features → *Network Demo Cell*. The bake has not been run against a live VyOS image; read [the page](../net-demo-cell.md) before you demo it. |
| **`pra_enabled`** | Required. The cell is reached only through a Shell Jump; with PRA off it is a device nobody can log into. |
| **`password_safe_enabled`** | For the credential half of the story. Without it the cell still deploys and is still reachable, but "nobody is handed the password" has nothing behind it. |
| **A VyOS Password Safe platform** | For the rotation half. A stock Linux platform is reverted by the next commit of `system login`; the change command has to run in vbash. You build it — see [the network cell page](../net-demo-cell.md#what-password-safe-does-here). |
| A **Gateway** | On the cell's subnet, before you deploy. The deploy refuses without one rather than building a device it cannot reach. |
| The **`vyos-cell` image** | Baked first, from a VyOS image you supply — see [provisioners/net/README.md](https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/provisioners/net/README.md). |

Three of the five cards above target a cloud console, which a POV instance does not
serve — see [the POV profile](../../pov/README.md) for why the two install profiles
differ there. The other two run on either.

## Talking to this buyer

Lead with the incident, not the platform. This audience has sat through a 2 a.m. change
where the only person who could make it was asleep, and they have also sat through the
audit afterwards. Both halves are familiar; what is unfamiliar is having them solved by
the same thing.

Expect three objections, in this order:

- **"This adds a step to an emergency."** It removes one. Today the first step is
  finding whoever knows the password. Show the request-to-shell path end to end and
  time it.
- **"Our firewall already controls access."** The firewall decides which *traffic* is
  allowed. Nothing in it decides who may reconfigure it, and its logs do not carry a
  person's name. These are different questions with different answers.
- **"We can't put an agent on a Cisco."** Correct, and nothing here does. The Gateway
  sits beside the device and speaks SSH to it; the device is unmodified. This demo runs
  on VyOS because it is the network OS that can be stood up on demand — the access path
  is the same one you would point at an ASA or a PAN.
