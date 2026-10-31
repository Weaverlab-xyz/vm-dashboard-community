# Running POVs on a public cloud

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are running POVs on AWS, Azure, GCP or OCI rather than on a lab platform.

Part of [A POV Instance](README.md). Choosing clouds, what gets created, what it costs, and the per-provider differences.

## Running POVs on a public cloud

Skytap is not the only place a POV can run. A POV instance may also select any of the
public clouds AWS, Azure, GCP and OCI — one or several — and build POVs on them through
the same pages, the same blueprints, the same wire-up and the same auto-delete timer.

Turn them on in **Settings → Integrations → POV cloud provider**: tick each provider,
fill in its credentials, save, then **Test connection**, which tests every ticked cloud.
The stored setting is `pov_cloud_platform`, a comma-separated list such as `aws,azure`,
or blank for Skytap only. A value from before the list (`aws`) still means that one cloud.
The POV page's **Build** tab platform selector gains each ticked cloud alongside Skytap. It only
chooses which platform's blueprints a new POV is built from; **Mine** and **Past** list every
platform, show a **Platform** column, and have their own **All platforms** filter.

### Choosing clouds

The selection is enforced in one place — `lab_platforms.selectable_platforms()`, which
the create form renders from and the provision endpoint refuses against. Each cloud keeps
its own credentials, quota and bill, so selecting two is two independent integrations,
not one shared one. The cloud-specific pages take the cloud explicitly: **POV templates**
and its **Images** tab follow the platform toggle, and **POV Cloud** (`/pov/cloud`) shows
one cloud at a time with a switcher. A new cloud template can only be saved for a selected
cloud, since no POV could be built from one for an unticked cloud; templates already saved
for a cloud you untick stay listed and editable.

Unticking a cloud does **not** disturb POVs you already built there. Reads, power and
teardown never ask whether a platform is still selected — only *creating a new
environment* does — so those POVs stay visible, suspendable and destroyable until you are
done with them.

### Selecting a cloud does not open the cloud consoles

`/aws`, `/azure`, `/gcp`, `/oci`, `/images` and their API routers stay unavailable on a POV
instance, whatever credentials it holds. That is not an oversight to work around: those
deploys resolve the **global** BeyondTrust tenant singletons, so a VM built there would
onboard into the estate tenant rather than into this POV's — silently, because both paths
"work". It is the same tenancy argument the whole estate/POV split rests on.

Everything a POV builds goes through the POV pages, which resolve tenants from the
registry. That is also what keeps every cloud resource inside the auto-delete timer's
reach.

The images a POV boots are built there too: **POV → Templates → Images** builds the two
[standard images](public-cloud/images.md) through the same Packer engine `/images` uses,
without opening that page or its routers.

### What a cloud template is

Skytap hands you a template as a first-class object: one call against a template id and N
VMs exist, powered and networked. No public cloud has that call. So on a cloud **the
dashboard holds the template** — a named list of VMs plus the private network they sit on,
authored at **POV → Templates**.

| Field | What it does |
|---|---|
| Name | Lowercase slug. Shows in the create form and in job output |
| Region | Blank uses the provider's configured default |
| Private network | The CIDR this POV's own network gets. Blank uses `10.20.0.0/16` |
| VMs | Name, role, OS family, image, instance type |

Exactly one VM may carry the **control node** role. It is where the dashboard agent, the
Gateway and the Resource Broker all land; two of them would mean two agents enrolled for
one POV, each holding half the wire-up. The Role dropdown shows it as **control node**; in
the API and the template JSON the value is still `broker` (`"role": "broker"`), and the
resolved VM comes back as `broker_vm_name`.

**The Role dropdown decides which VM is the control node, not the VM's name.** A VM named
`broker` whose role is **target** is a target, and the template list then says **no
control node** — its POV would have no dashboard agent. Set the role on whichever VM
should carry the agent; its name can be anything.

