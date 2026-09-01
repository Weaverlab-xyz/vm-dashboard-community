# A POV Instance

A **second** dashboard, next to the one managing your demo estate. Same image, same code,
its own database, its own users, its own agents — and a different set of features.

It exists because of one thing: **tenancy.** A demo instance resolves its BeyondTrust
tenant from the global singletons (`bt_api_host`, `pscli_api_url`, `entitle_api_key`). A POV
instance holds a *registry* of many named tenants, because several POVs run at once and each
has its own PRA appliance and Password Safe Cloud tenant. An instance claiming both roles
would have two answers to "which tenant?" at every call site — and the wrong answer is
silent, not loud: a demo VM deploy onboarding into a customer's Password Safe, or a POV
onboarding into your demo tenant. Nothing errors, both paths "work".

So the two profiles are **mutually exclusive**, and the exclusion is enforced in code rather
than left to discipline.

---

## The gate

`install_profile` is `demo` (the default, and what every existing install already is) or
`pov`. It is set on the setup wizard's **Purpose** step, and it is a gate, not a preference.

Everything resolves through one function — `services/feature_flags.enabled()`:

```
enabled(flag) = False                         if this profile masks the flag
              = config_service.get_bool(flag) otherwise
```

That single reader is the point. `main._feature_gate` decides whether a router 404s, and
`feature_flags.flags()` decides whether the nav link and Settings toggle render. A mask
applied in only one of them gives you a nav link to a router that 404s, or a page with no
way to reach it. Both come through `enabled()`.

Two properties worth knowing:

- **The mask only ever subtracts.** A profile can refuse a feature. It can never turn on one
  that config left off.
- **An unrecognised profile resolves to `demo`.** The profile is read on the request path, so
  a typo in one config row must not take the app down — and falling back to `demo` means the
  worst case is *today's* behaviour.

### What each profile gets

| | `demo` | `pov` |
|---|---|---|
| Cloud VMs, images, Packer, promote | yes | — |
| On-prem hypervisors (Proxmox, vSphere, Hyper-V, Nutanix, XCP-ng) | yes | — |
| Cloud databases, Kubernetes, containers, cloud functions | yes | — |
| Virtual desktops, EPM-L, cost reporting | yes | — |
| The AWS / Azure / GCP / OCI consoles and Images | yes | — |
| POV environments | — | yes |
| Lab platforms (Skytap) | — | yes |
| PRA, Password Safe, Entitle | yes | yes |
| Remote agents, Config Management, Storage | yes | yes |
| Auto-delete timer, notifications, secret scanning, auth/SSO | yes | yes |

The third block is deliberate: a POV instance needs PRA, Password Safe and the agent more
than a demo instance does, so those are profile-neutral rather than owned by either.

The cloud-console row is the one entry that is **not** a feature flag. Those five pages
gate on credential *presence*, so there was no flag for the mask to subtract and all five
kept rendering on a POV instance — pointing at pages the wizard guarantees can never hold
data. They resolve through `feature_flags.profile_page_allowed` and `main._profile_page_gate`
instead, which obey the same rule as the flag gate: one reader for the nav link and the
route, or you get a link to a 404.

