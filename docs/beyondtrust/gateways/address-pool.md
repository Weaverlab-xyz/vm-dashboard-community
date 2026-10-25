# The network tunnel address pool

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are using PRA network tunnels and need to set or check the address pool they hand out.

Part of [BeyondTrust Gateway Hosts](../gateways.md).

A **Protocol Tunnel** working tells you nothing about whether a **Network Tunnel** will. A
protocol tunnel opens an outbound TCP socket; a network tunnel leases the operator a *real
address on the target network* for the life of the session. That lease needs three things from
the cloud fabric, and the dashboard does all three.

| | Azure | AWS | GCP |
|---|---|---|---|
| **1** the container sees the real network | `--network host` on the container | `networkMode: "host"` on the ECS task — always had it | konlet runs the container on the host network |
| **2** the fabric forwards for it | `enableIPForwarding` on the NIC | `SourceDestCheck=false` on the host | `canIpForward` on the instance |
| **3** the pool is registered | secondary ipconfigs on the NIC | secondary private IPs on the host's ENI | an **alias IP range** on the NIC |

Requirement 1 is why a protocol tunnel is no evidence: an outbound TCP socket is NAT'd out of a
bridged container quite happily, so it works while the gateway cannot see the target subnet at
all. Azure was the only cloud that had this wrong.

**Requirement 3 is the one with a manual counterpart.** The gateway asks DHCP for its lease
first; no cloud answers, because cloud DHCP only serves an address already bound to a NIC. It
falls back to the pool, then **ARPs to validate the address it picked**. An address the fabric
does not know about gets no reply:

```
Timeout: No valid ARP reply received from 10.99.5.202 in 2000ms
Allocated IP address [10.99.5.202] not an existing Azure resource?  Is it configured as a
  secondary IP address on the virtual NIC of the Gateway VM?
setup: Exception - Address: 0.0.0.0 not found
```

A route table is **not** the answer on any of them — the product is cloud-aware and asks for
registered addresses by name.

## Where the pool comes from

Blank (the default) **derives** it from the gateway subnet: 8 addresses from the **top**,
because every cloud allocates dynamically from the bottom (`.4` upward), so the top stays clear
of real hosts longest. Each cloud's reserved addresses are excluded, and they differ — Azure and
AWS reserve `.1`/`.2`/`.3`, GCE reserves `.1` and the *second-to-last*. For a `10.99.5.0/24`:

| Cloud | Derived pool |
|---|---|
| Azure, AWS | `10.99.5.247`–`10.99.5.254` |
| GCP | `10.99.5.240/29` |

To pin one instead, set `jumpoint_tunnel_pool` for the region (or the flat per-cloud key) to a
range `10.99.5.200-10.99.5.207`, a CIDR, or a single address. `off` disables the pool entirely.
An unparseable value falls back to deriving rather than leaving the gateway with no pool, and an
oversized one is capped — a pasted `/16` would otherwise try to create 65k addresses.

> **GCP takes a CIDR, not a range.** An alias IP range *is* a prefix, so `10.99.5.240/29` is
> accepted and `10.99.5.240-10.99.5.247` is refused rather than silently widened to the
> enclosing block — widening would hand you addresses you never listed, which is how you
> collide with a live host.

> **Prefer the per-region key.** A pool only makes sense inside its own subnet's prefix, so a
> flat key is the wrong answer in every region but the default one — the same trap described in
> [Placement and the region picker](deploying.md#placement-and-the-region-picker).

## One pool per subnet

The pool is derived from the *subnet*, so every gateway in a subnet derives the same addresses
and only the first can own them. A second gateway's registration is refused (AWS: the address is
already assigned; GCE: alias ranges may not overlap). This degrades rather than breaks — pool
registration is never fatal, so the second gateway comes up and simply carries no network
tunnel, with the reason in the log.

The exception is GCP's **paired** mode (`gcp_vm_jumpoint_mode = paired`), which builds a gateway
*per VM*. There the collision is guaranteed and would fail the instance insert outright, so that
path opts out of the pool explicitly. Network tunnels are a property of the **shared** gateway.

## GCP gateways cannot be upgraded in place

Azure and AWS apply requirements 2 and 3 on **reuse** as well as create — both are live API
calls — so an existing gateway picks the pool up on the next ensure without a rebuild.

GCE is different: `canIpForward` and alias ranges are settable only when the instance is created
(or while it is TERMINATED). The ensure path is called routinely by VM, cloud-database and
Kubernetes deploys and reuses the shared gateway, so repairing in place would mean stopping a
gateway and dropping every live session on it. Instead it logs:

```
tunnel-pool: gateway 'clouddb-shared-jumpoint' predates network-tunnel support
(canIpForward=false, no alias IP range) — protocol tunnels are unaffected, but a
NETWORK tunnel needs the VM recreated
```

Delete the gateway VM and let the next deploy recreate it.

## It has to match the appliance, and nothing checks that for you

The pool must equal **Managed IP Addresses for Protocol Tunnel** on the Gateway in the
Pathfinder console (`app.beyondtrust.io` → Privileged Remote Access → Jump → the Gateway →
Gateway Platform). That section only appears once **Enable Protocol Tunnel Method** is ticked,
which is why it reads as "missing" if you go looking for the IP list first. It is **not** in the
appliance's `/login`.

The dashboard can neither read nor write that setting, so a mismatch is invisible until a
session fails its ARP check. For that reason the resolved pool is logged at INFO when the
gateway is ensured:

```
gateway-host(azure): network-tunnel pool on clouddb-jumpoint = 10.99.5.247, … — this must
match Managed IP Addresses for Protocol Tunnel on the Gateway in the Pathfinder console
```

AWS and GCP log the same line against their own gateway; on GCP it names the alias range.

Success looks like the gateway binding a lease as a `/32` on its interface
(`10.99.5.204/32`). Note the pool addresses do **not** answer ping — they are registered with
the fabric but not bound in the guest OS — and that is fine: the agent checks ARP, not ICMP.
