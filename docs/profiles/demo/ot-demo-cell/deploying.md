# OT demo cell: deploying a cell

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are baking the images and deploying an OT demo cell on AWS, Azure or GCP, and want every setting and step.

Part of [OT demo cell](../ot-demo-cell.md).

## Deploying a cell

1. **Bake the image once per cloud**: Storage page → upload
   `provisioners/ot/ot-sim-debian.sh`; then that cloud's *Build Image* tab → name
   `ot-sim`, pick the Debian 12 source, load the script from storage, build
   (~10–15 min). See `provisioners/ot/README.md` for the image contract and pins.
   - **GCP**: source family `debian-12` (Compute Engine resolves the family itself).
   - **AWS**: the *Debian 12* preset — it names an OS **family** the build resolves
     to the newest public AMI at launch (and sets the image's `admin` login user);
     a literal AMI ID pasted into the field still wins.
   - **Azure**: the *Debian 12* preset (marketplace `Debian/debian-12/12-gen2`).
     Linux builds always publish a **Compute Gallery image version** — Azure's
     managed-image export path is broken, gallery-version is the supported route.
2. **The cloud page → OT Demo Cell tab**: pick the image (the picker pre-filters names
   containing `ot-sim`), name the cell, **tick what to broker** — Modbus is pre-checked,
   each ticked entry becomes its own PRA tunnel, and the list's second group is the
   cell's own Kubernetes API rather than anything a PLC speaks — then pick the
   **PRA Jump Group + Gateway** that match the cell's region (see below) and deploy.
   The VM defaults
   to the 4 GB shape everywhere (`e2-medium` / `t3.medium` / `Standard_B2s`) — a 2 GB
   cell proved too tight for the PLC sims + FUXA in live use, and the k3s server idles
   at about 500 MB on top of them. On GCP and Azure
   the cell never gets a public IP (the form pins it); the GCP cell also carries the
   **`ot-sim`** network tag, which
   [Purdue-zone firewalling](#purdue-zone-firewalling) keys off. **On AWS there is no
   per-instance public-IP switch — the subnet decides — so keep the form's default
   private sandbox subnet**: the deploy now *refuses* a subnet that auto-assigns public
   IPs (see [the air-gap guard](#the-aws-air-gap-guard)).
3. The job page shows the parent `ot_cell_deploy` job driving one deploy child
   (`gce_deploy` / `ec2_deploy` / `azure_deploy`) — the child **is** the cell's
   inventory record.

### Choosing the Jump Group and Gateway

PRA objects are not region-scoped, so nothing stops a us-east1 cell from wiring into a
Jump Group named `centralus` through a us-central1 Gateway — which is exactly what the
configured defaults will do if they were set up for another region. Each cloud's cell
resolves the same fallback chain its own Shell Jump uses: GCP
`gcp_bt_jump_group_name` / `gcp_jumpoint_name`, Azure `azure_bt_jump_group_name` /
`azure_jumpoint_name`, AWS straight to the shared `bt_jump_group_name` /
`bt_jumpoint_name` — all falling back to the shared pair. The deploy form's
**BeyondTrust PRA placement** pickers (fed by `GET /api/pra/pickers`, same as the VM
deploy modal) override the defaults per cell: every jump item lands in the chosen
Jump Group and ride the chosen Gateway, and the PRA Vault checkout account is
associated to the same Jump Group. The standalone tunnel form has the same two
pickers. Left at "(configured default)", behaviour is unchanged.

### The gateway sizing guard

A Web Jump renders **headless Chromium on the PRA gateway host**. Below ~2 GB the
renderer is OOM-killed, and the session error is indistinguishable from a blocked
firewall. The cell deploy therefore checks the **live** managed gateway host (falling
back to the configured size key) and refuses early — before launching anything — with
the remedy in the job error. Per cloud, the key and the sizes the remedy names (all
under **Settings → Integrations → Privileged Remote Access**):

| Cloud | Size key | Minimum | Preferred | Fresh-install default |
|---|---|---|---|---|
| GCP | `gcp_jumpoint_machine_type` | `e2-small` | `e2-medium` | `e2-medium` — but the sandbox setup scripts seed `e2-micro` (1 GB) to keep standing cost down, so on a sandbox install the refusal is the out-of-the-box experience |
| AWS | `bt_ecs_host_instance_type` | `t3.small` | `t3.medium` | `t3.small` (2 GB — exactly the minimum) |
| Azure | `azure_jumpoint_vm_size` | `Standard_B1ms` | `Standard_B2s` | `Standard_B2s` (4 GB) |

Changing a key never resizes a live gateway: delete the existing gateway host so the
next deploy recreates it at the new size, then retry the cell.

The guard reasons about the **dashboard-managed shared gateway only**. When the form's
Gateway picker overrides it with another Gateway, the Web Jump renders on a host
this install cannot size, so the guard steps aside (noted in the job progress) — the
≥2 GB requirement then rests on whoever runs that Gateway.

### The AWS air-gap guard

GCE and Azure let a deploy pin the external IP off per instance, and the OT forms do.
EC2 has no such switch — the subnet's `MapPublicIpOnLaunch` decides — so on AWS the
air gap used to rest entirely on the operator picking the right subnet, and a cell that
came up internet-addressable still looked like a successful deploy.

The AWS cell deploy now reads the chosen subnet before launching anything and refuses
one that auto-assigns public IPs, naming the subnet and the remedy in the job error. A
subnet it cannot read (a transient `DescribeSubnets` failure) is **not** a refusal — an
AWS blip must not look like a misconfigured subnet. Turn the check off with
**`ot_aws_require_private_subnet`** (Settings → Integrations → Privileged Remote Access)
if a public subnet is genuinely what you want.

### Purdue-zone firewalling

*Optional, default off:* **`ot_purdue_firewall_enabled`**. One toggle, three
implementations — and the differences between them are the point, not an
implementation detail, because each cloud's primitive can be made to *look* like a
boundary while enforcing nothing:

| | What a zone is | The trap | Applies to |
|---|---|---|---|
| **GCP** | VPC firewall rules on network tags, with priorities and explicit DENY | none — rules exist independently of the instance | every cell, with or without a broker |
| **AWS** | Security groups: pure allow-lists, no priority, no deny. "Denied" is what the group does not contain | Groups **union** their allows, so a zone must *replace* an instance's groups, not join them — and a new group is created allowing **all egress**, so the air gap is made by revoking that rule | cells with a DMZ broker |
| **Azure** | NSG rules on the NIC: priorities and real Deny, closest to GCP | Azure's **default outbound access** gives a VM with no public IP a route to the internet, and nothing created an NSG for a cell at all — so the outbound Deny *is* the air gap here, not hardening on top of one | cells with a DMZ broker |

On AWS and Azure the zoning applies only to a cell that also has a broker — i.e. one
deployed with Entitle. Those two clouds' cells have live miles on them and their zoning
*replaces* something, so it arrives with the feature that needs it rather than changing
an existing cell's posture underneath it. GCP's rules are additive and unchanged.

**A finding worth stating plainly:** before this, an Azure cell's air gap was a claim in
this document and nothing else. The form pins the public IP off, but default outbound
access means the VM still reaches the internet, and the cell's `nsg_ids` are
operator-supplied with nothing creating one. The outbound Deny above is the first code
that makes the claim true on Azure.

#### On GCP

The GCP cell has always carried
the `ot-sim` network tag, but nothing consumed it — the cell's isolation was really the
sandbox's posture (no NAT on the VM subnet, no public IP). That posture is one toggle
away from evaporating: `gcp_vm_nat_enabled` adds a priority-900 EGRESS ALLOW on the VM
tag *every* cell also carries, so switching on on-demand egress for one ordinary VM
quietly gives every plant cell in the sandbox a route to the internet.

Enabled, the wiring gives each cell three rules of its own, on its `ot-sim` tag:

| Rule | Priority | Effect |
|---|---|---|
| `<cell>-ot-egress-deny` | 800 | EGRESS DENY all → `0.0.0.0/0` — no route out, whatever the NAT toggle says |
| `<cell>-ot-ingress-allow` | 800 | INGRESS ALLOW tcp from **`source_tags=[bt-jumpoint]`** on 22, the HMI port and every preset protocol port |
| `<cell>-ot-ingress-deny` | 810 | INGRESS DENY all from `0.0.0.0/0` — everything else stops at the plant boundary |

800 is chosen to outrank both the on-demand egress ALLOW (900) and the sandbox's
standing VM-tag DENY (1000), so the air gap holds regardless of how those are set.

Two deliberate properties:

- **The Gateway is matched by network tag, not address.** The shared Gateway is
  ref-counted and recreated on demand; a pinned `/32` would stop matching the day it
  came back with a new internal IP, and the symptom — a Web Jump that times out — is
  exactly what the troubleshooting table teaches you to read as an undersized gateway.
- **The catch-all ingress DENY is never created without its paired Gateway ALLOW.** If
  the allow fails, the wiring stops there and says so: a cell fenced away from the
  Gateway brokering the session you would use to fix it is the one failure worth
  designing against.

A cell that brokers its own identity gets a fourth rule, `<cell>-ot-ingress-agent`:
**tcp 22 from the `ot-dmz` zone**, so the plant's own Entitle agent can mint ephemeral
accounts and do nothing else. Its own line rather than another source on the Gateway's,
so the audit reads as the sentence it is — and the DMZ host gets a zone of its own,
described in [Who brokers identity in the plant](identity.md#who-brokers-identity-in-the-plant).

The rules are recorded on the child job as they are created, so a destroy removes
exactly what exists and **Re-wire** adds them to a cell deployed before you turned the
flag on.

#### On AWS

Two security groups, and the instance's group set *becomes* the zone:

| Group | Ingress | Egress |
|---|---|---|
| `<cell>-ot-zone` | tcp from the Gateway host's group (`bt_ecs_jumpoint_security_group_id`) on 22, the HMI port and every preset protocol port; tcp 22 from the broker's group | **none** — every rule revoked, including the allow-all AWS creates the group with |
| `<broker>-dmz-zone` | tcp 22 from the Gateway's group and `ot_config_runner_source_cidr` | tcp 443 + 8080 to the Entitle set; udp/tcp 53 to `169.254.169.253` |

There is no priority and no deny rule, because a security group does not have them —
which reads *better* in a demo (`aws ec2 describe-security-groups` is the whole
boundary, with no ordering caveat) but sets two traps this implementation has to avoid.
Groups union their allows, so the zone **replaces** the groups you picked in the form
rather than joining them; and the egress set is managed whole, so the plant's air gap
is made by revoking AWS's default allow-all rather than by declining to add one.

The group is looked up by `(name, VPC)`, and nothing on an EC2 deploy records the VPC —
only the subnet — so the wiring resolves it once and writes it onto both job rows.

#### On Azure

Two NSGs, attached to the VMs' NICs (a NIC carries at most one, so this replaces
whatever was there):

| NSG | Direction | Priority | Rule |
|---|---|---|---|
| `<cell>-ot-zone` | Inbound | 800 | ALLOW tcp from the Gateway's address on 22, the HMI port and every preset port |
| | Inbound | 810 | ALLOW tcp 22 from the broker's address |
| | Inbound | 900 | DENY all |
| | **Outbound** | **800** | **DENY all** — the rule that makes the air gap real |
| `<broker>-dmz-zone` | Outbound | 790 | ALLOW tcp 443 + 8080 to the Entitle set |
| | Outbound | 791/792 | ALLOW udp/tcp 53 to the `AzurePlatformDNS` service tag |
| | Outbound | 800 | DENY all |
| | Inbound | 800 | ALLOW tcp 22 from the Gateway and `ot_config_runner_source_cidr` |
| | Inbound | 900 | DENY all |

The outbound allows sit at 790–792 so they outrank the 800 deny, the same way GCP's do.
`AllowInternetOutBound` is a platform default at 65001, so any deny below that closes
the cell.

**The Gateway is matched by address here, not by a tag.** Azure's honest analogue of a
network tag is an Application Security Group, which would have to be attached to the
Gateway VM's own NIC — a change to the one Azure path with live miles on it. So the
address is resolved from `azure_jumpoint_name` at wiring time, and **Re-wire** repairs
the zone if the Gateway is ever rebuilt.

### Partial failures and re-wiring

Wiring failures (Web Jump, tunnel or the PRA-checkout pair) fail the parent job with
the remedy in its error message, but the VM and any completed wiring stay. Every
artifact is written to the child job's metadata the moment it exists, so the
**Re-wire** button (`POST /api/ot/cell/{vm_job_id}/rewire`) retries only the missing
pieces, and a destroy cleans exactly what exists. A cell deployed **before** the
PRA-checkout feature shows *wiring incomplete* once (its Password Safe onboarding
exists but the checkout pair doesn't) — Re-wire retrofits exactly the missing pieces.

### Clearing a cell whose VM never deployed

A failure *before* the VM exists is a different case, because the cell's inventory
record is the VM-deploy child row and Destroy only acts on a **completed** deploy.
Such a card has neither Re-wire (there is nothing to wire — wiring runs only after
the VM is up) nor Destroy, and there is no Terraform state to fall back on: the cell
VM is an SDK deploy, and only the wiring uses Terraform. Use **Clear**
(`DELETE /api/ot/cell/{vm_job_id}`), which retires the record:

- The VM is **probed first**. One that still exists is refused — clearing the record
  is exactly how a VM nobody is tracking keeps billing — so destroy it from the
  cloud's VMs tab and then clear. If the probe cannot answer (no credentials, no read
  access to its resource group), the page asks you to confirm and re-sends with
  `force=true`; check the cloud console first.
- The shared Gateway reference this deploy took is released, so a host kept alive
  only by the failed cell is reclaimed.
- The row keeps its **failed** status and its error message — Clear only hides the
  card. The job page stays as the record of what went wrong.

On a stuck Azure create the job's error now quotes what ARM reported (provisioning
state, instance-view statuses, whether the guest agent ever checked in) — an absent
guest agent after the deploy deadline points at the *image*, not the VM size.