A VM names either a **catalog image** (a row in the image registry, whose per-cloud id is
resolved from its promotions) or a **literal image id**. Catalog images are resolved when
the POV is built, not when the template is saved — so you can write a template before its
image has been promoted to this cloud, and a re-promote is picked up without editing
anything.

Nothing in the template path talks to a cloud. Whether an AMI exists, whether the account
has quota for the instance type, whether the region is enabled — those are answered by the
provision job. Everything that *can* be checked without credentials is checked when you
save, because the alternative is a template that stores cleanly and fails eleven minutes
into a build with half a network already made.

**A template does not bake its own image.** Skytap's instantiate → change → bake would
mean one machine image per template, which is slow, region-locked and a standing storage
bill for every template anyone saves. Instead a template names one of the two
[standard images](public-cloud/images.md) — Standard Linux (Ubuntu 24.04) and Standard
Windows (Server 2022), built once per region on **POV → Templates → Images** — and pick
them from the image column. The column also takes a literal image id for anything else.

### What gets created, and what it costs

One POV environment is one private network and its VMs:

- a **VPC**, its own per POV, with a single subnet;
- an **internet gateway** — and no NAT gateway. A NAT is roughly thirty dollars a month
  standing before a byte moves, and a POV runs for weeks on your own bill. Instances take a
  public address for egress instead;
- a **security group** that allows the environment to talk to itself and accepts **nothing
  inbound from outside it**. Every component the dashboard installs dials out: the agent
  polls, the Gateway reaches the PRA appliance, the Resource Broker reaches Password Safe.
  There is no SSH-from-the-internet rule to forget to remove;
- the VMs the template names, each with a gp3 root volume and IMDSv2 required.

Every one of them is tagged `povEnvironment=<environment id>` and
`povManagedBy=vm-dashboard`, in the same API call that creates it.

**Suspending a cloud POV does not stop the bill the way suspending a Skytap environment
does.** Stopping an instance halts its compute charge and nothing else: the root volume,
the public address and the network keep billing for the whole evaluation. Budget for that,
and reap a POV when it is finished rather than leaving it suspended indefinitely — which
is what the auto-delete timer is for.

### Pages

Each cloud's specifics, and the cost controls, have a page:

| Page | Read it when |
|---|---|
| [Keyless AWS for POVs](public-cloud/keyless-aws.md) | you want POVs on AWS without a stored access key, through the dashboard's SPIFFE identity |
| [POVs on Azure](public-cloud/azure.md) | you are running POVs on Azure and need to know where it differs from AWS |
| [Keyless Azure for POVs](public-cloud/keyless-azure.md) | you want POVs on Azure without a stored client secret |
| [POVs on GCP](public-cloud/gcp.md) | you are running POVs on GCP and need to know its two API constraints |
| [Keyless GCP for POVs](public-cloud/keyless-gcp.md) | you want POVs on GCP without a stored key, with write access granted through Entitle |
| [POVs on OCI](public-cloud/oci.md) | you are running POVs on OCI and need to know how compartments map to environments |
| [The POV suspend schedule](public-cloud/suspend-schedule.md) | you want a POV's cloud resources off outside working hours |
| [Idle suspend on AWS](public-cloud/idle-suspend.md) | you want an AWS POV suspended when nobody has used it for a while |
| [The POV control node on a cloud](public-cloud/control-node.md) | you need to know what the control node VM does in a cloud POV, and how to reach it |
| [Standard POV images](public-cloud/images.md) | you need the Standard Linux or Standard Windows image a cloud template boots |

### The environment id is derived from the POV name

A Skytap environment has an id the platform mints. A cloud environment does not exist as an
object at all, so the dashboard chooses one: `povenv-<pov name>`, which is also the tag
every resource carries. POV names are already unique among live POVs, so the id is too.

That is worth knowing because it is what makes a **partial failure safe**. Creating a cloud
environment is many API calls, not one, and a build can fail at VM three of five. The id is
written to the POV row *before the first call*, so Destroy and the reaper both find
everything that did get made. Tear the failed POV down from the POV page and build again.

### The spend cap

