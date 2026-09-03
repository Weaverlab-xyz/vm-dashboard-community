# Running POVs on a public cloud

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are running POVs on AWS, Azure, GCP or OCI rather than on a lab platform.

Part of [A POV Instance](README.md). One cloud at a time, what gets created, what it costs, and the per-provider differences.

## Running POVs on a public cloud

Skytap is not the only place a POV can run. A POV instance may also select **one** public
cloud, and build POVs on it through the same pages, the same blueprints, the same wire-up
and the same auto-delete timer. Today that cloud is AWS, Azure, GCP or OCI.

Turn it on in **Settings → Integrations → POV cloud provider**: pick the provider, paste
its credentials, save, then **Test connection**. The POV page's platform selector gains it
alongside Skytap.

### One cloud at a time

The limit is deliberate, and it is enforced in one place —
`lab_platforms.selectable_platforms()`, which the create form renders from and the
provision endpoint refuses against. A POV instance is meant to be narrow: one cloud's
credentials to protect, one account's quota to watch, one bill to explain.

Switching provider does **not** disturb POVs you already built. Reads, power and teardown
never ask whether a platform is still selected — only *creating a new environment* does —
so the POVs on your old provider stay visible, suspendable and destroyable until you are
done with them.

### Selecting a cloud does not open the cloud consoles

`/aws`, `/azure`, `/gcp`, `/oci`, `/images` and their API routers stay unavailable on a POV
instance, whatever credentials it holds. That is not an oversight to work around: those
deploys resolve the **global** BeyondTrust tenant singletons, so a VM built there would
onboard into the demo tenant rather than into this POV's — silently, because both paths
"work". It is the same tenancy argument the whole demo/POV split rests on.

Everything a POV builds goes through the POV pages, which resolve tenants from the
registry. That is also what keeps every cloud resource inside the auto-delete timer's
reach.

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

Exactly one VM may carry the **broker** role. It is where the dashboard agent, the Gateway
and the Resource Broker all land; two of them would mean two agents enrolled for one POV,
each holding half the wire-up.

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

**Baking a real machine image is deliberately not offered.** It would be the faithful
analogue of Skytap's instantiate → change → bake, and it is slow, region-locked,
cloud-specific, and a standing storage bill for every template anyone ever saves. Build
your images with the Packer and image-promote tooling, and let the template reference
them.

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

### The environment id is derived from the POV name

A Skytap environment has an id the platform mints. A cloud environment does not exist as an
object at all, so the dashboard chooses one: `povenv-<pov name>`, which is also the tag
every resource carries. POV names are already unique among live POVs, so the id is too.

That is worth knowing because it is what makes a **partial failure safe**. Creating a cloud
environment is many API calls, not one, and a build can fail at VM three of five. The id is
written to the POV row *before the first call*, so Destroy and the reaper both find
everything that did get made. Tear the failed POV down from the POV page and build again.

### Azure, and where it differs from AWS

Azure is the second provider, and it maps onto this feature more naturally than AWS
because it has the primitive AWS lacks: **a resource group is an environment.** One per
POV, named `povenv-<name>`, and destroying the POV is a single `begin_delete` on the group
that takes the VMs, their managed disks, NICs, public addresses, the VNet and the NSG with
it, in ARM's own dependency order. The AWS driver unpicks six resource types by hand for
the same result.

Give the service principal **Contributor** on its own subscription, or on a scope where it
can create resource groups.

| | AWS | Azure |
|---|---|---|
| Environment | A tag on every resource | A resource group |
| Teardown | Six resource types, in order | One call |
| Listing | Per region | Subscription-wide, so the orphan sweep is complete |
| Platform login | None | **Generated per POV** — see below |
| Suspend | `StopInstances` | `begin_deallocate`, never power-off |
| Private address | Persists across stop/start | **Made static**, because it does not |

**Azure POVs have a platform login; AWS ones do not.** Not an inconsistency — Azure's
`os_profile` requires an admin account at VM creation, for Linux as well as Windows, so a
POV built there has one whether anybody wanted it or not. The dashboard generates it,
stores it encrypted alongside the POV's other secrets, and puts the same account on every
VM in the environment. That is why Azure's `stored_credentials` capability is True and
AWS's is False, and it means the Resource Broker install has the login it needs without
anybody pasting one.

