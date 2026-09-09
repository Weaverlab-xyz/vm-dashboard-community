# Virtual Desktops

> **Audience:** operator · **Profile:** `both` · **Read this when:** you need a pool of private desktop VMs that reps reach through the PRA Gateway instead of over the open internet.

> **Preview.** All three clouds provision and broker seats, but the feature has not been
> run end to end on AWS or GCP against a live PRA appliance. Off by default; turn it on
> with the **Virtual Desktops** preview toggle in Settings.

A **desktop pool** is *N* private VMs built from one desktop image, tagged so the pool is
recoverable from the cloud itself rather than only from this database. Seats are created,
scaled and deleted as a unit. Each seat is registered as a PRA Jump Item, so a rep reaches
a desktop from the representative console and no seat needs a public IP.

## What each cloud can do

| | Azure | AWS | GCP |
|---|---|---|---|
| Provisions VMs | yes | yes | yes |
| Windows seats | **yes** | no | no |
| Linux seats | yes | yes | yes |
| Jump item | Remote RDP (Windows) / Shell Jump (Linux) | Shell Jump | Shell Jump |
| Credential injection | Windows only | none | none |
| Pool tag key | `dashboard:desktop_pool` | `dashboard:desktop_pool` | `dashboard_desktop_pool` |

### Why AWS and GCP are Linux-only

Windows credentials work differently on each cloud, and only one of the three is wired.
Azure generates a password and vaults it before the VM exists, so the credential survives
a failure anywhere after that point. EC2 instead returns **password data encrypted to the
launch key pair**, decrypted client-side. GCE delivers a password through **`windows-keys`
instance metadata** and an RSA exchange. Those are two more mechanisms, each with its own
storage question, so an AWS or GCP Windows pool is **refused at create time with that
reason** rather than provisioned into seats nobody can sign into.

### Why Linux seats inject no credential

A Linux seat authenticates with an SSH key. The dashboard never holds the private half,
and the PRA provider wrapper here has no SSH-key vault resource — the only vault resource
wired is a username/password account, which a key-based login cannot use. So a Linux seat's
Shell Jump registers **without** credential injection and the rep supplies the key in PRA.
This is a real difference from the Windows path, not an oversight, and the pool form and
the session dialog both say so.

## Creating a pool

**Desktops → New pool.** Pick a cloud, name the pool and choose a seat count, then fill in
that cloud's block. Create stays disabled until everything that cloud's backend requires is
present — a pool that would 400 cannot be submitted.

- **Azure** — image (gallery or managed, or a pasted ARM id), location, VM size, subnet,
  optional NSG and resource group. Guest OS is auto-set from the selected image and can be
  overridden for a pasted id. Windows seats each get a generated admin password stored in
  the secrets backend; retrieve one per VM from **Azure → VMs → Password**.
- **AWS** — region, AMI (Windows AMIs are omitted), instance type, subnet, and at least one
  security group. EC2 falls back to the VPC default security group if none is sent, but the
  form asks for one explicitly so the choice is visible.
- **GCP** — image (custom first, then public; Windows images omitted), zone, machine type,
  subnetwork, boot disk size, and whether to give each seat an external IP (off by default).

Changing the AWS region or the GCP zone clears the selections scoped to it and refetches.
That is deliberate: an AMI id from another region fails with `InvalidAMIID.NotFound`, which
names no region at all, and a sandbox uses the **same subnetwork name in every GCP region**,
so a stale self-link looks correct and fails at launch on a scope mismatch.

### The SSH key

The form does not ask for one on AWS or GCP. The key is resolved server-side from that
cloud's configured secret — Secrets Manager on AWS, Secret Manager on GCP — exactly as the
single-VM deploy pages do. (`/api/gcp/secrets/ssh-key` returns only the first 80 characters
of the key, so a form field there could not show a real one anyway.) Azure's picker can show
its key, so the Azure block still posts one. If no key is configured, the pool form says so
and blocks Create rather than producing seats nobody can reach.

## Settings

**Settings → Virtual Desktops** holds the per-cloud defaults. Everything here is optional
except Azure's subnet; a blank field falls back down a chain.

| Setting | Blank falls back to |
|---|---|
| `azure_desktops_subnet_id` | this region's `desktops_subnet_id`. **No further fallback** |
| `azure_desktops_vm_size` | the pool form's own default |
| `azure_desktops_vault_account_group_id` | PRA's Default account group |
| `aws_desktops_subnet_id` | this region's `desktops_subnet_id`, then `aws_default_subnet_id` |
| `aws_desktops_instance_type` | the pool form's own default |
| `gcp_desktops_subnetwork` | this region's `desktops_subnetwork`, then `gcp_subnetwork` |
| `gcp_desktops_machine_type` | the pool form's own default |

Azure is the one cloud with no final fallback to its ordinary VM subnet, and that is on
purpose: an Azure sandbox's `aci-subnet` is **delegated** to Container Instances and cannot
host a VM NIC, so inheriting "the VM subnet" there could silently select a subnet that
cannot deploy a desktop. Every AWS subnet and every GCP subnetwork can host an instance, so
inheriting is a default an operator can live with rather than a guess.

Per-region overrides for all three clouds live in **Settings → Multi-region**.

## Brokering and the Gateway

Before any seat registers, the pool's provision job warms the shared PRA Gateway host for
**that seat's cloud and region** — register first and the jump items exist but read
*Unavailable*, with no Gateway to broker them. Warming is idempotent and best-effort: it
usually reuses an existing host and takes about a minute the first time.

Two consequences worth knowing before turning this on:

- Creating a pool on a PRA-configured install **can create a Gateway host that did not
  exist before**, with the cost and the network dependency that implies.
- **The Gateway must be able to reach the seat's subnet.** It lands in its own cloud and
  region; a seat elsewhere needs the networks peered. A jump item that registers but cannot
  reach the seat's private IP looks identical to success on this page.

Registration is best-effort by design. A running seat with no jump item is debuggable; a
seat marked failed because PRA was briefly unreachable is a VM nobody cleans up. When a
seat shows no jump item, the reason is on the pool's job.

## Scaling and deleting

**Scale** grows or shrinks a pool to a seat count. Growing reuses the spec stored on the
pool's create-time job, so a pool created before that job was recorded cannot scale up and
says so. Shrinking tears down the newest seats.

**Delete** terminates every backing VM, removes each seat's PRA jump item from its stored
Terraform state, drops the rows, and then reaps the shared Gateway if nothing else still
references it. Terminate runs before the row is dropped — a row dropped first is a VM
nobody can find again. If a terminate fails the rows still drop, but the job ends **failed**
with the reason, because a green job whose failure only shows on a seat row is a job nobody
reads.

Seats are exempt from the auto-delete timer: a pool is inventory, not a scratch deploy.

## Related

- [Cloud VMs](cloud-vms.md) — the single-VM deploy paths these seat backends reuse.
- [Privileged Remote Access](integrations/privileged-remote-access.md) — Jump Groups, Jump Items and credential injection.
- [Gateways](integrations/gateways.md) — the shared Gateway host and how it is reference-counted.
- [Cloud sandbox](CLOUD_SANDBOX.md) — the desktops network segment the Azure default points at.
