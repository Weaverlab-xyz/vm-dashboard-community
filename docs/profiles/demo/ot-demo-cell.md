# OT Demo Cell

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are showing an air-gapped plant cell and the PAM layers on top of it.

The dashboard can stand up a simulated **OT/ICS plant cell** — Modbus, Siemens
S7comm, Rockwell EtherNet/IP and OPC UA PLC simulators plus the FUXA web SCADA/HMI —
inside a cloud sandbox's **private,
egress-less subnet**, then layer the BeyondTrust PAM stack on top. Same
**provisioning + three layers** model as [Cloud VMs](../../cloud-vms.md); the OT twist is
that the air-gapped subnet *is* the plant network, and every path in is PRA-brokered:

- **Provisioning** *(stand it up)* — deploy a VM from the Packer-baked **`ot-sim`**
  image (`provisioners/ot/ot-sim-debian.sh`). Everything is baked at build time, so the
  running cell needs **zero outbound internet**: a PLC simulator whose holding registers
  tick every second (:502), the same four process values over **Siemens S7comm** (:102),
  **Rockwell EtherNet/IP** (:44818) and **OPC UA** (:4840), and FUXA (:1881) with its PLC
  connection pre-seeded. They run as workloads of **[KubeSolo](../../kubesolo.md)** —
  the single-node Kubernetes this repo puts on plant hosts — so the cell is a plant IPC
  running a real cluster, and its Kubernetes API (:6443) is one more thing PRA can
  broker. A systemd unit applies the workloads at boot.
