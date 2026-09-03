# Wiring a POV's VMs into PRA, Password Safe and Entitle

> **Audience:** operator · **Profile:** `pov` · **Read this when:** a POV's VMs are up and you want them reachable, vaulted and time-boxed.

Part of [A POV Instance](README.md). Jump items, vaulted accounts and just-in-time grants, per POV and per tenant.

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
[installed inside the environment](gateway-and-broker.md#the-pov-gateway) — and never through the tenant's
appliance-wide default. That is not a preference. The default one lives on the customer's
side of the world and has no route into the POV's private network, so an item pointed at
it is created successfully, looks correct in the appliance, and times out at session
launch. That is the most expensive failure in this feature to diagnose, because
nothing reports it until somebody clicks.

So a POV with no Gateway is refused, up front, before any VM is touched.

### The tenant, not the singleton

The jump items are created against the PRA tenant [this POV is wired
into](standing-one-up.md#the-tenant-registry) — resolved with the POV's explicit id, so a POV whose tenant
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

### Password Safe reaches the VM through the Resource Broker — by zone, not by field

Password Safe does reach these guests through the Resource Broker slice 5b installs. What
arranges that is the broker's **resource zone** and the workgroup mapped to it: you create
the zone, add the workgroup to it, and install the broker into that zone. All of it is
BeyondInsight configuration, and **this dashboard performs no part of it.**

`application_host_id` is *not* that mechanism, though this document used to say it was. It
is a managed-system attribute naming another managed system that carries
`IsApplicationHost`. Three things say so:

* `cloud_database_service` onboards private databases *through* a Resource Broker and
  passes no `application_host_id` at all. It is the most live-tested Password Safe path
  here, and it works.
* `ps_vm_hook` passes whatever integer an operator typed into
  `passwordsafe_application_host_id`. Nothing derives one.
* BeyondTrust's own Skytap Password Safe POC runbook (SELab, rev 7.0, validated against
  PWS SaaS 26.2.0.1427) never mentions an application host anywhere. Its step 5 creates
  the zone, adds the workgroup, and installs the broker.

So `PovEnvironment.ps_application_host_id` is an **optional override** with one writer —
`POST /api/pov/managed/{id}/application-host` — and the wire-up sends `0` when it is
unset, which is what every other caller in this codebase does. It used to be a hard
precondition, which meant the Password Safe half refused on every POV ever created: no
code path has ever written that column automatically.

If a rotation later cannot reach a guest, check the broker's zone-to-workgroup mapping
first. The wire-up's job log says which of the two it used.

Two things come from the tenant rather than from Settings, because they are names inside
*that* tenant and mean nothing in another one:

| | |
|---|---|
| **Workgroup** | Where the managed system lands |
| **Functional account (Linux / Windows)** | Split by guest OS because Password Safe derives the managed system's **platform** from the functional account. One account cannot serve both |

A guest whose OS has no functional account configured is skipped with that reason — a POV
of only Linux VMs has no use for a Windows one, and demanding it would block a wire-up
that could have completed.

#### Leave the functional accounts blank and each POV creates its own

The two functional-account fields are **names of accounts, not labels**: a functional
account is the identity Password Safe authenticates to the guest AS in order to rotate its
managed account. A POV against a *customer's* Password Safe tenant usually has both
already, because they are the first thing anyone builds during onboarding. A
BeyondTrust-owned POV tenant on day one has neither, which is why blank means **create
one** rather than "skip Password Safe".

| The field | What happens |
|---|---|
| A name | That account is resolved and used as-is. Nothing is created, and **teardown never touches it** — it is the customer's |
| Blank | This POV mints its own, from the login its guests of that OS already use, and teardown deletes exactly that one |

**Nothing is stored.** The credential is read live from the lab platform per run — the same
`stored_credentials` box the Resource Broker install reads ([There is no login field,
on purpose](gateway-and-broker.md#there-is-no-login-field-on-purpose)) — used for the create call, and never written to this database. A credential that is
fetched cannot go stale, and a POV whose template password changed picks up the new one on
the next wire-up with nothing here to update.

**The account is per-POV, and the guests must agree.** One functional account serves every
managed system that names it, so before minting one the dashboard reads *every* guest of
that OS and requires them to report the same login. Guests that disagree are a refusal
naming which ones, because minting from one of them would rotate that guest and fail the
others — days later, on a schedule, which is the same delayed failure the Resource Broker's
application host exists to prevent.

The account is created on Password Safe's built-in **Linux** and **Windows** platforms. If
those platforms were renamed in the tenant, set
`pov_ps_functional_account_platform_linux` / `..._windows` to their names there — or just
create the account by hand and type its name on the tenant, which skips minting entirely.

The API account on the tenant needs permission to create functional accounts. Without it
the mint fails with that reason on the VM's own row, and the rest of the wire-up is
unaffected.

When the lab platform has a login for the VM, it is seeded as the managed account's
initial credential, so the first rotation replaces a password somebody knows rather than
one nobody does.

### The Entitle agent

Entitle's SSH connector reaches a **private** target through an **Entitle agent** running
inside that network — a Kubernetes deployment, named on the integration as its *agent
token*. A POV's VMs are on a private network by construction, so every POV integration is
a private one, and for a long time the agent was the one prerequisite this dashboard could
not satisfy.

It installs one now. **Tenants column → install agent**, beside the POV's Entitle tenant.
It runs through the POV's broker agent as a Config-Management job, so there is nothing new
on the wire and no agent image to rebuild — but the broker's `policy.yaml` has to carry
the grant, so a POV brokered before this shipped needs **Broker** pressed once to rewrite
it. The refusal says so.

What it does, on a **Linux** guest named on the POV (default `entitle`):

1. mints an agent token **in this POV's own Entitle tenant**, not the instance's;
2. installs single-node **k3s** (`--disable traefik --disable servicelb` — the agent is
   outbound-only and a POV VM that suddenly answers on `:80` is a change nobody asked
   for);
3. `helm upgrade --install entitle-agent` from the BeyondTrust-published chart repo,
   sized for one small VM;
4. records the token name on the POV, which is what makes the SSH wire-up stop refusing.

**The token is per POV, not per tenant, and that matters.** Two POVs for the same customer
share one Entitle tenant, and the tenant's `agent_token_name` can name only one agent.
Whichever POV was set up second would create integrations pointing at the *first* one's
network — and nothing would error, because Entitle creates them happily and the connector
simply cannot reach the host. So a POV's own agent always wins, and the tenant option
stays what it was: the answer for an agent you deployed yourself.

#### Sizing, and why the chart defaults do not fit

Worth knowing before you pick a host. The chart asks for **three replicas at 1 CPU and 1
GiB of *requests* each**, so on any VM you would give a POV the pods sit `Pending` forever
and nothing says why except `kubectl describe`. The install overrides that to one replica
at 250m/512Mi, and it turns off the Datadog log-shipping sidecar the chart runs even with
`datadog.enabled=false` — that one is a *native* sidecar, which additionally needs
Kubernetes 1.29+. Give the host **2 vCPU and 4 GB** and it is comfortable.

Two things it needs from the network, both of which fail in a way the job output names:
egress to `get.k3s.io` and `get.helm.sh` for the install, and to `ghcr.io` for the agent
image. An `ImagePullBackOff` is the second one.

**Not the broker VM.** The named host defaults to a VM called `entitle` and the guessed
fallback deliberately excludes the broker: k3s brings its own containerd and its own
iptables rules, and the broker is the one guest whose job is to keep alive the channel
this install runs over. Nothing stops you naming it on purpose.

**Teardown destroys the token**, and it has to. Entitle refuses to mint a name it already
holds and cannot read an existing value back, so a survivor would wedge the next POV that
derives the same name. A destroy that fails is reported on the job log with the name to
retire by hand rather than blocking the POV's deletion.

The SSH connector also needs a **key**. Entitle's connector authenticates with an SSH private key, not
the password the lab platform holds — so this is the one credential in the whole POV
wire-up that cannot be derived from something already there. Store the private half of the
key baked into your template's Linux guests with **ssh key** on the POV row; it is
encrypted like every other secret here and cleared at teardown.

Windows guests are skipped: the ephemeral-accounts app mints an account *over SSH*, which
a Windows guest does not answer.

Three things come from the tenant, because they are ids and names inside *that* Entitle
tenant: the **owner id**, the **workflow id** and the **SSH sudo user**. The fourth, the
**agent token name**, comes from the POV's own agent when it has one and falls back to the
tenant when it does not.

### Teardown

Destroying a POV removes the Entitle integrations **first**, then off-boards the managed
systems, then removes the jump items, then destroys the Entitle agent's token — all before
the Gateway, the broker agent and the environment itself.

The agent token goes *after* the integrations and not with them, because an integration
names the agent it routes through: reversed, Entitle would be left holding integrations
pointing at a token that no longer exists.

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


## Failure modes

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
per guest OS. Either name one on the Password Safe tenant, or leave the field blank and let
the POV create its own — see
[Leave the functional accounts blank](#leave-the-functional-accounts-blank-and-each-pov-creates-its-own).

**A VM shows as skipped with "this POV's <os> guests report N different logins".** A
functional account is ONE credential used against every managed system that names it, so
the dashboard will not mint one from guests that disagree — it would rotate the first guest
and fail the rest on a schedule, days later. The message names which guests hold which
group. Give them the same login, or create one functional account by hand in Password Safe
and name it on the tenant.

**A VM shows as skipped with "none of this POV's <os> guests offered a usable stored
credential".** The lab platform's credential box is empty for those guests, or holds
something the parser will not guess at — the same refusal `pov_credentials` raises for the
Resource Broker install, and for the same reason: a wrong username comes back from the wire
as an authentication failure, which reads as a bad password.

**A VM shows as skipped with "the Password Safe platform 'Linux' could not be resolved".**
The built-in platform was renamed in that tenant. Set
`pov_ps_functional_account_platform_linux` (or `..._windows`) to its name there, or name an
existing functional account on the tenant.

**A VM shows as skipped with "did not report an OS".** The lab platform reported a blank
`os_family`, and guessing would build the wrong kind of jump item. Power it on and refresh
the POV so the platform re-reads it.