The auto-delete timer answers *how long may this POV live?* This answers the question an
operator running POVs on their own cloud account actually loses sleep over: **how much may
it cost?**

A clock is a poor proxy for money. The same fortnight is twenty dollars or two thousand
depending on what the template asked for, and on a cloud the difference only becomes
visible on an invoice weeks later.

Set a cap per POV from the **Spend** column, or carry one on a blueprint so every POV of a
kind starts with it. `pov_spend_cap_default_usd` stamps one on new POVs; **0 is the
default**, so turning nothing on changes nothing.

#### It is accrued, not billed — and that is the point

Every reconcile pass adds `rate now × time since the last pass` to a running total on the
row, using list prices and the VM list that pass already fetched.

Reading a real bill instead would be worse in three ways:

- **A bill lags a day.** Cost Explorer, Azure Cost Management and a GCP BigQuery export all
  report yesterday. A cap driven off one would *report* a runaway rather than stop it.
  Accrual reacts within one sweep.
- **A bill costs money to read.** Cost Explorer bills per request and is itself untaggable
  — that line has reached 45% of an account in this project's own history.
- **Accrual is the same everywhere.** It is arithmetic over what the dashboard already
  knows, so it needs no billing export, no new API and no new permission.

What you give up is accuracy. **It is a list-price estimate**: no Savings Plans,
reservations, credits, free tier, data transfer or snapshots. It errs high, which is the
safe direction for a cap, and everything on screen says so.

**Storage accrues while the POV is suspended.** That is deliberate and it is what makes the
cap honest — a POV left asleep for a month still pays for its disks, and a cap that counted
only running compute would never notice.

#### What happens at the cap

| `pov_spend_cap_action` | At the warning threshold | At the cap |
|---|---|---|
| `warn` *(default)* | One warning in the sweep's job log | One warning; nothing is suspended |
| `suspend` | One warning | Enqueues the same `pov_env_power` job the Suspend button does |

**It suspends; it never destroys.** That is what lets this feature exist without the
auto-delete timer's two arming clocks and dry-run mode — the worst outcome is a POV
somebody starts again. The default is `warn` regardless, because the figure is an estimate
and one that suspended a live customer demo on its first outing would be the last time
anybody trusted it.

Both the warning and the action fire **once**. Raising the cap clears both latches, so
"give it another fifty dollars" works. The accrued total is deliberately *not* reset — it
is what the POV has cost so far, and zeroing it on every edit would quietly turn the cap
into "another $X from now".

#### Where it works

A cap needs a price source, and each cloud needs its own.

| Cloud | Price source | Needs |
|---|---|---|
| **AWS** | Pricing API | `pricing:GetProducts` — an EC2-scoped key does **not** have it. Without it nothing accrues and the page names the missing permission |
| **Azure** | Retail Prices API | Nothing. It is public and unauthenticated, so a cap works the moment the provider is selected |
| **GCP** | Cloud Billing Catalog | `cloudbilling.googleapis.com` enabled on the project. No extra IAM role — the catalogue is a public price list |
| **OCI** | Public price list | Nothing. One unauthenticated GET returns the whole list |

**Every cloud a POV can be built on can now be priced**, so a spend cap works wherever a
POV works. Skytap stays unpriced: it bills the lab rather than the VM, and its own idle
timer is the lever there.

Skytap has no per-VM figure at all — it bills the lab, and its own idle timer is the lever
there.

A cloud with no price source is never offered a cap. That is deliberate: one that accrued
a confident zero and never acted would be a worse promise than no cap at all.

**OCI prices OCPUs and memory separately too**, like GCE — so a rate is
`ocpus × rate + memory_gb × rate`, with the counts coming from the same `parse_shape` that
sized the instance. Two OCI-specific notes: a **fixed (non-Flex) shape has no estimate**,
because the list prices those per shape rather than per OCPU; and a **block volume is two
charges**, capacity plus performance units, so pricing only the first would understate
every POV's storage by two thirds. The sum comes to the $0.0425 per GB-month Oracle
publishes for Balanced, which is what a POV boot volume defaults to.