**The two Azure-specific traps this driver exists to avoid**, both of which fail quietly:

- **Power-off is not suspend.** `begin_power_off` leaves a VM "Stopped" and still billing
  for its compute. Only `begin_deallocate` reaches "Stopped (deallocated)". A nightly
  schedule making that mistake would cost the full compute bill while the page reported
  every POV asleep.
- **Private addresses are allocated statically here**, unlike on the other three clouds. A
  deallocated Azure VM with a dynamic private address can return on a different one — and
  by then the wire-up has written the old address into a PRA jump item, a Password Safe
  managed system and an Entitle integration. Every scheduled suspend would silently
  invalidate all three.

The network security group carries **no custom rules**, deliberately: Azure's defaults
already allow the VNet to talk to itself, deny everything inbound from outside it, and
allow outbound. Restating them would be three more things to keep correct and no change in
behaviour.

Template images take either a marketplace URN (`publisher:offer:sku:version`) or the
resource id of a managed or gallery image. Anything else is refused by name — ARM's own
error for a malformed reference names neither the field nor the value.

### GCP, and its two API constraints

GCP sits between the other two. Like AWS it has no native environment object, so a
teardown unpicks resource types rather than deleting one group. Unlike either, two of
Compute Engine's own rules cannot be met by the shared code.

Give the service account **Compute Admin** on its project, with the Compute Engine API
enabled.

**A GCE label key must be lowercase.** The rule is `[a-z](../../[-_a-z0-9]*)?`, so
`povEnvironment` is refused outright — with an error naming the API field rather than the
tag. Each shared key is mapped once: `pov_environment`, `pov_managed_by`, `pov_role`, and
`managed-by` unchanged.

**Only some GCE resources carry labels at all.** An Instance and a Disk do; a Network, a
Subnetwork and a Firewall have no labels field. So a POV's network layer is selected by
**name** instead — `povenv-<name>-net`, `-subnet`, `-fw` — and the instances by label. That
also means a POV name has a shorter ceiling here than elsewhere: a GCE resource name is
capped at 63 characters, and an over-long one is refused at build with the real number
rather than truncated, because truncation is how two POVs collide.

**The zone is resolved from the region, never assembled.** `us-east1` and `europe-west1`
have no `-a` zone, and GCE reports a nonexistent zone as
`403 Permission denied on 'locations/us-east1-a' (or it may not exist)` — which reads as a
credentials problem. This dashboard has paid for that mistake once already; the region's
own zone list is one call and is always right. It also guarantees the subnetwork and the
instances share a region, which GCE otherwise rejects at insert time with "Scope of the
specified subnetwork doesn't match the scope of the instance", naming neither.

**GCP is the only cloud where the POV records a project.** An AWS account and an Azure
subscription are instance-wide settings; a GCP project is a boundary an environment is
built *inside*. So `projects` is True, `PovEnvironment.project_id` records which project a
POV went into, and the teardown reads that back rather than re-deriving it from current
config — the rule `expiry_reaper` states outright, that a destroy aimed at the wrong
project is the worst version of this bug.

Two smaller differences:

- **Suspend is `stop`, not GCE's `suspend`.** GCE's suspend preserves RAM to disk and
  charges for that storage plus the reserved resources. `stop` lands on TERMINATED, where
  only the disks bill — which is the state the schedule is aiming at, despite the name.
  TERMINATED reads back as `stopped`, so a suspended POV does not appear destroyed.
- **The bootstrap goes in `user-data`, not `startup-script`.** The guest agent re-runs a
  startup script on *every* boot, and the payload carries a single-use enrolment code.

One firewall rule, allowing the POV's own subnet to reach itself. GCP denies ingress by
default and allows egress, so nothing else is needed and nothing from outside reaches in.

### OCI, and the compartment that is not the environment

OCI is shaped like AWS: no native environment object, so a teardown unpicks resource types
in dependency order rather than deleting one group. Give the API user **manage** on
instances, VCNs, subnets, route tables, security lists and internet gateways in its
compartment.

