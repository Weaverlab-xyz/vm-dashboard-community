# OT demo cell: who brokers identity

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you want to know how people and the Entitle agent are identified inside the plant network, where the broker sits, and what the Purdue zoning changes about it.

Part of [OT demo cell](../ot-demo-cell.md).

## Who brokers identity in the plant

Tick **Register the VM in Entitle** on the cell form and the deploy stands up a second
machine: the plant's **industrial-DMZ broker**, `<cell>-dmz`. It runs the same k3s
the cell does and carries the BeyondTrust Entitle agent and nothing else — no
simulators, because a DMZ host that answers Modbus is a lie about where it sits.

The reason it is a separate host is the reason a real plant has one: the thing with a
way out does not sit on the plant floor. With `ot_purdue_firewall_enabled` on, the two
zones are firewall rules you can read out loud:

| Zone | Rule | Effect |
|---|---|---|
| cell (`ot-sim`) | `<cell>-ot-egress-deny` | **no route out, ever** — the plant floor gets no allow at all |
| | `<cell>-ot-ingress-allow` | the PRA Gateway, on the cell's own ports |
| | `<cell>-ot-ingress-agent` | **tcp 22 from the DMZ zone** — the agent's only reach into the plant floor |
| | `<cell>-ot-ingress-deny` | everything else stops at the boundary |
| broker (`ot-dmz`) | `<broker>-dmz-egress-entitle-<hash>` | **tcp 443 + 8080 to the Entitle channel**, and nothing else |
| | `<broker>-dmz-egress-dns-udp` / `-tcp` | DNS to the metadata resolver |
| | `<broker>-dmz-egress-deny` | the rest of the internet |
| | `<broker>-dmz-ingress-allow` | tcp 22 from the PRA Gateway and the Config-Management runner |
| | `<broker>-dmz-ingress-deny` | everything else |

So the sentences the demo can now make, and prove in the console:

- nothing on the plant floor has a route out — not the HMI, not the PLCs, nothing;
- exactly one machine in the plant has one, and it is two ports to one destination;
- the agent that grants a vendor time-boxed access to the cell **runs inside the
  plant**, and reaches the cell on port 22 and no other port;
- stop the broker and the grants stop working, because there is no second path.

### The address problem, stated plainly

A firewall rule takes addresses; `agent.<region>.entitle.io` is a name, and BeyondTrust
publishes no range for it. So:

1. **`ot_entitle_egress_cidrs`** is the supported answer — the firewall ticket a real
   plant would have. Set it and the rule is built from it.
2. Left blank, the wiring **resolves the hostname once, at wiring time**, and records
   that it did. Honest, and not a contract: if BeyondTrust rotates those addresses the
   agent loses its channel until you **Re-wire**, which re-resolves. The rule's name
   carries a digest of the address set, so a changed set arrives as a new rule rather
   than being silently ignored.
3. Neither → the deploy **refuses**, naming the key. It never quietly widens to
   `0.0.0.0/0`. If you genuinely cannot get a list, `ot_dmz_egress_open_ports` allows
   the broker 443/8080 to anywhere — a weaker claim, opted into deliberately, and the
   plant floor is unaffected either way.

#### The card says which of the three you have

Those are three different sentences, and only one of them is "pinned to a list
BeyondTrust gave us". The cell card carries the answer so nobody has to go and check a
setting to find out which sentence they are allowed to say in front of a customer:

| Card | What it means |
|---|---|
| **pinned** | Built from `ot_entitle_egress_cidrs`. The strongest claim, and the one a real plant's firewall ticket produces. |
| **resolved once** | Built from a DNS answer at wiring time, with the timestamp. Honest, and not a contract. |
| **not pinned** (amber) | `ot_dmz_egress_open_ports` is on: 443/8080 to `0.0.0.0/0`. The plant floor is still closed and the broker still has no other port — but its destination is unbounded. |

A cell deployed before this existed shows nothing rather than the flattering guess.

#### And when it stops being true

The card also watches for **drift**: the rule still says what it said on wiring day,
but what it would be drawn from *now* has changed — the addresses moved, or the
`ot_dmz_egress_open_ports` toggle was flipped since. The remedy is always **Re-wire**,
which re-resolves and re-applies on all three clouds. Before this, the first symptom of
a rotation was an agent that had quietly lost its channel while the card still read
*agent installed*.

The check never claims drift it cannot prove: an unrecorded broker, or a DNS lookup
that fails, says nothing rather than raising a false alarm at a demo. It is cached for
five minutes because the cells list asks on every request; the wiring path deliberately
does not use that cache, because a rule must be drawn from a fresh answer.

#### Proving it, on demand

**Probe egress** on the cell card runs the agent play's own probe and nothing else: a
throwaway pod on the broker that checks DNS, 443, 8080 and the cell's :22, from where
the agent actually sits. A host-level `curl` is a different source address and a
different answer, which is why it runs as a pod.