**GCE does not price a machine type.** It prices vCPU and memory separately, so
`n2-standard-4` is four units of *N2 Instance Core* plus sixteen of *N2 Instance Ram*. The
estimate asks the Compute API what a shape contains, then the catalogue what a core and a
gigabyte cost in that region. Preemptible and committed-use SKUs are excluded.

Two consequences worth knowing on GCP. **A custom machine type has no estimate** — the
catalogue prices families, and `custom-4-16384` belongs to none, so the cap is unavailable
for a POV using one. And **a Windows licence is not counted**: unlike AWS and Azure, GCE
licences a premium OS image as a separate SKU keyed on the image rather than the shape, so
a Windows POV is under-estimated by its licence.

**Azure bills a managed disk by size tier, not per gigabyte.** A 30 GB and a 32 GB Standard
SSD are both an E4 and cost exactly the same; a 33 GB one is an E6 and costs roughly
double. The estimate rounds up into the tier Azure would actually charge, rather than
multiplying a per-GB figure that would be wrong in both directions. Spot and Low Priority
SKUs are excluded — they are cheaper, and understating is the one direction an estimate
behind a cap must not err.

### The cloud view, and orphans

**POV → Cloud** (`/pov/cloud`), linked from the POV page when a provider is selected. Its
own POV-owned page rather than re-opening `/aws`, and it shows without ever creating —
there is no deploy control on it and no endpoint behind one.

Three things:

**Footprint.** Environments, VMs running out of VMs total, gigabytes of EBS, and the
instance shapes in use. Read from the same describe the VM lists come from, so it cannot
disagree with them. Stopped VMs and their disks are counted, because they are still
billing.

**Orphans — the reason the page exists.** `pov_reconcile` compares each POV row against
the cloud and can tell you a row's environment has gone. It cannot tell you the reverse:
that the cloud is holding an environment no live POV row remembers. That is the direction
cost leaks — a provision that died before its row was written, a POV destroyed from the
console leaving its network behind, a row deleted by hand. Every resource carries
`povManagedBy=vm-dashboard`, so the question is answerable.

A POV row that reached `destroyed` does not count as remembering one. If a teardown left
something behind, the row says gone and the cloud says otherwise — and that is exactly the
case somebody needs to be told about.

**The page will not delete an orphan.** A tag-scoped teardown driven from a read is how
the wrong environment gets destroyed; the Destroy button on a POV is the one path that
knows the order to tear things down in. The page tells you the tag to select on in the
cloud's own console.

**An estimate, when the account will give one.** On-demand **list price** from the AWS
Pricing API — not a bill. No Savings Plans, reservations, free tier, credits, data
transfer or snapshots. It needs `pricing:GetProducts`, which an EC2-scoped key does not
have; without it the page says so and shows the footprint alone. That is deliberate: a
hardcoded price table goes stale silently and reports a number somebody plans around.

The figure worth reading is **per month at the current power state**, which includes
storage. It is the answer to "can I leave this up for the evaluation?".

### What a cloud POV does not have

Read these off the platform's capability row rather than discovering them:

| Not available | Why, and what to use instead |
|---|---|
| **Share link** | No cloud has publish sets. The customer's front door is PRA, which makes PRA **required** for a cloud POV where it is optional on Skytap |
| **Idle suspend** | No cloud has a platform idle timer. The dashboard supplies a [scheduled suspend](public-cloud/suspend-schedule.md) on every cloud, and on **AWS** an [idle timer of its own](public-cloud/idle-suspend.md) as well |
| **Stored credentials** | **AWS, GCP and OCI.** None holds a guest login to read back, so set it yourself: the **Login** column on the POV's VMs tab takes a username and, on these clouds, a password. The password is stored encrypted, never shown again, and cleared at teardown. The Resource Broker, Entitle agent and guest-step runs are refused until the guest they target has both. Azure generates one — see above |
| **Published services** | No NAT-a-guest-port primitive, and none needed — access is PRA through this POV's own Gateway |

---