**A compartment is not used as the environment, despite being the obvious analogy.** It
looks like an Azure resource group and behaves nothing like one: creating a compartment
needs tenancy-level IAM a POV's credential should not have, deleting one requires it to be
empty first, the delete is a slow asynchronous operation, and the name stays reserved
afterwards. A POV that could not be torn down in one pass, or whose name could not be
reused for months, would be worse than the tagging used instead.

The compartment is still **recorded** on the POV, the way GCP records its project, so a
teardown weeks later aims at the compartment the environment actually went into.

**Three things are OCI's own, and each fails by naming something other than the cause:**

- **Every VCN gets a default security list that allows SSH from `0.0.0.0/0`.** A POV placed
  on it would be a customer environment with port 22 open to the internet. This driver
  creates its own list — the POV's subnet in, everything out — and never attaches the
  default.
- **A Flex shape is refused without an explicit OCPU count**, and the API's error names
  `shapeConfig` rather than the template field. Most of OCI's current catalogue is Flex, so
  a template's instance type accepts a size suffix rather than needing a column only one
  cloud would read:

  | Instance type | Result |
  |---|---|
  | `VM.Standard.E4.Flex` | 2 OCPUs, 16 GB — the defaults |
  | `VM.Standard.E4.Flex:4` | 4 OCPUs, 16 GB |
  | `VM.Standard.E4.Flex:4:32` | 4 OCPUs, 32 GB |
  | `VM.Standard2.2` | a fixed shape; a suffix on one of these is **refused**, because it means the author believed they were sizing something |

  The POV page reports the shape back in the same form, so you are never translating a
  `shapeConfig` into a suffix by hand.
- **`user_data` must be base64.** The SDK passes it through untouched, and plain text gives
  an instance that boots fine and never runs its bootstrap. Azure has the same wrinkle;
  AWS and GCP do not.

Two smaller notes. Availability domains are **listed, never assembled** — an AD is named
like `Uocm:PHX-AD-1`, a tenancy-specific prefix plus a region code, so there is nothing to
build. And suspend uses **SOFTSTOP**, which asks the guest to shut down before falling back
to a hard stop: a POV somebody resumes next morning should not have been pulled out at the
cord every night.

### The suspend schedule

Skytap suspends an environment on its own idle timer. No public cloud has one, so on a
cloud the dashboard supplies the timer — a **schedule**, set per POV from the *Sleeps*
column, or carried on a blueprint so every POV of a kind starts with it.

| Field | Meaning |
|---|---|
| Suspend at | Local time, 24-hour `HH:MM`. Blank removes the schedule |
| Resume at | Blank means it stays down until somebody starts it |
| Timezone | An IANA name. Blank means UTC — not the server's zone, which is an accident of its base image |
| Days | Seven characters of `0`/`1`, Monday first. `1111100` is Mon–Fri |

A schedule rather than an inactivity timer, because "idle" on a cloud has no honest
definition from outside the guest. A PRA session says nothing about a customer clicking
around a console; an agent heartbeat never stops. Every candidate signal has a blind spot
that either leaves a POV running all month or suspends one mid-demo. Business hours are
something you can state, predict, and explain on an invoice.

**The rule is "has a boundary been crossed since the last check", not "should this be
asleep right now".** That distinction is the whole design, and it shows up in three places
you would otherwise file as bugs:

- **A manual start outside hours survives.** Start a POV by hand at 20:00 for a call and it
  stays up until tomorrow's suspend time. A state check would put it back to sleep on the
  next sweep, four minutes later, and every sweep after that.
- **The first pass after you set a schedule does nothing.** An unevaluated row has crossed
  every boundary there has ever been, so the first pass records the time and acts on
  nothing — the same rule the auto-delete timer's arming clock follows.
- **An outage settles on the later boundary.** Down from 18:00 to 22:00 with a 19:00
  suspend and a 21:00 resume, the POV ends up running. It does not replay both. A latch
  older than 25 hours is re-armed rather than replayed at all.