It installs nothing — the play ends after the probe — so it is safe against a broker
with a healthy agent on it, and it needs neither the token nor the chart, so it also
works on a broker whose agent never installed. That is the case where the answer
matters most, and it is the thing to run when someone asks whether the boundary is
real.

What a production site does instead is FQDN egress (Cloud NGFW, AWS Network Firewall
domain lists, Azure Firewall application rules) or an L3.5 forward proxy. All three
cost real per-hour infrastructure, which is why the demo pins addresses — and saying
so is part of the conversation, not an apology for it.

### The probe, and why it exists

The agent runs as a **pod**, so whether its traffic leaves with the node's address is a
property of the CNI, not something to assume. Before helm runs,
the install play starts a one-shot pod that checks, in order: DNS resolves the endpoint →
tcp 443 opens → tcp 8080 opens → the cell's :22 opens. A failure names the three
candidates (pod SNAT, the DNS hole, the destination set) and stops. It is also the thing
to run in front of a customer who asks "so what else can this host reach?"

### What it needs, and what it costs

Each of these is refused **before any VM is launched**, with the remedy in the job error:

- a **broker image** baked with `OT_ROLE=broker` (see `provisioners/ot/README.md`),
  with `broker` in its name (e.g. `ot-broker`). The image does not record its role, so
  the pickers go by name: *DMZ broker image* lists names containing `broker`, and the
  cell's *Image* picker hides them. "Show all private images" lifts both filters;
- **`ot_purdue_firewall_enabled` on** — the agent's way out is a hole in the plant
  boundary, and without the boundary there is nothing to make a hole in;
- a **destination set**, per above;
- an **in-cloud Config-Management runner** — `ansible_runner_gcp` plus
  `gcp_run_subnetwork` or `gcp_ansible_vpc_connector`; `ansible_runner_aws` = ECS
  Fargate with `ansible_ecs_subnet_id`; `ansible_runner_azure` = ACI with
  `ansible_aci_subnet_id` in the cell's VNet — and
  **`ot_config_runner_source_cidr`**, set to that runner subnet's CIDR — the dashboard host has no route to a private
  broker, so the agent is installed from inside the VPC, and the broker's firewall has
  to admit that runner. **On all three clouds**, deliberately: SSM SendCommand and Azure
  Run Command would drop that inbound rule, but the SSM agent reaches AWS through three
  interface VPC endpoints on 443 that the DMZ zone denies, and a command's parameters
  are retained in its history — so the token would either need a fourth endpoint to
  fetch itself from, or would sit in that history as a live credential. One named source
  range inbound is the smaller hole and the better sentence: the broker's egress stays
  at exactly one destination;
- **`entitle_registration_enabled`** and a configured tenant **on `routing: v1`**. The
  token is a base64 JSON blob that says which it is, and the deploy reads it between
  minting and the first launch. A `v0` tenant's agent pulls straight from `ghcr.io` and
  `gcr.io/datadoghq`, which are CDN-backed and cannot be named in a narrow allow-list —
  so it would reach `CrashLoopBackOff` inside a subnet with no egress to fix it from.
  The same read compares the token's `platform` against the region the hole was drawn
  for: `entitle_egress.region()` derives that from `entitle_api_url` and falls back to a
  default when the URL is a proxy or a bare host, so a tenant can quietly be on `eu`
  while the rule points at `agent.us.entitle.io`. Either mismatch refuses, names the
  key, and destroys the token it just minted;
- the **8 GB broker shape** (`e2-standard-2`): the agent requests 1Gi on its own;
- the thing each cloud's zone needs in order to **name the PRA Gateway** as a source:
  nothing on GCP (a network tag always exists), **`bt_ecs_jumpoint_security_group_id`**
  on AWS, and **`azure_jumpoint_name`** on Azure. This is the expensive one to get
  wrong — a zone written without it denies the Gateway along with everything else, and
  the cell is unreachable by the only path the demo has, including the session you
  would use to undo it. So the deploy refuses instead.

All three clouds now, each through its own primitive — see *[Purdue-zone
firewalling](deploying.md#purdue-zone-firewalling)* on the deploying page for what that means where.

The cost is a second VM per cell, and on GCP the `gcp_vm_nat_enabled` guidance
**inverts**: the
subnet needs a NAT path for the broker's one hole to lead anywhere, and the plant's own
priority-800 deny outranks the NAT's priority-900 allow, so the cell stays closed with
the toggle on. That inversion only holds *with the Purdue zoning enabled* — which is
why the feature refuses without it.

The agent install runs the **same play an on-prem site runs**
([`ot/entitle-agent-install.yml`](https://github.com/Weaverlab-xyz/vm-dashboard-community/tree/main/examples/playbooks/ot)),
against the broker, with the token bound by reference through the run form's secret
channel. Its output is on its own job page, and the cell card shows *agent installing* /
*agent installed*.
