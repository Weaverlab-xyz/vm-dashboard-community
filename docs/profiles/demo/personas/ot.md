# OT / ICS Engineer

Runs the plant network. Owns the PLCs, the HMI, the historian — and a set of constraints no
IT engineer has: the equipment cannot be patched on your schedule, it often cannot be
patched at all, and it speaks protocols that were designed on the assumption that anything
able to reach them was already trusted.

Their access problem is not "who should log in". It is **the vendor**. Somebody from the
integrator has to reach a controller to commission a line or diagnose a fault, and the
options historically were a VPN into the plant network, a jump box with a shared password,
or someone driving to site.

## Why this is the flagship OT story

Secure third-party access into a plant network is the OT PAM use case. Everything else —
credential rotation, session recording, least privilege — matters, but this is the one that
gets budget, because it is the one that has caused incidents.

What a demo has to show, concretely:

- the plant network has **no inbound firewall rule and no VPN**, and still someone reaches
  the HMI;
- they reach it in a **recorded browser session**, with the credential injected rather than
  told to them;
- they read **live process data** over the plant's own protocol, through a tunnel;
- the access **expires on its own**.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Stands up a simulated cell — PLC simulators plus a web SCADA/HMI — inside a private, egress-less subnet. The air gap *is* the demo. |
| **PRA** | The only way in. A Web Jump to the HMI, and one protocol-aware tunnel per protocol, so a policy can grant Siemens and Rockwell separately rather than as one opaque item. |
| **Password Safe** | Owns the cell's admin credential, mirrors it into the PRA vault and rotates it, so a rep injects a real secret without seeing it. |
| **Entitle** | Makes the vendor's access time-boxed instead of standing. |

## Use cases

Each of these appears as a card under **Use cases** in the dashboard when this focus is
active.

### Stand up a Modbus cell and tunnel to it

Deploy the cell, then read its holding registers on TCP 502 through a brokered tunnel. The
values change every second, so a client visibly shows live process data rather than a static
mock — which is what stops the demo reading as a mock.

**Guide:** [OT Demo Cell](../ot-demo-cell.md)

### One cell, four protocols

The same four process values served over Modbus, OPC UA, Siemens S7comm and Rockwell
EtherNet/IP, each as its own tunnel. This is what turns "we are a Siemens shop" and "we are
a Rockwell shop" into the same demo rather than two.

**Guide:** [OT Demo Cell](../ot-demo-cell.md)

### Time-bound vendor access to one cell

Grant the integrator two hours on a single cell, then watch the grant expire and the tunnel
close by itself. The point to land is that nobody had to remember to revoke it.

**Guide:** [Entitle user JIT](../../../design/entitle-user-jit.md)

### Check out the cell's admin credential in PRA

The cell's account is onboarded in Password Safe, mirrored onto the PRA vault and rotated
once, so the credential a rep injects is real and current from the first session.

**Guide:** [OT Demo Cell](../ot-demo-cell.md) · [Password Safe](../../../integrations/password-safe.md)

### Where the Gateway sits, and why that is the whole story

The architecture slide, told against a live gateway instead of a diagram: the broker inside
the plant segment dials *outward*, so there is nothing to open on the perimeter.

**Guide:** [Gateways](../../../integrations/gateways.md) ·
[Privileged Remote Access](../../../integrations/privileged-remote-access.md)

## What to enable

**Privileged Remote Access** is the only requirement, and it is not optional: the OT feature
is gated on it, because a cell nobody can reach would be a VM with no purpose. Add
**Password Safe** for the credential-checkout story and **Entitle** for the expiry story.

The cell runs on AWS, Azure or GCP, so the focus also needs one cloud configured — which
means it is a demo-instance story. On a [POV instance](../../../pov-instance.md) the cloud consoles
are masked and these cards say so rather than offering a link that would 404.

## Talking to this buyer

Two habits worth keeping:

- **Do not lead with the dashboard.** Lead with the plant network having no route in. The
  tooling is interesting only after that lands.
- **Say "recorded", not "logged".** In OT, an auditable video of what a vendor did to a
  controller is a different category of assurance from a line in a log file, and the
  distinction is one the customer will already care about.