- **Layer 1 — PRA** *(reach it)* — auto-provisioned per cell:
  - **Web Jump** → `http://<vm>:1881` (the HMI, rendered and recorded on the gateway);
  - **one Protocol Tunnel per endpoint you tick** (generic TCP), named
    `ot-<cell>-<protocol>` — so a rep sees the Siemens PLC and the Rockwell PLC as
    distinct targets and a Jump Group policy can grant them separately, rather than
    one opaque item opening every port. The cell's own **Kubernetes API** is on the
    same list (`ot-<cell>-kubesolo` → :6443), so "read the PLC but not the cluster"
    is a policy decision rather than a network one;
  - **Shell Jump** → SSH, inherited from the cloud's normal VM deploy path.
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional, default on.* The
  image's `adminuser` is onboarded via the cloud-native plugin the cloud's VM deploy
  path already uses — **`gcpvm`** on GCP (managed system address
  `projectId/zone/instanceName`), **`ssm`** on AWS (managed system DNS
  `{instance-id}:{region}`, over Systems Manager), **`azurevm`** on Azure (address
  `tenantId/subscriptionId/resourceGroup/vmName`, over Run Command). On top of that,
  the wiring makes the credential **usable in PRA**: a PRA Vault username/password
  account plus a Password Safe mirror on the "PRA Vault Username Password" plugin,
  linked with SyncedAccounts, then rotated once so PRA holds a real credential from the
  start — see
  [PRA checkout of the cell's admin credential](#pra-checkout-of-the-cells-admin-credential).
- **Layer 3 — Entitle** *(grant time-boxed access)* — *optional.* SSH ephemeral
  accounts — brokered by an agent running **in the plant**, on a DMZ host deployed
  beside the cell, because that is the only arrangement in which "Entitle manages
  access to plant resources" is true as stated. See
  [Who brokers identity in the plant](#who-brokers-identity-in-the-plant).

Runs on **GCP, AWS and Azure** — each cloud page has its own *OT Demo Cell* tab, and
the cell's VM is that cloud's plain deploy child (`gce_deploy` / `ec2_deploy` /
`azure_deploy`), so admission policy, expiry, Shell Jump and Password Safe behave
exactly as on a normal VM there. The whole feature is gated on **`pra_enabled`** (the
router and the tabs all follow it) — a cell without PRA would be a VM nobody can
reach, by design.

---

## Why this demos well for OT customers

Secure third-party/vendor access into plant networks is the flagship OT PAM use case:
no VPN, no inbound firewall holes, recorded sessions, credentials injected rather than
shared. The cell makes that concrete — the "plant" has **no public IP and no egress**,
yet a rep reaches the HMI in a recorded browser session, reads live Modbus registers
through a tunnel, and never learns a credential. The values *change* every second
(counter, temperature, flow), so a client through the tunnel visibly shows live process
data, not a static mock — and the same four values are served over **Modbus, Siemens
S7comm, Rockwell EtherNet/IP and OPC UA**, so the story holds whichever protocol the
customer's plant speaks. Ticking several protocols on one cell is what turns "we are a
Siemens shop" and "we are a Rockwell shop" into the same demo.

The cell also **is** a [KubeSolo](../../kubesolo.md) host — the simulators are its
workloads — so the other half of the OT conversation, "we cannot carry Kubernetes on
plant hardware", has a running answer instead of a slide. See
[The cell runs on KubeSolo](#the-cell-runs-on-kubesolo).

## The cell runs on KubeSolo

The baked image installs **[KubeSolo](../../kubesolo.md)** — single-node, etcd-free
Kubernetes, a control plane of about 200 MB — and runs the four simulators and FUXA on
it as Deployments in the `ot-sim` namespace. Nothing the customer sees changes: same
images, same ports, same Web Jump, same protocol tunnels. Every workload runs with
`hostNetwork`, so it binds the node's own address, which is exactly what the Gateway
dials.

What changes is what the cell stands for. KubeSolo is the answer this repo gives an OT
customer who cannot put a cluster on plant hardware, and the cell is the only plant
floor it ships; while that ran docker compose, the answer was a slide. Now the plant
IPC carries a real cluster, takes stock Helm charts, and the only route to its API is a
recorded PRA session.

Tick **Kubernetes API (KubeSolo)** on the deploy form and the cell gets a tunnel to
`:6443` beside its fieldbus ones, named `ot-<cell>-kubesolo`. With that jump started in
the rep console:

```bash
# once, through the Shell Jump: the cell writes a kubeconfig aimed at the tunnel
sudo cat /var/lib/ot-sim/kubeconfig-via-tunnel.yaml    # → save it as ot-cell.yaml

# then from your own machine, through the tunnel
kubectl --kubeconfig ot-cell.yaml -n ot-sim get pods -o wide
kubectl --kubeconfig ot-cell.yaml -n ot-sim logs deploy/ot-plc
```

That file is the admin kubeconfig with two edits: `server: https://127.0.0.1:6443` (the
tunnel's local end) and a `tls-server-name` of the cell's private IP, because the API
certificate is issued to the cell and not to your loopback. Without the second line the
first command fails on a certificate error that reads like a broken tunnel.

**How it stays air-gapped.** The cell has no egress and no registry, so both halves of
the image supply are baked: KubeSolo is installed from its **`-offline` build**, whose
binary carries CoreDNS and the rest of what it starts, and the simulator and FUXA
images are exported to `/var/lib/ot-sim/images` and imported into KubeSolo's containerd
(`imagePullPolicy: Never`, so a missing image says *not in the local store* instead of
producing an `ImagePullBackOff` that reads like a firewall). The first boot of a cell
therefore takes a few minutes longer than a docker one: it mints the cluster's own CA
and node identity — the bake deliberately does not clone those into every cell — and
re-imports whatever is not already in its store. `systemctl status ot-sim` shows that
progress; the Web Jump is worth trying only once it reports `active (exited)`.

**Docker is not on the cell.** KubeSolo's installer refuses a host that still carries
Docker — it brings its own containerd and CNI — so the bake builds the images with
Docker and then purges it. `docker ps` on a cell is now `kubectl -n ot-sim get pods`,
and `docker logs ot-plc` is `kubectl -n ot-sim logs deploy/ot-plc`. `kubectl` and
`helm` are on the cell (KubeSolo ships neither, and the plays in
[`examples/playbooks/kubesolo/`](https://github.com/Weaverlab-xyz/vm-dashboard-community/tree/main/examples/playbooks/kubesolo)
expect both). Baking with **`OT_RUNTIME=docker`** brings the old compose stack back
unchanged if a KubeSolo bake ever fails on a platform the script has not met.

**Tell the form which one you baked.** The deploy form has an *Image runtime* picker,
because the dashboard cannot read this off an image — both bakes produce an `ot-sim`
image, and the runtime is a bake-time environment variable that leaves no trace the
cloud exposes. It gates one thing: the cell's own platform endpoints, which exist
because the cell runs a cluster. Pick `docker` and the *Kubernetes API (KubeSolo)*
entry disappears from the protocol list, and the deploy refuses it if you send it
anyway — a tunnel to :6443 on a cell with no cluster is a session that fails exactly
like a blocked firewall, which is the most expensive kind of demo failure there is.

**The agent does not run here** — it runs on the plant's DMZ broker, one zone over, and
that is the whole point of [Who brokers identity in the plant](#who-brokers-identity-in-the-plant).
The plant floor keeps a true air gap; the machine with a way out is a different machine.

## Who brokers identity in the plant

Tick **Register the VM in Entitle** on the cell form and the deploy stands up a second
machine: the plant's **industrial-DMZ broker**, `<cell>-dmz`. It runs the same KubeSolo
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

What a production site does instead is FQDN egress (Cloud NGFW, AWS Network Firewall
domain lists, Azure Firewall application rules) or an L3.5 forward proxy. All three
cost real per-hour infrastructure, which is why the demo pins addresses — and saying
so is part of the conversation, not an apology for it.

### The probe, and why it exists

The agent runs as a **pod**, so whether its traffic leaves with the node's address is a
property of the CNI — and KubeSolo documents nothing about masquerade. Before helm runs,
the install play starts a one-shot pod that checks, in order: DNS resolves the endpoint →
tcp 443 opens → tcp 8080 opens → the cell's :22 opens. A failure names the three
candidates (pod SNAT, the DNS hole, the destination set) and stops. It is also the thing
to run in front of a customer who asks "so what else can this host reach?"

### What it needs, and what it costs

Each of these is refused **before any VM is launched**, with the remedy in the job error:

- a **broker image** baked with `OT_ROLE=broker` (see `provisioners/ot/README.md`);
- **`ot_purdue_firewall_enabled` on** — the agent's way out is a hole in the plant
  boundary, and without the boundary there is nothing to make a hole in;
- a **destination set**, per above;
- an **in-cloud Config-Management runner** (`ansible_runner_gcp`, plus
  `gcp_run_subnetwork` or `gcp_ansible_vpc_connector`) and
  **`ot_config_runner_source_cidr`** — the dashboard host has no route to a private
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
firewalling](#purdue-zone-firewalling)* below for what that means where.

The cost is a second VM per cell, and on GCP the `gcp_vm_nat_enabled` guidance
**inverts**: the
subnet needs a NAT path for the broker's one hole to lead anywhere, and the plant's own
priority-800 deny outranks the NAT's priority-900 allow, so the cell stays closed with
the toggle on. That inversion only holds *with the Purdue zoning enabled* — which is
why the feature refuses without it.

The agent install runs the **same play an on-prem site runs**
([`kubesolo/entitle-agent-install.yml`](https://github.com/Weaverlab-xyz/vm-dashboard-community/tree/main/examples/playbooks/kubesolo)),
against the broker, with the token bound by reference through the run form's secret
channel. Its output is on its own job page, and the cell card shows *agent installing* /
*agent installed*.

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
   cell proved too tight for the PLC sims + FUXA in live use, and KubeSolo's control
   plane idles at ~200 MB on top of them. On GCP and Azure
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
described in [Who brokers identity in the plant](#who-brokers-identity-in-the-plant).

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

## PRA checkout of the cell's admin credential

Password Safe onboarding alone puts `adminuser` on the **GCP VM SSH Rotation**
platform — rotatable, but invisible to PRA. To make it **checkout-able and injectable
in PRA**, the wiring adds three linked artifacts (skipped when the Password Safe
checkbox is off, or via `ot_ps_pra_checkout_enabled=false`):

1. a **PRA Vault username/password account** `<cell>-adminuser`, associated to the
   cell's Jump Group (`criteria.shared_jump_groups`, and placed in
   `bt_vault_account_group_id` when set so a group policy grants it to users). It is
   born with a throwaway placeholder password;
2. a **Password Safe mirror**: managed system `<cell>-pravault` on the **"PRA Vault
   Username Password"** plugin with a managed account named exactly like the Vault
   account — the plugin resolves its PRA-side target **by name** and PATCHes each
   credential change into it;
3. a **SyncedAccounts link** making the mirror a subscriber of the cell's `adminuser`
   account, followed by one Change Password on the parent so PRA holds a real
   credential immediately (the deploy-time initial mint ran before the link existed).

That last rotation is governed by **`ot_ps_checkout_converge`** (default **on**), and
deliberately *not* by the cloud's change-on-register flag. The two answer different
questions: change-on-register asks "rotate the credential when we first onboard it?",
the converge asks "a subscriber appeared *after* the mint — push one change through
it?". They only looked alike on GCP and Azure, where the flag defaults on. On AWS
`passwordsafe_ssm_change_password_on_register` defaults **off** (SSM auto-management
rotates on its own schedule), so every fresh AWS cell's Vault account held the throwaway
placeholder until some later rotation — a checkout that hands the rep a password which
does not log in. With `ot_ps_checkout_converge` off, that is the behaviour you get back,
on every cloud.

From then on Password Safe owns the propagation: every `adminuser` rotation lands in
the PRA Vault account, no credential passes through the dashboard, and in the PRA rep
console the account appears under **Vault Accounts** for checkout — and is offered for
**injection** when starting any of the cell's jump items (it is associated at the Jump
*Group* level, so Shell Jump, Web Jump and tunnel all see it).

**Prerequisites** (one-time, tenant-side): the "PRA Vault Username Password" custom
plugin platform imported in Password Safe, and a functional account on it (username =
the PRA OAuth client id, password = its secret). Name it in
`ot_ps_pravault_functional_account` — or leave that blank if
`clouddb_ps_pravault_functional_account` is already set for the cloud-DB feature; the
OT cell falls back to it (same for `ot_ps_pravault_platform` →
`clouddb_ps_pravault_platform`). The API identity also needs *Password Safe Account
Management* for the SyncedAccounts link.

## Using the protocol tunnels

A PRA **Protocol Tunnel Jump** forwards raw TCP from the rep's machine to the target
through the Gateway: start the jump item in the **PRA representative console** (it
shows "tunnel established" with the listen port) and point your client at
**`127.0.0.1:<local port>`** — the local port defaults to the protocol's canonical
port, so client configs read naturally. The session is audited/recorded like any other
jump; closing it closes the listener.

**Setting up a rep machine:** [OT protocol clients on Windows](ot-protocol-clients.md) walks through installing the four Python clients and
running [`scripts/ot/verify_tunnels.py`](../../../scripts/ot/verify_tunnels.py), which
reads every protocol through its tunnel and tells you which are live *before* you
share your screen.

Per-protocol, with a client to demo with:

| Preset | Port | Client through `127.0.0.1:<port>` | Against the `ot-sim` cell |
|---|---|---|---|
| Modbus TCP | 502 | `mbpoll -a 1 -r 1 -c 4 127.0.0.1`, QModMaster, pymodbus | **Yes** — holding registers 0–3 tick every second |
| OPC UA | 4840 | UaExpert, `opcua-client` (endpoint `opc.tcp://127.0.0.1:4840`) | **Yes** — `Objects/Plant` → Counter, Temperature, Flow, Running; anonymous, no security policy |
| EtherNet/IP | 44818 | pylogix, cpppo (`Logix` driver at 127.0.0.1) | **Yes** — the same four values as `DINT` tags |
| Siemens S7comm | 102 | python-snap7 (`db_read(1, 0, 8)`), TIA Portal (PLC at 127.0.0.1) | **Yes** — the same four values as big-endian DB1 words at offsets 0/2/4/6 |
| Kubernetes API (KubeSolo) | 6443 | `kubectl --kubeconfig ot-cell.yaml` — the file the cell writes at `/var/lib/ot-sim/kubeconfig-via-tunnel.yaml` | **Yes** — the cell's own cluster; the simulators are its `ot-sim` namespace |
| DNP3 | 20000 | OpenDNP3 master, Axon Test | No — standalone tunnel to real/lab gear |

Notes that save demo time:

- **The cell answers Modbus, Siemens S7comm, EtherNet/IP and OPC UA — DNP3 it does
  not.** `opendnp3` needs a native library built from source, which the baked image's
  everything-is-a-pinned-wheel contract cannot honour, so that one preset exists for
  the **standalone tunnel** card — point it at your own PLC/RTU (anything the chosen
  Gateway can reach) and demo the same brokered-access story against a real protocol
  stack. `OT_SIMS` at bake time picks which sims the image carries (default: all four).
  Siemens was in the same excluded bucket until python-snap7 3.0 reimplemented its S7
  server in pure Python; an image baked before that carries no `ot-s7` container.
- **A cell gets a tunnel per endpoint you tick** — Modbus is pre-checked, and each
  extra vendor becomes its own named jump item, so showing a Siemens PLC *and* a
  Rockwell PLC on one air-gapped host, each brokered separately, needs no manual
  wiring. To reach a protocol the image does not simulate (DNP3) or a different host,
  the **standalone tunnel** card still points anywhere the Gateway can reach.
- **The Kubernetes API is on that list but is not a fieldbus protocol**, and the form
  groups it apart for that reason: it is the cell's own KubeSolo cluster, the thing
  running the simulators. The same preset on the **standalone tunnel** card brokers a
  real plant host's KubeSolo — the install
  [`kubesolo/kubesolo-install.yml`](https://github.com/Weaverlab-xyz/vm-dashboard-community/tree/main/examples/playbooks/kubesolo)
  puts on an on-prem IPC — without opening anything else on it.
- **One protocol per tunnel jump.** A tunnel carries one `local;remote` port pair. A
  cell gets one PLC tunnel (chosen at deploy); to speak a second protocol to the same
  cell or host, create a standalone tunnel with a different **name** and (if both run
  at once) a distinct local port — two tunnels listening on the same local port can't
  be open simultaneously on one rep machine.
- **Clients that follow redirects/back-connections** (some EtherNet/IP and OPC UA
  stacks re-connect to the address the server advertises) must be pointed at
  `127.0.0.1` explicitly; the tunnel forwards only the brokered port.
- The tunnel is generic TCP (`tunnel_type="tcp"`): no credential injection happens on
  the wire — the protocols above are unauthenticated-by-design in most PLCs, which is
  itself a talking point: the *network path* is the control, and PRA is the only way
  in.

## Lifecycle

- **Destroy** (OT tab button → the cloud's normal delete endpoint:
  `DELETE /api/gcp/instances/{name}` / `DELETE /api/aws/instances/{instance-id}` /
  `DELETE /api/azure/vms/{name}`) and the **auto-delete timer** both run that cloud's
  same extended destroy (`gce_destroy` / `ec2_destroy` / `azure_destroy`): remove the
  Web Jump and **every** protocol tunnel from their stored Terraform state, unlink
  the SyncedAccounts
  pair and off-board the PRA-checkout mirror, destroy the PRA Vault checkout account,
  then the Shell Jump, Password Safe and Entitle deregistrations, then the instance —
  and release the shared gateway reference last. There is no separate OT teardown
  path to forget, on any cloud.
- **Clear** (`DELETE /api/ot/cell/{vm_job_id}`) is the exit for a cell whose VM
  deploy *failed*, which Destroy cannot see — see
  [Clearing a cell whose VM never deployed](#clearing-a-cell-whose-vm-never-deployed).
  It destroys no VM; it refuses if either the cell **or its DMZ broker** is still there.
  The broker matters because it deploys *first*, so the common failure is "broker up,
  cell failed" — clearing only the cell would retire the card while a VM carrying a
  working Entitle agent kept running and kept billing. Clear does destroy one thing: the
  plant's **agent token**, which is minted before either VM exists and which the normal
  destroy runner only ever collects for a deploy that *completed*.
- **Expiry**: the child is a normal deploy row for its cloud, so the cell participates
  in the auto-delete timer with no extra configuration (see
  [auto-delete-timer](../../auto-delete-timer.md)). A cell with a broker gives the
  broker **its own expiry**, not the policy default: on separate clocks the two diverge
  the moment anyone extends one, and whichever reaped first would leave the other
  useless — an agent brokering access to a plant that is gone, or a cell whose Entitle
  grants quietly stop working.
- **Air-gap**: keep the on-demand egress flags **off** for the cell's cloud
  (`gcp_vm_nat_enabled` / `aws_nat_instance_enabled`; Azure VMs have no dashboard
  NAT toggle). Turning one on gives cell subnets egress and silently deflates the
  "no path out of the plant" story. On GCP,
  [Purdue-zone firewalling](#purdue-zone-firewalling) removes that coupling
  entirely — the cell's own egress deny outranks the NAT allow. One deliberate AWS exception:
  `aws_ssm_endpoints_enabled` adds **interface endpoints inside the VPC** for the
  Password Safe SSM onboarding — private AWS API access, not internet egress, so it
  doesn't break the story.

## Standalone OT protocol tunnels

Each cloud's OT tab also creates a **standalone tunnel** to *any* host the gateway can
reach — for demoing against real lab gear without deploying a cell. It is the same
generic-TCP protocol tunnel the k8s API tunnel uses, with the OT port presets and the
same Jump Group / Gateway pickers as the cell form. State lives in config keys
(`ot_tunnel_*`, each recording which cloud's shared gateway it rides), and live
tunnels hold a reference in **that cloud's** gateway idle-teardown count, so an
unrelated decommission cannot reap the gateway mid-session. Connection details and
per-protocol clients: [Using the protocol tunnels](#using-the-protocol-tunnels).

## FUXA wiring

The bake **pre-seeds the project** with a `ModbusTCP` device named `PLC` (address
`127.0.0.1`, port `502`) and four tags for holding registers 0–3, by asking the running
FUXA for its own project, adding the device and posting it back. So inside the recorded
Web Jump session the remaining step is just to **drop the tags on a view** — a FUXA view
is SVG and its item format is the most version-coupled part of the project, so the bake
does not generate one.

The address is the loopback because every workload runs with `hostNetwork`: FUXA and the
simulators share the node's network namespace, so they reach each other exactly as a
client through a tunnel does. (An image baked with `OT_RUNTIME=docker` uses the compose
service names — `plc`, `s7`, `enip`, `opcua` — instead.)

If your bake log says `WARNING: FUXA project NOT seeded`, the pinned FUXA rejected the
shape and the image is exactly as it was before seeding existed: add the connection by
hand, once per cell (~1 minute) — FUXA → Connections → **ModbusTCP** at `plc`:`502`,
then tags for holding registers 0–3. Either way the project persists on the VM.

Only the Modbus device is seeded, but FUXA also speaks the other three, and every
simulator answers on the same address it does — so adding a second vendor to the same
view is a one-minute job:

| Device type | Address | Port | Tags |
|---|---|---|---|
| S7 | `127.0.0.1` | 102 | DB1 words at offsets 0, 2, 4, 6 |
| EthernetIP | `127.0.0.1` | 44818 | `COUNTER`, `TEMPERATURE`, `FLOW`, `RUNNING` |
| OPC UA | `opc.tcp://127.0.0.1:4840/freeopcua/server/` | 4840 | `Plant/Counter`, `…/Temperature`, `…/Flow`, `…/Running` |

A single recorded HMI session showing a **Siemens and a Rockwell** device side by side,
on a host with no route to the internet, is the demo this cell exists for.

## E2E verification checklist

Written for GCP (the first cloud); on AWS/Azure substitute that cloud's deploy child
(`ec2_deploy` / `azure_deploy`), destroy endpoint, functional account
(`passwordsafe_vm_functional_account_aws` / `_azure`), managed-system address shape
(`{instance-id}:{region}` / `tenantId/subscriptionId/resourceGroup/vmName`) and
gateway size key (see [the sizing guard](#the-gateway-sizing-guard)). On AWS also
verify the cell landed in the **private** subnet (no public IP on the child job) and,
for Password Safe over SSM, that `aws_ssm_endpoints_enabled` is on or the subnet
otherwise reaches the SSM control plane. **As of 2026-08-25 the AWS and Azure slices
have not been E2E-verified live** — this checklist is the script for that pass.

The `ot-sim` image, the FUXA seed, the extra protocol sims and the Purdue rules have
**not been exercised on a live bake or cell** either: they are covered by unit and
structural tests, and the sims themselves were run and read against real clients
outside the image. The **KubeSolo runtime is the newest of these** — the Docker purge,
the offline install, the image side-load and the identity reset are held by
`tests/test_ot_kubesolo.py` and by the bake's own smoke test, and nothing more. Steps
2a, 4a and 10 below are their first live pass; `OT_RUNTIME=docker` is the fallback if
the bake fails on a platform the script has not met.

1. Settings: `pra_enabled` on; `bt_api_host` / `bt_client_id` / `bt_client_secret` /
   `bt_jump_group_name` / `bt_jumpoint_name` set; `gcp_jumpoint_machine_type=e2-medium`
   (delete an existing gateway VM so it recreates); `gcp_vm_nat_enabled` **off**;
   Password Safe registration on with the GCP functional account.
2. Bake `ot-sim`; it appears in the OT tab's image picker. Watch the bake log for the
   Docker purge, `installing KubeSolo …`, every image importing into containerd, the
   five ports answering the smoke test, and either `FUXA project seeded` or the
   `NOT seeded` warning.
   - **2a.** On the deployed cell (Shell Jump): `systemctl status ot-sim` is
     `active (exited)`, `kubectl -n ot-sim get pods` shows `ot-plc`, `ot-hmi`,
     `ot-opcua`, `ot-enip` and `ot-s7` **Running on the node's own IP** (`-o wide`),
     `docker` is absent, and the Web Jump opens FUXA on a project that already has the
     `PLC` connection and its four tags. `kubectl get nodes` names *this* cell, not the
     build VM, and `kubectl -n kube-system get pods` is healthy with no image pulls
     pending — that is the offline install doing its job.
3. Deploy a cell **with several protocols ticked** and the Jump Group + Gateway
   pickers set to the cell's region; the parent job completes; the child holds Shell
   Jump id + private IP; **one tunnel jump per ticked protocol** appears, each named
   `ot-<cell>-<protocol>`, all in the picked Jump Group (not the configured default),
   and each carrying an OT-cell comment rather than "k8s API tunnel". The cell shows
   **wired** only once every one of them exists.
4. PRA rep console: Shell Jump SSH works; Web Jump renders FUXA (recorded); a Modbus
   client (mbpoll / QModMaster) through the tunnel at `127.0.0.1:502` reads holding
   register 0 **incrementing every second**. Then deploy (or add a standalone tunnel
   for) each other vendor and confirm the same four values move:
   - Siemens — python-snap7 `db_read(1, 0, 8)` against `127.0.0.1:102`. The
     pure-Python server logs a COTP framing warning on some handshakes; the read is
     what matters, not that line;
   - Rockwell — `pylogix` `Read("Counter")` against `127.0.0.1:44818` (CIP tag
     names are case-sensitive);
   - OPC UA — UaExpert against `opc.tcp://127.0.0.1:4840`, browse `Objects/Plant`.

   `python scripts/ot/verify_tunnels.py` does all four in one pass and distinguishes
   "no listener" from "listener, no answer" from "answers but frozen" — see
   [OT protocol clients on Windows](ot-protocol-clients.md).
   - **4a. The cluster, through PRA.** With the cell deployed with **Kubernetes API
     (KubeSolo)** ticked: the jump item `ot-<cell>-kubesolo` exists, and with it
     started, `kubectl --kubeconfig <the cell's /var/lib/ot-sim/kubeconfig-via-tunnel.yaml>
     -n ot-sim get pods` answers from the rep machine — no certificate error, because
     that file carries the `tls-server-name` the cell wrote. Stop the jump and the same
     command fails to connect: the cluster has no other way in.
5. Password Safe: managed system `projectId/zone/instanceName` exists; the mirror
   system `<cell>-pravault` exists with account `<cell>-adminuser`; the pair shows
   under `adminuser`'s **Synced Accounts**; rotate `adminuser` and watch the change
   propagate to the mirror.
6. PRA: the Vault account `<cell>-adminuser` exists, is **check-out-able** in the rep
   console / `/login`, and is **offered for injection** when starting the cell's Shell
   Jump; after the rotation in step 5, checkout returns the NEW credential and SSH with
   it succeeds.
6a. **The plant's own agent.** Deploy a cell with **Register in Entitle** ticked
   and a broker image picked. The parent job shows: token minted → broker deployed →
   cell deployed → PRA wiring → both zones applied → agent install queued.
   The zones are readable from the cloud's own CLI — `gcloud compute firewall-rules
   list`, `aws ec2 describe-security-groups --group-names <cell>-ot-zone
   <broker>-dmz-zone`, or `az network nsg rule list -g <rg> --nsg-name <cell>-ot-zone`
   — and on AWS confirm the cell's *instance* carries `<cell>-ot-zone` and nothing
   else, since a zone beside a permissive group restricts nothing. On Azure the row
   to look for is the **outbound Deny**: without it the cell reaches the internet
   despite having no public IP.
   `gcloud compute firewall-rules list` shows the two zones from
   [Who brokers identity in the plant](#who-brokers-identity-in-the-plant). From the
   broker (Shell Jump): `kubectl -n entitle get pods` is Running and the install job's
   probe passed. From the cell: every `curl` fails. Then request access in Entitle and
   confirm the ephemeral account appears **on the cell** — that is the agent reaching it
   from inside the plant — and that SSH with it through the Shell Jump works and expires
   on its own. Destroy: both VMs, both rule sets, the agent token and the integration go.
7. Negative test: set the gateway to `e2-micro` → a new cell fails fast with the sizing
   remedy in the job error (not a mid-session OOM). With a Gateway override picked, the
   same deploy proceeds (guard skipped, noted in progress).
8. **Per-tunnel Re-wire**: delete one of the cell's tunnel jumps in PRA, press
   **Re-wire**, and confirm only that protocol is recreated (the others are untouched
   and not duplicated). Re-wire again with nothing missing and confirm it completes
   without provisioning anything.
9. Destroy the cell → **every** tunnel jump gone from PRA (check each protocol, not
   just the first), the Web Jump gone, the Vault checkout account gone, the mirror + PS
   system off-boarded, VM deleted, gateway reaped only once nothing else references it.
10. **Back-compat**: destroy a cell deployed *before* this change — its metadata has the
    singular `ot_tunnel_*` keys and no `ot_tunnels` list — and confirm its tunnel is
    still torn down. (A cell in that state also Re-wires cleanly: the existing tunnel is
    adopted into the list rather than provisioned a second time.)
11. Expiry: with the timer enabled, `expires_at` is stamped on the child row; a reaped
    cell cleans up identically to a destroyed one — including every tunnel.
12. **Purdue rules (GCP, optional)**: with `ot_purdue_firewall_enabled` on, deploy a
    cell (or **Re-wire** an existing one) and check `gcloud compute firewall-rules list`
    shows its three `<cell>-ot-*` rules. Then: Shell Jump, Web Jump and the tunnel all
    still work; `curl` to the internet from the cell fails **even with
    `gcp_vm_nat_enabled` on**; and a destroy removes all three rules.
13. **Standalone tunnels still work alongside**: the cell's own tunnels cover the
    protocols it was deployed with, so use a standalone tunnel for what it was not —
    e.g. DNP3 to real gear, or a second local port for a protocol already brokered.
    Two tunnels cannot listen on the same local port on one rep machine at once.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Cell deploy fails immediately with a sizing message | Working as designed — the gateway is <2 GB; follow the remedy in the error |
| Web Jump session dies with "internal timeout starting session" | Gateway too small (if the guard was bypassed by resizing after deploy), or the gateway host is down — check the Gateways tab against reality |
| Tunnel connects but the Modbus client times out | The cell VM isn't running the stack — Shell Jump in and check `systemctl status ot-sim` / `kubectl -n ot-sim get pods` |
| Everything times out for the first few minutes of a fresh cell | Working as designed — first boot mints the cluster's CA and node identity and loads the baked images, with no registry to shortcut it. `systemctl status ot-sim` shows the progress and reaches `active (exited)` |
| Azure: Web Jump/Shell Jump work but the tunnel never establishes | The cell's Gateway resolves to an **ACI** gateway — ACI is serverless and cannot do protocol tunneling. Keep `azure_vm_jumpoint_mode=shared` and point `azure_jumpoint_name` / the form's Gateway picker at the shared **VM** gateway |
| Registers read but never change | The sim restarted into a crash loop — `kubectl -n ot-sim logs deploy/ot-plc` (S7: `ot-s7`; OPC UA: `ot-opcua`; EtherNet/IP: `ot-enip`) |
| An S7 / OPC UA / EtherNet-IP tunnel connects but nothing answers | That sim was not baked — `OT_SIMS` at bake time selects them (default is all four), and an image baked before Siemens was added has no `ot-s7`. `kubectl -n ot-sim get pods` on the cell shows which are running |
| `kubectl` through the tunnel fails on a certificate error | The kubeconfig is not the one the cell wrote: the API certificate names the cell, not your loopback. Use `/var/lib/ot-sim/kubeconfig-via-tunnel.yaml`, which carries `tls-server-name` |
| A pod is `ErrImageNeverPull` | Its image is not in the node's containerd. The cell pulls nothing by design — re-run `/opt/ot-sim/kubesolo/apply.sh`, which re-imports from `/var/lib/ot-sim/images` |
| The KubeSolo tunnel connects but nothing answers on :6443 | The image was baked with `OT_RUNTIME=docker`, so the cell runs no cluster. A new deploy refuses this — set *Image runtime* to match what you baked — so a cell in this state predates that guard, or was deployed through the API with the wrong `runtime`. Rebake with the default runtime, or untick that entry and Re-wire |
| The deploy refuses, naming `routing` | The tenant is on `routing: v0`, whose agent pulls its image from `ghcr.io` / `gcr.io/datadoghq`. No narrow allow-list can name a CDN, so the agent could not start inside the plant. Ask BeyondTrust to migrate the tenant, or deploy the cell without Entitle |
| The deploy refuses, saying the token's region is not the one the hole was drawn for | `entitle_api_url` is a proxy or a bare host, so the region fell back to the default while the tenant is somewhere else. Set it to the regional URL and redeploy — otherwise the agent would dial a host the plant denies, with nothing saying why |
| Clear refuses, naming the DMZ broker | The broker outlived its failed cell — it deploys first, so this is the common shape. Destroy it from the cloud's VMs tab, then clear the cell |
| The Entitle grant approves but the vendor's login is refused | The agent cannot reach the cell on :22. On a cell with its own broker, check `<cell>-ot-ingress-agent` exists; on one without, that is the old shared-agent arrangement, which the Purdue zoning blocks by design — redeploy with Entitle ticked |
| The agent install job fails at "Prove the agent's network path" | Working as designed, and it names which leg failed: DNS, 443/8080, or the cell's :22. Pod SNAT, the DNS hole and the destination set are the three candidates, in that order |
| The agent was fine and now is not | The Entitle endpoint's addresses moved. They are pinned at wiring time unless `ot_entitle_egress_cidrs` is set — **Re-wire** re-resolves and replaces the rule |
| Deploying with Entitle refuses, naming a setting | Working as designed: the in-plant agent needs the Purdue zoning, a broker image, a destination set, an in-cloud runner and an 8 GB broker. The error names the one that is missing |
| The bake fails saying Docker is installed | KubeSolo's installer refuses a host with Docker on it and the purge did not complete — read the lines above it in the bake log; `OT_RUNTIME=docker` skips the whole step |
| FUXA opens with no PLC connection | The bake's project seed was skipped — search the bake log for `FUXA project NOT seeded`, and wire it by hand (`provisioners/ot/README.md`) |
| AWS cell deploy fails immediately naming the subnet | Working as designed — that subnet auto-assigns public IPs; use the private sandbox subnet or clear `ot_aws_require_private_subnet` |
| GCP cell unreachable right after enabling Purdue firewalling | The Gateway you are brokering through is not the managed one, so it does not carry the `bt-jumpoint` tag the ingress allow matches. Delete the cell's `*-ot-ingress-deny` rule, then either use the managed Gateway or add that tag to yours |
| `ot-sim` bake fails at image pull | Docker Hub rate limit or a stale pin — see `provisioners/ot/README.md` (re-pin via `OT_FUXA_IMAGE`) |
| AWS bake hangs at "Waiting for SSH" | The build resolved a Debian AMI but the SSH username isn't `admin` — use the Debian 12 preset (it sets both), or fix the username field |
| AWS cell got a public IP | The chosen subnet auto-assigns them — EC2 has no per-instance switch; redeploy into the private sandbox subnet |
| AWS Password Safe onboarding fails / never rotates | The private subnet can't reach the SSM control plane — turn on `aws_ssm_endpoints_enabled` (interface endpoints, not internet egress) |
| Parent job failed after "VM deployed" | Wiring failure — the error names the failed piece; fix and **Re-wire** |
| Jump items landed in the wrong Jump Group / Gateway | The configured defaults were set up for another region — use the form's PRA placement pickers; existing cells: destroy + redeploy (jump items don't move) |
| Cell shows "wiring incomplete" but Web Jump + tunnel work | The PRA-checkout pair is missing (pre-feature cell, or it failed) — **Re-wire** creates only the missing pieces |
| PRA checkout returns a password that doesn't log in | The pair never converged — check `adminuser`'s Synced Accounts in Password Safe and its last change result; a rotation on the parent re-syncs both |
| Second protocol needed to the same host | One port pair per tunnel jump — create a standalone tunnel with another name (see [Using the protocol tunnels](#using-the-protocol-tunnels)) |

## Quick preview without a bake

`examples/compose/ot-sim.yml` runs FUXA + a *static* Modbus server through the
Containers page (ECS/ACI/GCE-COS). It is **unwired** — no PRA, no Password Safe, needs
egress at start, and register values don't change. Use it for a quick look at the
containers; use the cell for the actual demo.