The sweep rides the reconcile pass, which already runs every ten minutes and already knows
each POV's real runstate. It only ever **enqueues a `pov_env_power` job** — the same one
the Suspend and Start buttons create — so a scheduled action has a `/jobs` row, a Live
Output, a cancel, and a place in the failed-jobs panel. It is attributed to
`pov-schedule`, not to whoever set the schedule, so the row says why the POV went to sleep
at 19:00.

Setting a schedule is refused on a platform that has its own idle timer. The two answer
the same question, and a POV carrying both is one where neither is in charge.

#### The customer can wake it

A schedule without a customer-reachable resume is a broken demo: the POV sleeps at 19:00
and the prospect trying it at nine the next morning finds a dead environment and an SE to
email. So the accessor page carries a **Start it** button.

It is **start only**. There is no suspend and no destroy on that page, and the endpoint
takes no environment id — it resolves the POV from the accessor's own session, the same
way every other write on that page does, so "could they stop somebody else's POV?" is
unanswerable rather than merely guarded. The job it enqueues is the one the SE's Start
button creates, attributed to `pov-accessor` so the `/jobs` row says a prospect pressed it.

The button renders from state the API serves rather than probing, so it only appears when
pressing it will work. When it does not, the page says why in a sentence the prospect can
act on:

| State | What they see |
|---|---|
| Suspended, under its cap | **Start it** |
| Already starting | "Give it a few minutes" — a second job would race the first |
| Still provisioning, or being destroyed | The lifecycle reason |
| **Over its spend cap** | "Ask your contact to raise it" |

That last one matters. The spend cap **latches** once it has acted, so the sweep will not
re-suspend — which means a prospect who woke a capped POV would leave it running past its
cap indefinitely, and the account owner's one cost control would be the one anybody could
undo.

**Suspending a cloud POV is not free.** Stopping an instance halts its compute charge and
nothing else — the root volume, the public address and the network keep billing for the
whole evaluation. A schedule cuts the largest line on the bill, not the bill.

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

### The broker VM, on a cloud

A POV's broker is the VM that carries the dashboard agent, the Gateway and the Password
Safe Resource Broker. On Skytap the template ships it, the dashboard writes a payload into
its `user_data`, and a runner inside the guest fetches and executes it. A cloud does the
last part for you — cloud-init runs user-data — but only **on first boot**. Handing a
payload to an instance that is already up does nothing at all, silently.

So on a cloud the broker VM is **created by the Broker step, not by the build**, with its
bootstrap already in user-data. That ordering is forced by the agent's policy, which
grants the POV's target addresses one at a time — and those addresses do not exist until
the targets do. The sequence is: build the targets → wait for them → read their addresses
→ mint the enrolment code → create the broker VM carrying both.

Two consequences worth knowing:

- **An environment shows one fewer VM than its template until the broker lands.** That is
  the honest reading of "the broker is not installed yet", rather than a VM sitting there
  doing nothing.
- **Pressing Broker again rebuilds the VM.** The old one is terminated first, which
  destroys its agent-state volume with it. On Skytap a re-broker has to remember to delete
  that volume — an agent that already enrolled never redeems a second code, so a surviving
  volume gives a container that starts fine and 401s forever. Here it is clean by
  construction.

The broker VM's image must have **cloud-init and Docker**. Build it with the Packer
tooling and reference it from the template.

**The spent enrolment code stays in user-data.** A cloud's user-data can only be rewritten
while the instance is stopped, so unlike the Skytap path there is nothing to clear once
the code has been redeemed. The exposure is small — the code is single-use, fifteen
minutes old by then, and IMDSv2 is required so reading it needs a token obtained from on
the guest — but it is a real difference rather than an oversight.

### What a cloud POV does not have

Read these off the platform's capability row rather than discovering them:

| Not available | Why, and what to use instead |
|---|---|
| **Share link** | No cloud has publish sets. The customer's front door is PRA, which makes PRA **required** for a cloud POV where it is optional on Skytap |
| **Idle suspend** | No cloud has a platform idle timer. The dashboard supplies a scheduled suspend instead |
| **Stored credentials** | **AWS only.** AWS holds no guest login to read back, so the platform login comes from your image and its Vault account. Azure generates one — see above |
| **Published services** | No NAT-a-guest-port primitive, and none needed — access is PRA through this POV's own Gateway |

---