**Storage stays**, and it is load-bearing rather than an oversight. Config Management is
gated on there being an active storage backend, so without one a POV instance cannot run
a playbook at all — and it has no cloud to put one in. The
[agent-brokered filesystem backend](storage-management.md#remote-filesystem--unc-via-agent)
is the answer: a POV already runs an agent inside the customer's environment, and that
agent can reach a share the dashboard cannot.

A demo-only integration on a POV instance is not merely toggled off — **Settings refuses to
enable it**, with a 409 naming the profile. Accepting the write would store a flag that
reads back as on while `enabled()` keeps returning `False`: a toggle that saves cleanly,
shows on, and does nothing. Turning something *off* is always allowed.

A POV instance also needs a **lab platform** — the thing a POV environment actually runs
on. Skytap is the first one; see [integrations/skytap.md](integrations/skytap.md) for its
prerequisites, the API token it needs, and the
[template contract](integrations/skytap.md#the-template-contract) its broker VM has to
satisfy.

And it needs somewhere to put the many tenants the first paragraph of this page is about.
That is [the tenant registry](#the-tenant-registry) below. The platform registry lives in
`services/lab_platforms.py`, and `GET /api/pov/platforms` reports what each one can do so
the UI can degrade visibly rather than offering a button that fails.

---

## Standing one up

```bash
# Its own JWT key. Not a copy of the demo stack's — see below.
openssl rand -hex 32 > .jwt_secret_key.pov

# Its own env file. Start from the example and set only what a POV instance uses.
cp .env.example .env.pov

docker compose -p vmdash-pov -f docker-compose.pov.yml up -d
```

Then browse `http://localhost:8002` (the demo stack keeps 8001) and walk the wizard: create
the admin account, choose **Customer POV / POC environments** on the Purpose step, and the
four cloud credential steps disappear.

`-p vmdash-pov` matters. Without an explicit project name Compose derives one from the
directory, and both stacks would try to share networks and volume names.

### Four things that will bite otherwise

**Its `JWT_SECRET_KEY` must be its own value.** That key is the Fernet root for the
`app_config` table. Two instances sharing it is the only way their encrypted configuration
could be interchanged — and two instances with *different* keys is exactly why a `pg_dump`
restore from one into the other yields **unreadable ciphertext with no error at all**. If you
need to copy settings across, use the config-migration tool
([config-migration.md](config-migration.md)); never a database dump.

**Bring it up alone the first time.** First boot runs `init_db`, which takes a Postgres
advisory lock. Two cold instances started together can wedge on it. Start this stack, let it
finish, then start the other.

**Its agent endpoint must be reachable from inside your POV environments.** POV wiring works
by running an agent on a broker VM *inside* the customer environment, which polls outward —
see [the broker VM](integrations/skytap.md#the-broker-vm) for what that VM must carry.
That means `/api/agent` on this instance has to be reachable over public HTTPS from there.
The provision refuses the broker step outright when this instance does not know its own
public URL, or when that URL is plaintext: the agent will not sign over `http://`, so a
broker installed against one would never enrol.
When you host it beyond your LAN, use the gateway-sidecar split in
[cloud-hosting.md](cloud-hosting.md) so the agent endpoint is separate from the UI, and keep
the UI behind its own allowlist. A UI 404 that returns in microseconds is the allowlist
doing its job, not a broken deploy.

**Auto-delete is off, observe-only is on, and two one-hour arming clocks have not started.**
A POV is a 30-day resource, so this is the one piece of configuration a POV instance is not
useful without — and enabling it is deliberately not something the compose file can do.
Follow the rollout in [auto-delete-timer.md](auto-delete-timer.md) *before* you create a POV
you expect to be cleaned up. Note also that `resource_expiry_max_total_hours` defaults to
`720` — exactly 30 days — so a 30-day POV starts at the ceiling and cannot be extended until
you raise it.

---

## Blueprints, and where templates come from

**POV → Templates** (`/pov/templates`), admin only. Two things live there.

**The template builder** authors the templates a POV is built from — see
[building a template](integrations/skytap.md#building-a-template). It exists because a POV
*is* a template instantiated whole, so before it the whole feature was downstream of a
catalogue nobody could author from here, and because the one piece of the
[template contract](integrations/skytap.md#the-template-contract) that has to live in your
image — the metadata runner — had no automation at all.

**A blueprint** is a saved POV recipe: the fields the create form asks, under one name. Pick
one on the POV page and it fills the form; you still type the POV's own name. For a given
*kind* of POV — a Password Safe evaluation, a PRA-only demo — every field except the name
has the same right answer every time, and two SEs building "the same" POV should build the
same POV.

| A blueprint carries | It does not carry |
|---|---|
| The template, project and broker VM name | The POV's **name** — that is per-POV by definition |
| The idle timeout and an expiry override | The **workgroup**, which decides RBAC and the expiry exempt list. Not something a saved recipe should set silently |
| The three tenant references | Any **secret** — see below |
| The Gateway name, and the Resource Broker's VM, zone and installer asset | |

### It is defaults, not a second provision path

A blueprint supplies values for the fields a request left blank and nothing else. Everything
after that is the same provision job that runs without one — a blueprint that forked the
flow would be a second place for the rules about orphaned environments to drift. If you
typed a broker VM name before picking a recipe, the recipe does not overwrite it.

The tenant ids on a blueprint are validated **when you save it**, against the same check the
create form uses. A stored selection that only failed at provision time would move a form
error into a job, against an environment that already exists.

### Nothing on a blueprint auto-runs, and nothing on it is a secret

It is tempting to have a blueprint chain the Gateway, Resource Broker and wire-up straight
off a provision. It cannot, for two reasons that are worth stating rather than discovering:

- **Both installs run through the broker agent**, which does not exist until minutes after
  the environment comes up. A step enqueued at provision time would find no agent.
- **Both need a credential that is per-tenant, not per-recipe** — a PRA deploy key and the
  Password Safe installer key. A blueprint that carried those would spread two secrets
  across every recipe an SE ever saved, to save one paste.

So a blueprint fills in the non-secret half — the Gateway name, the Resource Broker's VM,
zone and asset — onto the new POV, and those panels open ready to press with only the secret
left to paste. A field the POV already carries is never overwritten.

---

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

**A GCE label key must be lowercase.** The rule is `[a-z]([-_a-z0-9]*)?`, so
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

**Suspending a cloud POV is not free.** Stopping an instance halts its compute charge and
nothing else — the root volume, the public address and the network keep billing for the
whole evaluation. A schedule cuts the largest line on the bill, not the bill.

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

## Keeping the view true

A POV's `runstate` is a **remembered** value, and for most of this feature's life it was
only ever written when this dashboard changed it. Two things change it that this dashboard
does not: the platform's own `suspend_on_idle` timer, and anybody with a Skytap login.

So a reconcile sweep runs every ten minutes — one collection read per configured platform —
and writes back what the platform says: runstate, the rate-limit flag, and the idle timer's
current value. **Re-check platform** on the POV page runs the same pass on demand.

Three properties are worth knowing, because each is a way this could do more harm than the
staleness it fixes:

**The page prefers a live read when it has one.** Opening `/pov` already reads every
environment on the platform for the read-only table, so the managed table shows that and
labels it `live`. The sweep is what keeps the *rows* honest for everything that reads them
when no page is open. Each row says which it is showing — `live`, `confirmed 8m ago`, or
`not confirmed` — because a remembered value presented as a current one is the whole bug.

**Both power buttons show while a reading is stale.** Gating Start on a remembered runstate
is what hid it from every POV the platform had suspended: the cost feature working exactly
as designed removed the control needed to undo it.

**A missing environment is flagged, never reaped.** Absence from the listing is not proof of
deletion — the listing is project-scoped, so an environment outside the configured project
is invisible and perfectly alive — so the sweep confirms with a direct read and only a 404
sets the flag. Even then it only flags: the row holds the only record of which PRA, Password
Safe and Entitle tenants that POV was wired into, and the manifest for reaping them. Use
**Destroy** to close one out; it is idempotent on a 404. The flag clears by itself if the
environment becomes visible again.

---

## The tenant registry

The reason this profile exists, made concrete. **POV page → BeyondTrust tenants.**

A demo instance has one PRA appliance, one Password Safe tenant and one Entitle tenant,
configured in Settings as `bt_api_host`, `pscli_api_url` and `entitle_api_url`. That is the
right shape when there is exactly one of each. A POV instance runs several POVs at once,
each for a different customer, each with its own appliance — so "which tenant?" stops
having one answer and the singletons stop being able to express the question.

The registry holds **one row per product, not per customer**, and a POV carries three
independent references. That is not an accident of modelling: PRA and Password Safe are
genuinely per-customer, while Entitle is multi-tenant behind one canonical API URL and is
usually the same tenant for every POV. One row holding all three would force a duplicate
Entitle credential per customer, and duplicated credentials rotate apart.

| | |
|---|---|
| **Name** | A slug — lowercase letters, digits, hyphens. See [below](#about-customer-data) |
| **URL / hostname** | `tenant.beyondtrustcloud.com` for PRA, the Password Safe URL, or the Entitle API base |
| **OAuth client id** | PRA and Password Safe. Entitle authenticates with a bearer token and has no paired id |
| **Client secret** | Encrypted with the same Fernet key as every other secret in this dashboard |
| **…or a vault reference** | `aws_sm://`, `azure_kv://`, `gcp_sm://`, `bt_safe://` — for operators who want no credential in this database at all. One or the other, never both |
| **Jump Group / Gateway** | PRA only. Names *inside that appliance*, which is why they belong on the tenant and not in global settings. The stored option key is still `jumpoint_name`, matching the `bt_jumpoint_name` setting it seeds from — see [why the key looks wrong](#why-the-gateway-option-key-looks-wrong) |
| **Password Safe run-as user** | Required by the `passwordsafe` Terraform provider block |

### How a POV picks one

Choose them on the New POV form, or press **edit** in the Tenants column of an existing
POV — a POV is often stood up while the customer's appliance is still being provisioned,
so "choose later" is a real answer and not a placeholder.

What the resolver does, in order:

1. the **id the POV carries**. A missing, disabled or wrong-product id is an **error**,
   never a fallback;
2. the row marked **default** for that product;
3. the only active row for that product, if there is exactly one;
4. **the Settings singletons**, if the registry holds no row for that product at all;
5. otherwise a refusal naming the fix.

Step 1 not falling back is the whole point. Falling back would mean a POV whose tenant was
deleted or disabled quietly resolving to *the default instead* — which is a POV onboarding
into another customer's Password Safe with nothing going wrong on the way. Step 5 is the
same rule seen from the other side: guessing between two customers' appliances is much
worse than refusing.

Step 4 is the compatibility contract. Every existing install, and every demo instance,
keeps working without knowing this table exists.

### Verify

**Verify** performs the same authentication the real work does — PRA's OAuth token
request, Password Safe's token plus `SignAppIn` — and stores the result on the row. Both
halves of the Password Safe check matter: the token proves the OAuth client, while
`SignAppIn` proves the BeyondInsight user it is linked to. An OAuth client with no linked
user gets a perfectly good token and fails at the second step, so checking only the first
would report green for a tenant that cannot do anything.

**Entitle has no Verify, deliberately.** Everything this dashboard does with Entitle goes
through the Terraform provider or POSTs an access request, and an access request is a side
effect rather than a check. There is no read here that would prove a bearer token, and an
invented one is how a Verify starts reporting green for a token that does not work. The
row says "not verifiable" rather than hiding the distinction, because "we did not check"
and "we checked and it was fine" must never look the same on a page you use to decide a
POV is ready.

A verify result is **cleared** when the URL, the client id or the secret changes. The
tenant that passed is not the tenant you now have, and stale-green is worse than unknown.

### Seeding, and the one-way door

On first boot the dashboard copies whatever singletons the install already has into rows,
once. After that **the rows are the truth and editing the Settings keys does nothing** —
the same one-way promise the Connections page makes for hypervisors, and for the same
reason: a second copy that keeps re-reading the singletons is how an operator edits a
field and watches it have no effect, or worse, have an effect a week later.

The seed is marked done rather than inferred from an empty table, so deleting a seeded row
on purpose does not bring it back on the next restart.

### Deleting one

Refused while a live POV still references it. The database constraint is `SET NULL`, so
deleting anyway would not error — it would blank that POV's tenant, and the next wire-up
would resolve the *default* instead. **Disable** it if you want it out of the pickers
without touching the POVs already wired into it.

### Why the Gateway option key looks wrong

BeyondTrust renamed this component to **Gateway**, and this dashboard's prose follows
that everywhere. Identifiers deliberately do not: `bt_jumpoint_name` is a row in
`app_config` and a line in operators' `.env` files, and `/api/config/v1/jumpoint` is the
vendor's own Config API path. Renaming either reads a key that was never written, or
calls a path that 404s — and the first failure is a blank setting rather than an error.

So a tenant's option key is `jumpoint_name` while the field is labelled **Gateway name**.
That asymmetry is the rule, not an oversight; `tests/test_gateway_terminology.py` pins
both halves of it.

### About customer data

This dashboard deliberately holds no customer field: `PovEnvironment` has none and adding
one should be treated as a schema regression. This table is the one place that rule bends,
and only exactly as far as it must. A PRA appliance is reached at a hostname like
`acme.beyondtrustcloud.com`, so the connection target inherently identifies whose it is
and there is no way to hold one without that.

A connection target you cannot avoid. A free-text "customer" box you can — so the **name**
is a constrained slug and not somewhere to type a company. Use a reference: `poc-014-pra`.

---

## The POV Gateway

The point at which a POV becomes *reachable*. **POV page → the Gateway column → edit.**

A POV's VMs live on the lab platform's private network, and every PRA jump item a later
release creates has to name a Gateway that can see them. This installs one: a BeyondTrust
Gateway container, on the broker VM from [the broker section](integrations/skytap.md#the-broker-vm),
registered into the PRA tenant [this POV is wired into](#the-tenant-registry).

**The dashboard does not create the Gateway in PRA.** You create it in the customer's
appliance and copy its deploy key — the same thing you already do for the cloud gateway
hosts, where that key sits in `aws_ecs_docker_deploy_key` and friends. What this adds is
per-POV: a key per environment, installed on a VM the dashboard cannot reach directly,
into a tenant it had to be told about.

So the sequence is:

1. In the customer's PRA, create a Gateway and copy its deploy key.
2. On the POV, set the **Gateway name** — the name you gave it in PRA — and paste the key.
3. Press **Install**. The broker agent starts the container and the job reports what
   happened on the VM.
4. Press **check** to ask PRA whether a node of it is connected.

### Where the deploy key goes, and where it does not

It is stored encrypted with the same Fernet key as every other secret here, and it reaches
the broker VM over the agent's **sealed per-job channel** — fetched once the job is
running, bound to a key that exists only for that fetch.

It is deliberately *not* in the job's metadata, *not* in the signed job envelope, and
above all **not in the lab platform's `user_data`**. That last one is the important
distinction from [the enrolment code](integrations/skytap.md#the-broker-vm), which does
ride that channel: an enrolment code is single-use and lives fifteen minutes, whereas a
PRA deploy key is neither. Every node registered with it joins the same Gateway — that is
exactly what makes the cloud gateway hosts a cluster — so anyone with read access to the
environment seeing it once is enough.

### What the agent will and will not do

The Gateway image comes from the **broker's `policy.yaml`**, never from the job. A job
says only *install* or *remove*; there is no image field, no container name and no
privileged flag anywhere in the protocol. That is the same rule the Config-Management and
hypervisor runners follow, and `examples/remote-agent/policy.example.yaml` documents the
`gateway:` block for an operator's own agent.

On a POV that file is **generated by the dashboard**, so read it accordingly: it is not
the trust boundary it is on a customer-owned agent, because the dashboard also created the
VM, the bootstrap and the agent. Keeping the same shape is still worth it — one code path
on the agent rather than two — but nobody should read a generated `gateway:` block as the
customer having agreed to it.

`privileged: true` is in that block and is not decoration. A Gateway carries protocol
tunnels, which needs `NET_ADMIN`, `NET_RAW`, `IPC_LOCK` and `/dev/net/tun`. Docker has no
granular way to grant that set, so it is the all-or-nothing flag — and the agent refuses
to start a Gateway without it rather than start one that cannot work. Without those
capabilities a Gateway registers **online** and every tunnel silently times out, which
reads as a firewall problem for a long time.

### Why "is it there?" is the wrong question

A Gateway is a **cluster**, not a host. Re-installing on a rebuilt broker VM adds a node
and PRA parks the dead one, so the name is present either way. The check therefore asks
whether a *node* is connected, and reports the node count next to it — if that number is
climbing across reinstalls, old nodes need retiring in the appliance.

`connected` is tri-state. Which field an appliance reports varies by version, and one this
dashboard does not recognise reads as **unknown**, never as disconnected: telling an
operator their Gateway is down because we did not know a key name is worse than saying
nothing.

### Upgrading a POV that predates this

A broker enrolled before this release has a `policy.yaml` with no `agent_gateway` grant
and no `gateway:` block, and an agent image below **2.4.0**, the build that first carried
the `agent_gateway` handler. Both are fixed the same way:
pull a newer `chrweav/dashboard-agent` onto the broker VM, then press **Broker** on the
POV, which re-issues the enrolment and rewrites the policy. The install refuses with that
remedy rather than queueing a job the broker would decline.

### Teardown

Destroying a POV queues the Gateway removal first, then revokes the broker, then deletes
the environment. The removal is **tidiness, not reaping** — the environment delete takes
the container with it either way — so a POV whose broker is already gone still destroys
cleanly, with a line in the job log saying the PRA-side node will linger.

The stored deploy key is cleared synchronously, because that is local state and leaving a
customer's credential in this database after their POV is gone is the part that matters.

---

## The Resource Broker

The Password Safe half, and the second Windows machine in a POV. **POV page → the Resource
Broker column → edit.**

A Password Safe Resource Broker gives the tenant reach into the POV's private network, the
way [the Gateway](#the-pov-gateway) does for PRA. It is a **Windows** program — Server 2019
or 2022 x64 — so it does not live on the broker VM, which is a Linux Docker host. A POV
therefore has two special VMs: the Linux broker running the agent and the Gateway, and a
Windows host running the Resource Broker.

**The dashboard cannot download the installer.** The package is generated per Password Safe
tenant and comes from that tenant, so there is no image to name and no URL to fetch. The
customer stages it:

1. Download the installer from the Password Safe tenant, along with its **installer key**.
2. Upload it on **Config Management → assets**. It is a `.exe`.
3. On the POV, name the Windows VM, pick the staged installer, and set the **resource
   zone** and the installer key.
4. Press **Install**.

### Two parameters, not one

This is the part that catches people. A silent install needs **`INSTALLKEY` and `ZONE`**,
and without the zone the installer **prompts** — which in an unattended run is not an error
but a process that sits there until the run's timeout kills it, with an install log that
ends mid-dialog. So the dashboard refuses to queue a run with either missing, and says
which.

The key is a credential and the zone is not, so they travel differently: the key is stored
encrypted and resolved when the agent fetches its sealed bundle, while the zone is a plain
value on the POV. Neither reaches the command line of the runner process — `argv` is
readable by every process on that host.

`RESTART` is deliberately never set. During a silent install it restarts the machine
automatically, which would drop the WinRM session mid-play and make Ansible report the
failure of a step that had in fact succeeded — the worst kind of wrong answer, because
retrying reinstalls a working broker.

### There is no login field, on purpose

The Windows credential comes from the **lab platform's own stored credentials** — Skytap
records a login against each VM, and the dashboard reads it per run. So no Windows password
for a customer's environment is stored here at all, and a template whose password changed
is picked up on the next run with nothing to update.

Skytap stores that as **free text** — whatever somebody typed in the box — so the dashboard
parses it, and **refuses rather than guesses**. `administrator / Passw0rd`,
`administrator:Passw0rd` and `username: … password: …` are understood; a sentence or a
multi-line note is refused, naming the VM. A bare space is not treated as a separator,
because a password containing one would split in the wrong place.

Two usable credentials on one VM are answered differently by the two callers, and the
difference is whether the caller can *ask*:

- **The Resource Broker install refuses.** It seals one credential into a run bundle the
  agent uses over WinRM, so it never authenticates and never learns the outcome — order is
  the only thing it could go on, and installing an RB as the wrong account is not a mistake
  worth being clever about.
- **A [template build](integrations/skytap.md#building-a-template) tries each in turn**,
  because it opens the SSH connection itself. The first login the VM accepts wins, and the
  build row records which one it was. If the guest answers and refuses them all, that
  fails immediately rather than on the readiness ladder: a rejected password does not
  become right in fifteen seconds.

If a template's credential box cannot be parsed, fix the box — a wrong username comes back
from WinRM as an authentication failure, which reads as a bad password and sends you to
reset one that was fine.

### What it grants, and why the scope is narrow

Installing the broker runs a **playbook, as root, on a host in the customer's
environment**. That is a much larger grant than the discovery sweep the broker already has,
so it gets its own target list in the generated `policy.yaml` — scoped to the named
Resource Broker host alone, or to the POV's Windows guests before one has been named, on
5985/5986 and nothing else. The Linux broker's own address is deliberately not in it.

A POV brokered before this release has no `agent_ansible` grant at all. Press **Broker** to
rewrite its policy; the install refuses with that remedy rather than queueing a job the
broker would decline.

### How long it takes

Longer than it looks. The bootstrapper is a bundle: it installs the VC++ 2010 and 2015-2019
redistributables, .NET Framework 4.7.2 and the .NET Core hosting bundle before the broker
itself. Minutes, and often a fresh download on a clean template.

### Teardown

Destroying a POV clears the stored installer key and forgets the broker id. It does **not**
uninstall anything — the environment delete takes the VM with it, and an uninstall run would
need the very WinRM session the teardown is about to make unreachable.

The broker's registration in the Password Safe tenant is a customer-side object this
dashboard never created, so the job log says it is still listed there rather than pretending
to have reaped it.

---

## Wiring the VMs into PRA, Password Safe and Entitle

The point at which a POV becomes usable by a *person*. **POV page → the Wired column →
wire.**

Up to three artifacts per VM, and they are **independent**:

* a **PRA jump item** in the customer's own appliance, tagged `POV`. Linux guests get a
  Shell Jump on 22; Windows guests a Remote RDP jump on 3389, with a Vault account for
  credential injection when the lab platform has a login for that VM;
* a **Password Safe managed system + managed account**, reached *through* the Resource
  Broker;
* an **Entitle SSH ephemeral-accounts integration**, for Linux guests.

A POV with no Password Safe or Entitle tenant gets the first and skips the rest with a line
in the job log. That is not a degraded outcome — a POV wired into PRA is already useful,
and failing the jump items over a half-configured tenant elsewhere would be the wrong
trade.

### Which Gateway they route through

Every jump item routes through **this POV's own Gateway** — the one
[installed inside the environment](#the-pov-gateway) — and never through the tenant's
appliance-wide default. That is not a preference. The default one lives on the customer's
side of the world and has no route into the POV's private network, so an item pointed at
it is created successfully, looks correct in the appliance, and times out at session
launch. That is the most expensive failure in this feature to diagnose, because
nothing reports it until somebody clicks.

So a POV with no Gateway is refused, up front, before any VM is touched.

### The tenant, not the singleton

The jump items are created against the PRA tenant [this POV is wired
into](#the-tenant-registry) — resolved with the POV's explicit id, so a POV whose tenant
was deleted or disabled is an error rather than a quiet fall back to the default. A
*partial* credential override is refused rather than merged with the configured appliance:
authenticating to one customer's host with another's client id is precisely the silent
cross-tenant mistake the registry exists to prevent.

### What is skipped, and why

| The VM | What happens |
|---|---|
| No private address | Skipped. The platform reports one once it is running |
| No OS reported | **Skipped, not guessed.** An SSH jump to a Windows box fails at session launch, in front of whoever clicked it |
| Already has a jump item | Skipped. PRA will happily create a second item with the same name pointing at the same host, and the second one is invisible here |

Each VM's outcome is written to its row the moment it exists, so a run that crashes halfway
leaves nothing in the appliance this dashboard cannot find again. One VM's failure does not
stop the others — a POV where seven of eight VMs are reachable is worth more than one that
rolled back to zero because the eighth had no address yet. A run where **nothing** worked
does fail the job, though: a green job with zero artifacts is the one nobody goes back and
reads.

Re-running is the remedy for a half-finished run, and it is safe by the "already wired"
rule above.

### Password Safe reaches the VM through the Resource Broker

Every managed system names the POV's Resource Broker as its **application host**. That is
the field that tells Password Safe to manage the host *via* the broker rather than
reaching a private address from the cloud tenant — which would fail on every rotation,
days later and on a schedule, rather than at onboarding. So a POV with no Resource Broker
skips this half and says so.

Two things come from the tenant rather than from Settings, because they are names inside
*that* tenant and mean nothing in another one:

| | |
|---|---|
| **Workgroup** | Where the managed system lands |
| **Functional account (Linux / Windows)** | Split by guest OS because Password Safe derives the managed system's **platform** from the functional account. One account cannot serve both |

A guest whose OS has no functional account configured is skipped with that reason — a POV
of only Linux VMs has no use for a Windows one, and demanding it would block a wire-up
that could have completed.

When the lab platform has a login for the VM, it is seeded as the managed account's
initial credential, so the first rotation replaces a password somebody knows rather than
one nobody does.

### Entitle needs an agent this dashboard does not install

Worth reading before you plan a POV around it. Entitle's SSH connector reaches a **private**
target through an **Entitle agent** running inside that network — a Kubernetes deployment,
named on the tenant as its *agent token*. A POV's VMs are on a private network by
construction, so every POV integration is a private one.

This dashboard installs a [Gateway](#the-pov-gateway) and a
[Resource Broker](#the-resource-broker) inside a POV. It does **not** install an Entitle
agent. So the agent is a prerequisite you deploy and then name on the tenant, and a POV
whose Entitle tenant names none is skipped with exactly that reason rather than being
registered against this instance's own Entitle tenant.

It also needs a **key**. Entitle's connector authenticates with an SSH private key, not
the password the lab platform holds — so this is the one credential in the whole POV
wire-up that cannot be derived from something already there. Store the private half of the
key baked into your template's Linux guests with **ssh key** on the POV row; it is
encrypted like every other secret here and cleared at teardown.

Windows guests are skipped: the ephemeral-accounts app mints an account *over SSH*, which
a Windows guest does not answer.

Four things come from the tenant, because they are ids and names inside *that* Entitle
tenant: the **owner id**, the **workflow id**, the **agent token name** and the **SSH sudo
user**.

### Teardown

Destroying a POV removes the Entitle integrations **first**, then off-boards the managed
systems, then removes the jump items — all before the Gateway, the broker agent and the
environment itself.

Entitle goes first because an integration is standing *access*: of the three, it is the
artifact whose lingering matters most. None of them stops the others — an integration or
managed system left in a tenant is untidy, while stopping the chain leaves something
worse behind, and a row whose removal failed keeps its state so a re-run can finish it. They are the only
artifacts in a *customer's* appliance, and every later teardown step removes something they
were resolved through — the tenant, the Gateway, the environment.

A row whose destroy failed keeps its terraform state, so a re-run can finish it; clearing
it optimistically is how an item becomes unreachable. If the tenant cannot be resolved at
all, the job log says how many were left and where.

---

## Use cases, per POV

**Use cases in the nav, or POV page → a POV's name → Use cases.** Both lead to the same
checklist; the first picks the POV for you, the second is where you act on it.

The in-app [Use cases](personas/) catalog is instance-wide: it asks `feature_flags` whether
a demo can run here, and on a POV instance the answer for most of it is "no, this profile
masks that". Correct, and useless in front of a customer — it was twenty-six greyed-out
cards saying so. So **on a POV instance that page leads with a POV**: pick one from the
selector and you get its checklist, asking a different question — **can I run this on THIS
POV?** — whose answer comes from the three tenant columns on the row, which is exactly where
a Password-Safe-only evaluation differs from an all-three one.

The instance-wide catalog is still there, underneath, collapsed behind one click. Collapsed
and not removed: nothing on this page is ever hidden from you, and a demo instance opens
exactly as it always did.

Every role and every card is always on the page. The product mix decides what each card
**says**, never whether you can see it:

| The card | What it means | What it offers |
|---|---|---|
| Live | This POV has the products, and the wire-up has run for them | A link to the tab that runs it |
| Needs wiring | The tenant is set and the artifact is not | What to run, and a link to the Wired tab |
| Not part of this POV | No tenant for that product on this row | Nothing — the fix is a decision about the evaluation, not a button |

That last row is the one worth reading twice. It is **not** the same state as a demo-only
feature being masked on this instance: nobody can turn it on from Settings, because there
is nothing wrong. A POV scoped to Password Safe is a normal POV, and a page that greyed
those cards out as unavailable would say the opposite.

A card that names two products, on a POV missing one of them, reads "not part of this POV"
rather than "needs wiring". "Run the wire-up" would send you to a button that skips the
half you came for.

### Ticking them off

Each card has **Mark done** and **Skip**, and the count rides on the row in the POV list.

`Skip` is a real answer rather than a way to hide a card: "we showed them and it did not
land" and "we never got to it" are different things to walk into a renewal conversation
with, and only one of them is fixed by running the demo. Every tick records who made it —
which matters more once the customer can make their own, which is the
[accessor](design/pov-use-cases.md#slice-2--the-ephemeral-accessor) slice.

### Destroying a POV keeps this

Everything else in a teardown is removed. This is not: the POV row itself is marked
`destroyed` rather than deleted because it is the record of something that existed, and the
use-case history is what that record contains. The destroy job logs the summary — *"use-case
record kept: 9 of 14 run, 2 skipped"* — at the moment somebody is closing the evaluation
out, which is the one time anybody reads it.

The design note, including the accessor identity that is **not** kept, is in
[design/pov-use-cases.md](design/pov-use-cases.md).

---

## Accessors: giving the customer a login

**POV page → a POV's name → Access.**

The share link below is a door into the *lab*. An accessor is a door into *this dashboard*:
an ephemeral login, bound to one POV, that opens that POV's use-case checklist and nothing
else. It exists so the evaluation continues when nobody from your side is on the call, and
so the prospect's own view of what they have covered is theirs to keep.

Three properties are not configurable, for the same reason the share link's three are not.

### It can only ever reach its own POV

Not "has few permissions" — **cannot reach anything else**. Every other route in the
dashboard refuses it server-side, on an allowlist rather than a blocklist, so a page added
next month is refused before anybody remembers to think about it. It is invisible on the
Users page, it cannot be granted anything through the Entitle integration that grants
dashboard permissions, it cannot open a job's Live Output, and it cannot be handed a
personal access token. An admin cannot promote one by editing it, either: those routes
refuse and point back here.

### It expires, and never outlives the POV

Fourteen days by default, and always shortened to the POV's own auto-delete date. Ask for
longer than the environment has left and you get what the environment has left.

Belt and braces, because the failure mode is a credential nobody associates with anything
any more: destroying a POV removes its accessors **first**, ahead of the share link; the
auto-delete timer goes through the same job, so a reaped POV takes its logins with it; and
a sweep on the POV reconcile pass catches anything the other two missed — expired logins,
and logins whose POV is already gone. That sweep does **not** depend on the auto-delete
timer being on, because a fresh POV instance starts with it off.

The use-case record is deliberately *not* removed with them. An accessor is a credential;
the checklist is the account of what the evaluation covered.

### The password is shown once

There is no reveal button, unlike the share link's. That link's password has to be
re-readable because you read it to a customer days later; an accessor that has lost its
password is **replaced**, which is one click, leaves an audit line, and ends with a
credential exactly one person ever saw. Minting and revoking are both audited, so "who
could log in to this POV" is answerable afterwards.

### What they can do with it

They land on their own page — their POV's name, the lab link, what is set up on each
machine, and the checklist.

**They tick cards off themselves**, and every tick is recorded as theirs, so your Use cases
tab distinguishes "we showed them" from "they did it". **They can leave a note on any
card**, and those come back to you on the same tab, marked as the customer's. A note is not
a verdict: somebody writing "couldn't get this working" on a card they have not marked
leaves exactly that, and the card stays unmarked.

Your ticks and theirs live on the same row, and neither erases the other's note.

They also get the lab link and, on request, its password — revealed one press at a time and
audited, the same as when you reveal it. What they do **not** get is anything that is an
identifier inside your appliances: no jump item ids, no managed account ids, no integration
ids, no tenant names, and none of the wiring errors written for you. That list is a
projection built for them, not your view with fields removed.

### Where they come from

Two ways, and the Access tab shows which for every row.

**By hand.** Fill in an email — a label, so you can tell two accessors apart; nothing is
sent to it — and press **New accessor**. Give the customer the username and password on the
spot. They sign in at the normal `/login` and land on their own page.

**From Entitle.** This dashboard hosts an Entitle **Remote Adapter** in Ephemeral Accounts
mode at `/api/pov/accessor/rest`, so a prospect can request access in Entitle and have the
login minted, and removed, by the grant itself. **Register with Entitle** on the Access tab
creates that integration in *this POV's own* Entitle tenant — not the instance's, and not
one shared between customers: the asset is the POV.

Four things have to be true first, and the button is replaced by the reason when one is not:

| | |
|---|---|
| This POV names an Entitle tenant | There is nowhere to register it |
| `pov_accessor_rest_secret` is set | Unset, the adapter answers **503** to everything, so the integration would be created and then reject every call Entitle made to it |
| This instance knows its own public URL | Entitle calls the adapter from its own cloud |
| That URL is HTTPS | Entitle will not call a plaintext endpoint |

The secret is deliberately **not** `entitle_rest_secret`: that one authenticates an
integration that grants dashboard permissions, and this one mints logins. Give Entitle the
same value as a bearer token.

⚠️  **Check the connection mode once.** Whether Entitle infers Ephemeral Accounts from the
route set, as this assumes, is unconfirmed against a live tenant. After your first
registration, open the integration in Entitle and confirm its **Connection** setting says an
Ephemeral option — if it says Standing, tell us, because the discriminator is real and
belongs in the registration. Minting an accessor by hand does not depend on this at all,
which is why that path exists.

Removing the integration does **not** revoke logins that already exist: those are accounts
here, not grants there. Destroying the POV removes the integration first and the logins
immediately after — in that order, because a live integration can mint a new accessor while
the destroy is running.

The asset Entitle sees per POV is `pov:<environment id>`, and the account it mints is
always named `povguest_…`. That prefix is load-bearing: the delete route refuses any name
without it, so the integration can never be talked into removing one of your accounts.

---

## What a POV leaves behind

**POV page → Past POVs**, or the **Summary** tab on any POV.

Destroying a POV removes the environment, the logins, the jump items and the integrations.
It does not remove the account of what the evaluation covered — that row is marked
`destroyed` rather than deleted precisely because it is the record of something that
existed, and the checklist is that record's contents.

Until now nothing could reach it. The table of POVs filters destroyed ones out, which is
right for a list of things you can act on, so a finished evaluation appeared nowhere. **Past
POVs** is the way back: name, when it started, what it was wired into, how much was covered,
and whether the customer worked it themselves. Clicking one opens its Summary.

### The summary, and one distinction in it

The Summary tab is the same on a live POV and a finished one: coverage overall and per role,
every card somebody ticked or skipped, and **what was said** — the customer's own notes,
marked as theirs.

One thing it is careful about. *"A login was issued"* and *"they used it"* are different
facts, and only the second is evidence. The page says **the customer worked the checklist
themselves** only when a card was actually ticked by them; the Past POVs table reports the
two separately, so a login nobody opened reads as exactly that.

**Copy as Markdown** puts the whole thing on the clipboard — for a renewal note, a CRM, or an
internal write-up. It is copied rather than downloaded because that is where it is going
anyway.

---

## The customer-facing share link

Everything above is something an SE touches. This is the one artifact a **customer** opens:
a single URL onto the POV's desktops, published by the lab platform. On Skytap it is a
*publish set*; on the POV page it is the **Share** column.

Three properties are not configurable, because each one exists to stop a specific way a
POV link goes wrong.

### It always has a password, and the password is generated

Skytap treats the publish set's password as optional. This does not, and there is no field
to type one into. A blank password box is how an anonymous door into a lab holding a
Gateway, a Resource Broker and a set of Entitle integrations gets left open — and an SE's
usual password is barely better.

So the dashboard generates a 20-character password, hands it to the platform, and stores it
Fernet-encrypted alongside the POV's other secrets (`pov/<env-id>/share_password`, the same
shape as the Gateway deploy key). It is shown once when you share, and re-readable
afterwards from the **password** button — that read is audited, because "who has this
link's password" is the first question asked when a URL turns up somewhere it should not
have.

The alphabet excludes `I l 1 O 0`. These get read down a phone line more often than anyone
plans for.

### It always expires

There is no "never" option. The failure mode of a POV share is not that someone guesses the
URL — it is that the link *outlives everyone's attention*: the evaluation ends, the
environment sits on `suspend_on_idle`, and the URL keeps working for months.

The expiry is chosen in this order:

1. what you asked for, if you typed a number of days (1 to 90, refused outside that rather
   than clamped);
2. the POV's own auto-delete date, if it has one — so the link cannot outlive the
   environment it points at;
3. otherwise 14 days.

Skytap enforces the expiry itself, so an expired link is already dead. The page shows it as
`expired` rather than clearing the row, because during an evaluation the fact that a link
*was* shared is worth keeping.

### Re-sharing replaces; revoking is its own button

**Re-share** revokes the current publish set before creating a new one, and mints a new
password. Leaving the old one would give the POV two live URLs and one stored id — the
second could never be revoked from here again. It also means re-sharing is a real remedy
when a link went to the wrong person.

**Revoke** kills the link without touching the POV. The platform call happens first and its
failure is *not* swallowed: clearing the row while the URL still worked would leave a live
share nobody could find, let alone kill.

Both are also reachable from the API:

```
POST   /api/pov/managed/{id}/share          {"days": 7}   -> the link and its password
DELETE /api/pov/managed/{id}/share                        -> revoke
POST   /api/pov/managed/{id}/share/reveal                 -> the password again, audited
```

The URL is shown with a **copy** button rather than as a hyperlink. It is a customer-facing
address, and an accidental middle-click from this page is an SE opening a session as the
customer.

### A platform that cannot do this

The **Share** column reads `not supported` when the platform's `share_link` capability is
false, and the API refuses with a message pointing at PRA instead — the jump items the
wire-up created already reach these VMs, which is the better answer for a customer who
needs *audited* access rather than a URL. Skytap supports it; the column exists so the
second adapter degrades explicitly instead of 500ing from inside a job.

### Teardown

Destroy revokes the share **before** anything else, ahead even of the PRA jump items. It is
the only artifact somebody outside the account can be holding, so the window where it still
works is the one worth making shortest.

It never blocks the destroy. Deleting the environment removes its publish sets server-side
anyway, so a failed revoke costs a note in the job log rather than a stranded POV — and the
row is cleared regardless, so a destroyed POV is never left advertising a link.

---

## The auto-delete timer

A POV is the resource the auto-delete feature was most obviously missing. It bills for
every VM inside it, it sits on `suspend_on_idle` for months after the evaluation ends, and
until this release nothing in the codebase ever noticed it was finished.

This is **not** a second sweeper. A POV is a new *kind* in the one that already exists, so
every gate — the arming delay, report-only, the enforcement gate, the per-pass cap, the
exempt workgroups — is the same code that guards a cloud VM. The only thing POV-specific is
the default lifetime and the shape of the warnings.

### Turning it on

Two settings, both defaulting to off, and both needed:

| | |
|---|---|
| `resource_expiry_enabled` | the master switch, shared with every other kind |
| `pov_expiry_default_hours` | `0` (the default) means new POVs are not stamped at all |

`pov_expiry_default_hours` is deliberately separate from `resource_expiry_default_hours`.
The two describe different things: a cloud VM's default is a working-day sort of number,
and a POV is an evaluation that runs for weeks with a customer inside it. Sharing one
number would force a wrong default on one of them, and reaping a customer's lab is much the
worse half of that trade.

Note `resource_expiry_max_total_hours` (30 days, counted from creation) still caps the
total lifetime. If your evaluations run longer than a month, raise it — otherwise an
Extend will be clamped.

### Nothing that already exists is ever selected

`expires_at` NULL means **never**, and every POV that predates this release is NULL. So
turning the feature on acts on nothing; only POVs created afterwards, with a default
configured, carry a timer. This is the same property the rest of the feature has and it is
worth restating, because the failure it prevents is the loud one.

Past that, the same four gates stand between overdue and destroyed: the feature enabled, a
stamped expiry, the feature armed for an hour with a sweep report on disk, and enforcement
armed with report-only off.

### The warning ladder

Everything else in the feature warns **once**, `resource_expiry_warn_hours` before expiry.
For a VM that is right: one message, one owner, one decision.

A POV is not that. It has been running for weeks, whoever created it has moved on to the
next evaluation, and a single mail the day before a customer's lab disappears is a mail
that gets read afterwards. So a POV warns on a ladder:

**7 days → 3 days → 24 hours → 4 hours → 1 hour**

Each rung fires at most once. That is what `warned_stage_minutes` is for: `expiry_warned_at`
alone is a boolean latch, so the first rung would burn it and the other four would never
fire. The column records the *tightest rung already sent*.

Two consequences worth knowing:

- **A missed rung does not fire late.** The sweep runs every 30 minutes and a deadline can
  cross two rungs between passes. You get the tightest one crossed — "expires in about 4h",
  not a stale "expires in about 3 days" followed by a burst of four more.
- **Extending re-opens the whole ladder.** Both latches are cleared, so the new deadline
  gets its own full set of warnings rather than being permanently silenced by a rung
  crossed against a deadline that no longer exists.

### Extending

The **Expires** column on the POV page has an `edit` control: a number of hours to add, or
`never` to clear the timer outright. `never` needs `resource_expiry_allow_never`, exactly
as it does on `/inventory`, and a clamp against `max_total_hours` is reported rather than
applied silently.

It routes through the same `expiry_reaper.set_expiry` the inventory page uses, so a POV can
never end up with looser rules than a cloud VM.

### What reaping actually does

It enqueues the identical `pov_env_destroy` job the Destroy button creates. The reaper
never learns the teardown order — the share link, then the Entitle integrations, then the
Password Safe managed systems, then the PRA jump items, then the Gateway, then the broker
agent, then the environment. That ordering lives in one place and this feature cannot get
it wrong.

A POV is only ever reaped from `active` or `failed` — the same pair
`pov_env_service._ACTIONABLE` allows a human to destroy. `failed` is in that set
deliberately: a POV that died halfway through provisioning is the one that most needs
reaping, because whatever it did create is still billing. Anything mid-provision or
mid-destroy is refused, and an unrecognised status is refused too, so a state added later
can only ever make the reaper do less.

Clearing `expires_at` in the same commit is what makes it at-most-once.

---

## What this stack deliberately does not mount

Unlike the demo stack, neither service gets `/var/run/docker.sock` or the `runner_work`
volume. The demo stack needs the socket to launch sibling Ansible and kubectl/helm
containers. A POV instance runs Ansible **on the remote agent inside the customer
environment**, and Kubernetes is masked off entirely, so handing this stack the host's Docker
daemon would grant a great deal and buy nothing.

If you later want local Config Management runs here, the socket has to come back — and that
is a deliberate decision to make, not an omission to fix.

---

## Failure modes

**A nav link is missing and I know the flag is on.** Check the profile. `GET /api/features`
returns `install_profile`, and a masked feature reports `false` there whatever the stored
flag says. That is the mask, working.

**Settings gives me a 409 when I enable an integration.** Also the mask. The message names
the profile. That integration belongs on the other instance.

**I switched the profile and a feature did not come back.** The mask only subtracts, so
switching to `demo` stops masking — but the feature's own `*_enabled` flag still has to be
on. Check both.

**A POV is refusing with "no … tenant is configured".** Nothing is registered for that
product and the Settings singleton for it is blank or half-filled — a URL with no secret
is not a tenant, because it fails at the first call. Register one, or fill in the
singleton.

**A POV refuses with "N tenants are configured and none is the default".** Step 5 of the
resolver, working. Pick one on the POV itself, or mark a default.

**I edited `bt_api_host` in Settings and the POV still uses the old appliance.** The seed
is one-way: once a row exists it is the truth. Edit the tenant on the POV page.

**The Gateway install refuses with "its policy.yaml predates the Gateway grant".** The
broker was enrolled before this release. Pull a newer agent image onto the broker VM and
press **Broker** — see [upgrading](#upgrading-a-pov-that-predates-this).

**The Gateway registers online and every tunnel times out.** `privileged` is missing from
the broker's `gateway:` block, so the container has no `NET_ADMIN`/`/dev/net/tun`. A
re-broker rewrites the policy with it.

**A jump item was created but the session times out.** Almost always the Gateway it
routes through: check the item names this POV's own one and not the tenant's
appliance-wide default. The dashboard always uses the POV's, so a hand-edited item is the
usual cause.

**Wiring is refused with "this POV has no Gateway".** Working as intended — see
[which Gateway they route through](#which-gateway-they-route-through). Install one first.

**Wiring skips the Entitle half with "names no agent token".** Working as intended: a
POV's VMs are private and Entitle reaches a private target through an agent inside the
network, which this dashboard does not install. Deploy it and name its token on the tenant.

**Wiring skips the Entitle half with "no Entitle SSH key".** Its connector authenticates
with a key rather than a password. Use **ssh key** on the POV row.

**Wiring skips the Password Safe half with "no Resource Broker".** Working as intended:
without one the platform has no route to a private address. Install one from the Resource
Broker column, then re-wire.

**A VM shows as skipped with "the tenant names no <os> functional account".** Password
Safe derives the managed system's platform from the functional account, so one is needed
per guest OS. Set it on the Password Safe tenant.

**A VM shows as skipped with "did not report an OS".** The lab platform reported a blank
`os_family`, and guessing would build the wrong kind of jump item. Power it on and refresh
the POV so the platform re-reads it.

**The Resource Broker install hangs and the job times out.** Almost always a missing
`ZONE` — the installer prompts for it and nothing can answer. The dashboard refuses to
queue without one, so this means the zone was set to something the installer did not
accept; check the install log on the target.

**"no stored credential this dashboard can use".** The lab platform's credential box for
that VM is empty, or holds something the parser will not guess at. See
[there is no login field](#there-is-no-login-field-on-purpose).

**The Gateway check says `disconnected` but the container is running.** Look at the node
count. A rebuilt broker VM adds a node and PRA keeps the dead one; the Gateway is a
cluster, and old nodes are retired in the appliance, not here.

**Verify says the credentials are rejected and I am sure they are right.** For PRA, the
client id and secret come from an API account in the appliance (Management → API
Configuration) — a user login always fails there. For Password Safe, a token that succeeds
and a `SignAppIn` that fails means the OAuth client is fine and its linked BeyondInsight
user is missing, disabled, or has no Password Safe API access.

**Everything is refused with "not enabled" on a fresh POV instance.** `pra_enabled` and
`password_safe_enabled` default to `True` in code but the compose file sets them explicitly;
if you wrote your own env file, set them. `configured()` is separate again: PRA and Password
Safe also need their credentials before their endpoints do anything.

**No POV ever gets an auto-delete timer.** Both switches are needed:
`resource_expiry_enabled` AND `pov_expiry_default_hours` (0, the default, means don't
stamp). Environments created before both were set stay NULL forever — that is deliberate,
and the fix is the Expires column's `edit`, not a backfill.

**A POV expired but was not destroyed.** Read the sweep's job log on /jobs; it reports the
full target list whichever gate is shut. Most often the feature or enforcement has not been
armed for its hour yet, or `resource_expiry_dry_run` is still on.

**An Extend was shorter than I asked for.** `resource_expiry_max_total_hours` caps the
total lifetime from creation, and it defaults to 30 days. The response says `clamped`.

**The share link 400s with "has no share links".** The platform's `share_link` capability
is false. Give the customer access through PRA instead — the wire-up's jump items already
point at these VMs.

**"this POV has no stored share password".** The link predates the stored password, or the
password was cleared. Re-share: it publishes a new URL and a new password together, which
is the only way to get back to a consistent pair.

**The wizard still shows cloud steps.** You are on `demo`. The Purpose step drives the step
list, so re-run `/setup` and change it — nothing else needs re-entering.

**A cloud region key appeared in a POV instance's config.** It should not. The `pov` profile
skips the cloud **writes**, not just the screens, precisely because the wizard otherwise
persists every non-secret field whether or not you filled it in — leaving
`aws_region="us-east-2"` and friends behind. If you see one, it predates the profile or was
set by hand.
