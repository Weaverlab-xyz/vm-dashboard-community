# Design — the demo cells we do not have, and the roles they would serve

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are deciding what to build after the network cell, or wondering why a role you expected to find has no page.

> **Status:** Design note, v1. **Nothing here is built.** It records a measurement, a
> definition, and three candidates in the order they are worth doing — so the next person
> to ask "what else could we demo?" starts from evidence rather than from a brainstorm.
> **Corrected after review, twice.** (1) An earlier draft called the Windows endpoint
> story blocked. It is not — EPM for Windows is a shipping product; what is missing is
> the dashboard integration. (2) It also called `itops` the weakest persona shipped and
> made "give it cloud cards" a sequenced work item. Measured, that is three of five cards
> needing a local VMware install and two working on cloud — a single missing card, not a
> project. §5 carries both corrections and §7 no longer lists the second. (3) §5b called
> the Workload Lab consumer blocked on the SPIFFE bridge. It is not — Workload
> Credentials authenticates a workload identity with no stored PAT, which is the whole
> point of the product, and §5b now says so.
> **Depends on:** the two cells that exist —
> [OT Demo Cell](../profiles/demo/ot-demo-cell.md) and
> [Network Demo Cell](../profiles/demo/net-demo-cell.md) — whose shared shape §2 extracts.

---

## 1. The measurement

Nine personas ship (`cloudops`, `devops`, `hypervisor`, `itops`, `ot`, `dba`, `security`,
`sre`, `netadmin`). Cross-referencing every `requires_flags` / `requires_any_flag` in
`services/personas.py` against `feature_flags.flags()` leaves **twelve flags no persona
card names at all**:

```
cloud_unmanaged_discovery_enabled   mcp_server_enabled          skytap_enabled
cost_explorer_enabled               portainer_enabled           vm_spend_cap_enabled
entitle_registration_enabled        pov_cloud_enabled           vm_suspend_schedule_enabled
entitle_user_jit_enabled            pov_environments_enabled    workload_lab_enabled
```

Strip the three POV-profile flags — `personas.applies()` answers false on a POV instance,
so those are not persona territory by design — and the rest cluster into two groups plus
some noise:

| Group | Flags | Reading |
|---|---|---|
| **Cost and governance** | `cost_explorer`, `vm_spend_cap`, `vm_suspend_schedule`, `cloud_unmanaged_discovery` | A coherent role with nobody telling its story. |
| **Non-human principals** | `mcp_server` | No persona owns *the thing that acts on its own*. |
| Noise | `portainer`, `workload_lab`, `entitle_registration`, `entitle_user_jit` | Covered adjacently — `sre` and `devops` cards reach the same surfaces through other flags. |

A flag with no card is not automatically a gap. It is a prompt to ask whether a *role*
is missing, and these two survive that question.

> **Since written:** the `finops` persona (§4) claimed five of these —
> `cloud_unmanaged_discovery`, `cost_explorer`, `vm_spend_cap`, `vm_suspend_schedule`
> and `entitle_user_jit` — taking the list from twelve to seven. The snippet in
> **Verification** reproduces the current list; the block above is kept as the
> measurement this note was written from, not as live state.
>
> One reading changed in the doing. `entitle_user_jit` was filed as noise above; it is
> not. It grants time-boxed access **to the dashboard itself**, including administrator,
> which turns out to be the governance role's strongest card rather than an adjacent
> one — "nobody is a standing admin of the thing that spends the money". `entitle_registration`
> stays orphaned and stays noise: registration happens at deploy time, by the thing
> being deployed, which is a `devops` concern.
>
> **Since updated again:** the `aiops` persona (§3) claimed `mcp_server_enabled`, leaving
> six. Of those, three are POV-profile flags that are not persona territory by design, so
> the substantive remainder is `entitle_registration`, `portainer` and `workload_lab` —
> the last of which is derived and deliberately unclaimable, since a card must name the
> constituent lab flag its target tab actually needs.

## 2. What a demo cell is, and what it is not

Both shipped cells share a shape, and naming it is the load-bearing part of this note —
because it is what disqualifies most ideas.

> **A cell is a place whose shape is the argument.**
> The OT cell's subnet has no egress, and *the air gap is the demo*. The network cell's
> device has no inbound rule and no `useradd`, and *being unreachable by normal means is
> the demo*. In both, the environment is not scenery for a feature tour — it is the
> premise, and the PAM layers are the resolution.

Two consequences follow, and both have teeth:

- **A capability is not a cell.** An estate-wide story — cost, discovery, policy — has no
  place to stand up. It can be a persona; it cannot be a cell. This is what rules out the
  strongest *role* candidate in §4.
- **A cell must be cheap because the deploy path already exists.** The network cell is a
  fraction of the OT cell's size for one reason: it provisions no Web Jump and no tunnel,
  so `gcp_vm_service._run_destroy` needed no new arm and teardown stayed inherited
  (`services/netcell_service.py` argues this at length). **A proposed cell whose wiring
  needs its own teardown path should be costed as the OT cell, not the network cell.**

## 3. Candidate A — the agent cell (recommended) — **BUILT**

**Role:** AI / agent platform engineer. **Owns:** the things that act without a person
at the keyboard.

`devops` and `sre` are humans *running* pipelines and clusters. Nothing in the registry
owns the non-human principal itself, and `mcp_server_enabled` is named by zero cards.

**The premise:** an autonomous worker in an egress-controlled subnet that **holds no
standing credential**. It fetches one per task, every action is recorded, and the grant
can be pulled mid-run. The absence of a standing credential is the demo, exactly as the
air gap is the OT cell's.

**Why it is first:**

- It is the privileged-access question with no current answer. Ask a room how their
  agents authenticate and the honest reply is a long-lived key in an environment
  variable.
- The machinery mostly exists. **`remote_agents` is already a container that runs inside
  a private network and dials out** ([Remote Agents](../remote-agents.md)) — that is an
  agent in a box with the network story solved. `workload_credentials`, the SPIRE lab and
  `cloud_functions` give it an identity.
- It fits the cheap shape: a Shell Jump plus a workload identity. No Web Jump, no tunnel,
  so teardown stays inherited.

**Scope it honestly.** The MCP server at `/mcp` is **read-only access to dashboard data**
([MCP Server](../integrations/mcp-server.md)). It demonstrates an AI client *reading* an
estate; it is not an agent with privileged access to infrastructure, and a card implying
otherwise would be overclaiming. The cell is new work that can *reuse* the MCP server as
one surface, not a wiring exercise over it.

**The worker is baked into the image**, the same way §5's EPM agent and the OT cell's
DMZ broker are. The deploy paths have no user-data hook, so anything that must be running
at first boot is baked at Packer time; anything needing a fresh secret — a registration
token, a workload identity — is a post-deploy job, because a token baked into an image
has expired before the image is used. That is not a constraint peculiar to this cell: it
is the rule both shipped cells follow, and it is why `provisioners/` exists.

**Open question, now answered:** the worker **drives this dashboard's own MCP server**.
It reads the estate on a loop and names, in one line, the SPIFFE ID it proved and the
token it spent. Reading rather than acting, deliberately: the argument does not need the
blast radius to be frightening, it needs nobody to be able to say what the blast radius
*is*.

**Shipped as the agent cell** — [`agent-demo-cell.md`](../profiles/demo/agent-demo-cell.md),
`examples/playbooks/agent/`, `services/agentcell_service.py`, and the `aiops` persona.
Three things the build settled that this section had guessed at:

- **The SVID does not authenticate to `/mcp`.** The MCP server takes a Bearer PAT and has
  no mTLS path, so identity and authorization stay two things and nothing mints one from
  the other. Closing that gap needs the Password Safe SPIFFE SVID plugin, whose
  configuration question `spire_lab_service` already records as unresolved. The cell shows
  both halves in one log line and names the gap rather than papering over it.
- **The cell attaches; it does not create.** Same call the SPIRE lab made, same reason
  (`resolve_host` re-derives the host from deploy rows), same payoff: teardown is
  inherited and the cell owns none.
- **The strongest refusal was not obvious from here.** The cell refuses to mint an agent
  token against an administrator. Every MCP tool applies the token user's RBAC, so the
  token user *is* the agent's blast radius — and picking an admin would quietly make the
  whole demo argue the opposite of its point.

## 4. Candidate B — cloud governance (a persona, deliberately not a cell) — **BUILT**

**Role:** FinOps / cloud governance. **Owns:** privileged infrastructure nobody is
accounting for.

Four orphan flags sit together — `cost_explorer`, `vm_spend_cap`, `vm_suspend_schedule`,
`cloud_unmanaged_discovery` — and `admission_control` / `resource_expiry` are touched only
lightly by `cloudops` and `security`. "What privileged thing exists that nobody
onboarded, and what is it costing?" is a real role with real machinery behind it.

**It gets no cell, by §2.** The story is the estate, not a place. Standing up an
environment to demonstrate discovery would be staging the answer — you would be
discovering the thing you just built, which proves nothing about a real estate.

So: a persona whose cards point at existing surfaces. Cheap, and it closes five orphan
flags at once.

**Shipped as `finops`** — label *FinOps / cloud governance*, page at
[`personas/finops.md`](../profiles/demo/personas/finops.md). Five cards: unmanaged
discovery, the spend cap, Entitle user JIT, the costs page, and suspend schedules.

Two things the build changed about the analysis above:

- **Entitle is the spine, not a side note.** The role reads as a cost story until you
  notice that `entitle_user_jit` governs the dashboard itself. Standing access and
  standing infrastructure accumulate for the same reason — removing them is nobody's job
  — so one persona covers both, and the Entitle card is the one to close a conversation
  on. It is positioned third for that reason: between the two halves it joins.
- **Only two of its six flags are wizard toggles.** `preset_flags` may only name what the
  Features step renders, so the spend cap, suspend schedule, unmanaged discovery and
  Entitle user JIT are configured in Settings and reported by the cards as `needs_flag`.
  That is the arrangement `vdesktops` and `notifications` already have, and it is worth
  knowing before designing any persona around Settings-only capabilities.

**Counter-argument that no longer applies, kept for the record:** `cloud_unmanaged_discovery`
might have belonged to `security` as a missing card rather than to a new role. It went to
`finops` because the discovery story is about *what is accumulating*, not *who has
access* — but a `security` card pointing at the same listing would not be wrong.

## 5. Candidate C — a Windows endpoint cell

**Role:** `itops` already exists. This is a cell for an existing persona.

The gap is sharper than "no cloud VDI" — cloud VDI is built and reasonably mature
([Virtual Desktops](../virtual-desktops.md), preview). The gap is that
**"remove local admin from a Windows endpoint and elevate per-application" — among the
most recognisable demos BeyondTrust has — has no home in this dashboard**, because
`epml_enabled` is the only EPM flag in the registry and
[`integrations/epml.md`](../integrations/epml.md) is EPM **for Linux**.

**To be clear about what that is and is not.** EPM for Windows is a shipping BeyondTrust
product and there is nothing preventing its use — what is missing is the *dashboard
integration*, not the capability. This is a build, not a wall, and it is a smaller build
than it looks, because **every surrounding piece is already here**:

| Piece | Already exists |
|---|---|
| Windows image preparation | `provisioners/beyondtrust/bt-ready-windows11-vdi.ps1` (multi-session AVD) and `bt-ready-windows.ps1` (Server Core), both run as Packer PowerShell provisioners. |
| Windows post-deploy configuration | `runners/ansible-winrm` is the dashboard's **default** runner image, carrying `pywinrm` + the NTLM backend precisely so a WinRM target works out of the box on every runner. |
| The agent-activation pattern | EPM-L already does exactly this — see below. |
| Windows seats with credential injection | Azure, today. |

### Bake the agent, activate it after

The agent goes **into the image**, and this is a settled pattern rather than a new idea.
EPM-L states the constraint and the resolution in one sentence
([`integrations/epml.md`](../integrations/epml.md#getting-an-installation-token)):

> Because the package is installed at build time but activation can't be — a token
> expires hours after issue, so one baked into an image is already dead — activation is a
> post-deploy step.

So: the EPM agent is baked by the Packer provisioner, and a **short-lived installation
token is minted server-side at execution time**, bound to a run variable and scrubbed
from the job output. Nothing expired or sensitive is left in the image or the job record.
The OT cell's DMZ broker bakes the Entitle agent's chart on the same principle and
installs it afterwards with its own job (`ot_agent_install_job_id`).

That matters here for the reason it mattered to the network cell: **the deploy paths have
no user-data hook**, so anything that must be present at first boot is baked, and
anything needing a fresh secret is a post-deploy job. Both halves already have a
precedent to copy.

### What a Windows EPM integration would need

Roughly the EPM-L integration's shape (`services/epml_sync_service.py`, `api/epml.py`,
`epml_sync` job): list and build agent packages, sync them to the asset backend, issue
installation tokens. A second product on the same Pathfinder gateway rather than a new
subsystem.

### What `itops` can and cannot tell today

An earlier draft of this section called `itops` the weakest persona shipped and treated
"give it cloud cards" as a work item. **That was overstated**, and the correction matters
because the claim was steering what to build next.

Measured rather than asserted — `itops` ships five cards:

| Card | Needs | Reachable on a cloud-only instance |
|---|---|---|
| Least privilege on a Linux endpoint | `epml_enabled` | **yes** — no cloud or hypervisor requirement |
| Support a user on a virtual desktop, recorded | `vdesktops_enabled`, `pra_enabled` | **yes** — Virtual Desktops provisions on all three clouds |
| Rotate a workstation local-admin password | `password_safe_enabled`, `vmware_enabled` | no |
| Power a workstation on and off without RDP | `remote_agents_enabled`, `vmware_enabled` | no |
| Remote support with no VPN | `pra_enabled`, `vmware_enabled` | no |

So three of five need VMware **Workstation** — a local desktop hypervisor, and
`_DEMO_ONLY` — and two work on cloud without it. "Three cards need a local VMware
install" is the accurate statement. "The persona is mostly unavailable" is not: the two
that work are the EPM and the recorded-support stories, which are the two an IT audience
came for.

**What is genuinely thin** is narrower: the *Windows* endpoint story on cloud. Azure is
the only cloud with Windows seats and credential injection, and the VDI card above does
not lean on that specifically. One card about an Azure Windows seat with credential
injection into a Remote RDP jump item would add something real.

That is **one card, not a project**, and it does not belong in the ordering below as a
peer of the cells. It is worth doing whenever someone is next in `personas.py` anyway.

## 5b. The Workload Lab's consumer, and the static secret it removes

**Corrected three times.** An earlier draft called this "the cheapest remaining item". A
second called it *blocked* — reasoning that every unbuilt tab vaults its credential where
a consumer would need another credential to reach it, and concluding that the only way out
was the SPIFFE bridge §3 records as unresolved. That conclusion was wrong, and wrong in the
direction that stops work happening: it reasoned about Password Safe without checking what
**Workload Credentials** is for.

The third correction is smaller and sharper, and it is recorded here because it is the one
that makes the mechanism general rather than clever. Two things the second draft got wrong:

1. It left vague *what* Workload Credentials serves, waving at "its own static store, a
   dynamic credential, or a `bt_safe://` reference". The concrete answer is better: WC
   holds the **Password Safe API client id and secret**, and the workload uses that pair
   to call Password Safe.
2. It treated the identity as an Azure application identity. **It is not Azure-only.**

### The chain that needs no static secret

`services/workload_credentials_service` states its half in its own docstring: *"Two auth
modes, and the second one stores nothing."* `wlc_auth_mode` is either `pat` — a stored
token — or **`workload`** (spelled `entra` before §5c), where the platform vouches for
the machine and no PAT exists:

1. the workload asks its platform for **its own identity token**;
2. it presents that to **Workload Credentials** in place of a PAT, with
   `X-BT-Service-Name` naming which registered Workload Identity it satisfies;
3. WC hands back the **Password Safe API client id and secret**;
4. the workload signs in to Password Safe with that pair
   (`POST Auth/Connect/Token` + `SignAppIn`, exactly as `services/ps_api_service._sign_in`
   does) and **requests** the credential it actually needs.

Everything the workload is *configured* with — site id, service name, audience, base URL,
the two WC secret **names**, the account id — is non-secret. **Nothing is stored on the
host.**

### Why the fourth step is the point, not an extra hop

Password Safe authenticates an application with a client-credentials pair. That pair is a
standing credential and always was, so the question was never whether one exists — it is
**where it lives**. Putting it in Workload Credentials makes WC a *bootstrap for the
vault* rather than a second vault beside it, and that distinction is what makes the shape
general: the workload can then reach anything Password Safe governs, not merely what
somebody remembered to copy into WC.

And what Password Safe governs, it governs here too. The retrieval is a **recorded request**
with a duration and a reason (`POST Requests` → `GET Credentials/{id}` → `PUT
Requests/{id}/Checkin`), it can be made to require approval, and the credential behind it
rotates on its own schedule. A PAT in a file has none of those properties and never will.

This is the suite solving a new problem with a combination of old and new. Password Safe
(old) holds and governs the secret. Workload Credentials (new) brokers the way in against
an identity the platform vouches for. The workload holds nothing. Neither product does
this alone, and the seam between them is exactly what a competitor holding one of the two
cannot show.

### The identity is not Azure-only

Federating a non-human identity against an OIDC issuer is available in **all three clouds**,
and from SPIRE besides. The worker takes `--identity-platform`:

| Platform | Where the token comes from | Note |
|---|---|---|
| `azure` | IMDS, or `IDENTITY_ENDPOINT`/`IDENTITY_HEADER` where the runtime injects them | the branch the dashboard's own client uses |
| `gcp` | the metadata server's `instance/service-accounts/default/identity` | returns the token as plain text, not JSON |
| `aws` | the projected token at `AWS_WEB_IDENTITY_TOKEN_FILE` (IRSA) or `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` (Pod Identity) | EC2's IMDS issues SigV4 credentials and a signed identity document, **not** an OIDC JWT — plain EC2 needs an issuer, see below |
| `spire` | a JWT-SVID from the agent already on the host | needs no cloud at all, and the Workload Lab already publishes the trust domain as an OIDC issuer (`spire_lab_service`, `OIDC_PORT`) |
| `file` | any other projected token on disk | the Kubernetes ServiceAccount token every cluster mounts |

The `spire` row is the one that ties this back to the lab. A bare-metal host has no cloud
metadata service to ask, and the lab's OIDC discovery provider is exactly the issuer that
answers for it — so the same mechanism covers the estate's clouds *and* the hardware
sitting in a rack, which is the shape a network or OT cell actually has.

`auto` detects by marker — an injected env var or a projected file — and **refuses rather
than guessing** when the host declares nothing. No marker distinguishes a bare Azure VM
from a bare GCE one, and 169.254.169.254 is both their metadata addresses; probing means a
request that has to time out to say no, on the host most likely to be neither.

### What was built

The worker gained two more token sources and a platform selector.

| `--token-source` | What it holds | What the vault sees |
|---|---|---|
| `file` (default) | a 0600 PAT on disk — a static secret, honestly labelled | nothing |
| `wlc` | nothing | nothing — WC serves the PAT from its own store |
| `ps` | nothing | a recorded credential request, with a duration and a check-in |

The install play refuses a `wlc` or `ps` worker missing any of its non-secret
configuration, naming each value — there is no reason to be vague about something that is
not a credential — and it **removes** any token left behind by a previous `file`-sourced
install, because "nothing is stored on this host" must not be contradicted by a file in
`/etc`.

One bug was fixed along the way: the unit's `ExecStart` was built from backslash
continuations with `{% if %}` blocks between them, and the `file` branch ended without a
trailing backslash — silently truncating the command so `--spiffe-socket` and `--interval`
never reached the worker. It is one folded line now, with no continuations to get wrong.

The link from the earlier draft stays: an agent is still made *answerable for* one Workload
Lab credential, and that remains a governance record rather than a capability. What changed
is that the worker can hold its own credential without one being left on a disk for it.

### Still unproven

Three named gaps, none of them a property of the design:

* ~~**The dashboard's own WC client is Azure-only.**~~ **Fixed — see §5c.**
* **Nothing here has been run live.** No Workload Credentials tenant, no registered
  Workload Identity, no federation trust. The client paths are unit-tested and that is all
  they are.
* **The Password Safe account has to exist and be requestable.** The `ps` source needs an
  API-enabled managed account, the Requestor role, and an access policy that auto-releases
  — the same out-of-band prerequisites `docs/integrations/password-safe.md` already
  records for every other request path.

So `file` stays the default, and every line the worker logs names which mode produced its
token.

## 5c. The dashboard's own client, and the hosting page that settles it

§5b built five identity platforms into the agent **worker** and recorded, honestly, that
the **dashboard's own** Workload Credentials client still had one. That asymmetry is now
closed, and the argument that closed it is worth keeping because it is stronger than the
one §5b used.

§5b argued from capability — *OIDC federation for non-human identities is available in all
three clouds*, which is true and is not quite a reason to build anything. The reason is in
`docs/cloud-hosting.md`, which has said all along that this dashboard runs as a managed
container on **Azure Container Apps, GCP Cloud Run or AWS ECS**. Wiring only Azure left two
of the three *documented hosting options* unable to use the mode that stores nothing —
not because the mechanism belonged to Azure, but because nothing here asked the other
platforms for a token. A feature the install guide offers and the auth path cannot serve is
a gap with a date on it, not a preference.

The module's own comment had the reasoning the wrong way round: *"Only the Azure one is
wired here, because the thing being authenticated is an Azure-hosted container."* The
container is Azure-hosted **in the reference install**. The page describing the other two
was already written.

### What changed

| | Before | After |
|---|---|---|
| Mode | `entra` | `workload` (`entra` still accepted on read) |
| Platform | implicit | `wlc_identity_platform` — `azure`, `gcp`, `aws`, `file` |
| Audience | `wlc_entra_resource` | `wlc_identity_audience` (old key read behind it) |
| Pathfinder registration | Azure Entra ID | Azure Entra ID, or **Custom IDP** for the rest |

Three details the build turned up:

* **GCP returns the token as plain text**, not a JSON envelope, so the expiry has to come
  out of the JWT's own `exp` claim. Nothing is verified in doing so and the code says why:
  the only consumer is the re-fetch memo, and Pathfinder is what holds the issuer's keys.
* **The file platforms are never memoised.** The platform rotates a projected token in
  place, the read is local, and a memo would be the only thing capable of serving a stale
  one.
* **ECS cannot use this mode at all**, and that is the one real limit. An ECS task — like a
  plain EC2 instance — gets SigV4 credentials and a signed instance identity document, and
  no endpoint there issues an OIDC token. Only EKS projects one, via IRSA or Pod Identity.
  A dashboard on ECS stays on a stored PAT; the panel says so before the choice is made and
  the error says so after.

### Still unproven

Azure was run end to end on 2026-09-15 and remains the only platform that has been. GCP
and AWS have unit-tested client paths and no live Pathfinder registration behind them. The
hosting page and the integration page both say which is which.

## 5d. The consumer that asks permission

§5b said the Workload Lab's four credentials had no consumer, and that the hub called the
candidates — *"a pipeline, a broker or a cluster"* — hypothetical.

**That was not true of the Kubernetes tab, and the tab's own page says so.**
`examples/playbooks/k8s/ci-deploy-with-ps-token.yml` and `ci-read-with-ps-token.yml` ship
with it and are *"the first Kubernetes plays in this repo that authenticate with something
they fetched themselves"*, with the 403 refusals written as in-play assertions precisely so
they cannot be skipped. Reading §5b's framing onto that tab was a third correction in the
same direction as the first two: an under-reading of what already existed.

### What the agent actually adds

Not a consumer. A consumer **that holds nothing in order to retrieve**.

| | The shipped plays | The agent cell |
|---|---|---|
| Who retrieves | an Ansible run, as the dashboard | a principal on its own host |
| What it holds to do so | `PASSWORD_SAFE_CLIENT_ID` + secret, from the run's environment | nothing — workload identity → Workload Credentials → the client pair |
| When | operator-triggered | on request, and only after a person approves |
| What it proves | the RBAC scope | the scope **and** that the retriever had no standing credential |

`workload_k8s_service` is candid about the axis it loses on: *"The vault authenticates
whoever can retrieve. Anyone who can retrieve **is** the workload, as far as this mechanism
can tell."* The agent **narrows** that — what reaches the vault is no longer transferable —
and does not close it. Saying it closes it would be the one way this demo becomes a lie.

### The shape, and why approval-gated

A bounded episode rather than a loop: ask, wait, probe, release. One request/check-in pair
in the audit trail with a person's approval in the middle, instead of a stream of
retrievals nobody can point at.

The cell's existing closing beat is a revoke — pull the PAT and the worker stops. This one
is better, because it happens *before* anything: an AI agent that asks for access to a
production cluster and cannot proceed until somebody says yes.

### Three traps, each already written down in this repo

* **"Awaiting approval" arrives as a soft-failure STRING**, not a status code —
  `btapi_service` learned it: *"It was not possible to get a credential for Request ID:
  N"*, returned in the credential position on a successful call. Treating it as failure
  makes the request unwaitable; treating it as a value hands a sentence to an API server
  as a bearer token.
* **Never check in while pending.** `ps_api_service._request_credential` checks in when
  the credential does not come back — right for auto-release, and exactly wrong here: it
  cancels the request a human is being asked to approve. But it must still give up, or an
  abandoned request holds the account's slot and the next attempt trips the concurrent cap
  (4035) reporting the cap instead of the approval it waited on.
* **The credential is a JWT, not a `vmcli_` PAT.** The worker's `password_safe_credential`
  hardcoded that prefix, which had to become a parameter — and which had been catching the
  soft-failure sentence by accident. That guard is explicit now.

### What was built

`kubernetes` joins `cloud` in `LINKABLE_MECHANISMS`, with a new `SPENDABLE_MECHANISMS`
keeping the two kinds of link apart — one confers accountability, the other confers
access, and a response that read the same for both would mislead either way. The stale
refusal text and the `AgentCell` comment that said no worker could spend any of them are
corrected rather than left as fossils.

The row records the **request** — id, state, timestamps, result — and never the
credential. `test_workload_lab_governance`'s no-credential-on-row rule now covers
`AgentCell` too, rather than relying on that being obvious.

### Why `certificates` is still not linkable

The refusal an operator gets is deliberately short, so the reasoning lives here. It is
**not** that a certificate is unreachable in principle — it is that the bundle is a
Secrets Safe **file secret**, and this worker only knows how to fetch a managed-account
password. Three separate things stand between the two, and only the first is common to
both certificate packages:

1. **A file secret has no content field to read.** `ps-cli secrets get` projects a
   per-type field set, and the file one carries `FileName` and `FileHash` and nothing
   else — no `Text`, no `Password`. `--decrypt` cannot help, because it only adds a
   query parameter and there is no payload field for it to fill. The body comes from a
   separate call that takes **only a GUID**, so even the happy path is two round trips.
   See [Password Safe → Troubleshooting](../integrations/password-safe.md#troubleshooting).
2. **On the leaf package it is binary — which rules out ps-cli, not the API.** `bundle`
   defaults to `Pkcs12` for `Certificate` and `PemBundle` for `Subordinate CA`. The
   endpoint returns `application/octet-stream` and is byte-faithful; every route ps-cli
   offers is not, because both `download-secret-file` and `raw` print `response.text`. A
   PEM bundle is ASCII and survives that; a PKCS#12 does not. So the subordinate package
   needs a path nobody wrote, and the leaf package needs that path to bypass ps-cli and
   call the endpoint directly. **That is a small ask of this worker specifically** — it
   already speaks Password Safe REST for `Auth/SignAppIn`, `Requests` and `Credentials`
   and holds no static credential doing it, so one more GET is the same shape it is
   already built around, not a new capability.
3. **It wants BeyondInsight ≥ 26.1 regardless.** Below 26.1.0.878, file secrets
   downloaded through the API came back larger than the original and did not match the
   web console's copy, so a non-human identity would have retrieved a corrupt bundle no
   matter how careful the client was. That floor is already a
   [Certificate Lab prerequisite](../workload-lab/certificate-lab.md#password-safe).

None of this is a structural objection of the kind `cloud` has — a certificate is a
credential a workload legitimately spends, and an agent answerable for one is a coherent
demo. It is genuinely unbuilt work, which is why the refusal says so rather than implying
the tab is off-limits.

### Still unproven

No Password Safe tenant, no cluster, no approver. The client paths are unit-tested against
a fake gateway and that is all they are. **Without an approval policy there is no wait**,
and the best beat silently does not happen — so the worker logs which path it took.

## 5e. The third control surface

§5d gave the agent a credential it must ask permission for. This gives it one **nobody
can take away**, and the arc is the reason to build it at all:

| Episode | Credential | How you take it away |
|---|---|---|
| the loop | its MCP PAT | **revoke it** — the worker stops mid-poll |
| cluster access | a Workload Lab token | **gated at retrieval**; once released it lives out its TTL |
| certificate use | a PKCS#12 identity | **neither** |

`docs/workload-lab/certificates.md` states the third position plainly: *"No revocation
checking. The plugin consults neither CRLs nor OCSP. Short lifetimes are the mitigation,
and that is a deliberate design position."* So the closing beat is deliberately
uncomfortable — disable the managed account, which revokes the certificate, run the agent
again, and it works. It stops when the certificate expires, not when somebody takes it
away. A room that has just watched a revoke kill an agent understands immediately why
that matters.

### The consumer correction, for the third time — and it was a smaller gap than stated

Like Kubernetes, this tab already had consumers: `ci-fetch-cert.yml` and
`nginx-mtls-endpoint.yml`, with the page calling the step that runs them *"the step
usually skipped, and the only one that proves anything"*. They authenticate with a
Password Safe client pair supplied to the run; the agent's contribution is the same narrow
one it was for Kubernetes.

**And my own refusal text overstated the barrier.** It said the tab *"writes a PKCS#12
into Secrets Safe rather than a managed-account password, and this worker has only the
managed-account retrieval path"*. Half of a certificate identity **is** a managed-account
password — the passphrase — and the worker could always fetch it. The gap was the
**bundle** alone.

### The retrieval decision, which took three passes to get right

Worth recording in full, because each wrong turn was confidently argued.

**Pass one** recommended a Secrets Safe REST client and rejected `ps-cli`, on the grounds
that the binary *"would need its own credential configuration, which is the standing
secret this cell exists to argue against"*. **False**, and one grep settled it:
`secrets_backend_service._pscli_env` maps `PSCLI_CLIENT_ID` / `PSCLI_CLIENT_SECRET` from
the **environment**, and they are the same OAuth2 pair the worker already fetches from
Workload Credentials. There is no second credential to configure.

**Pass two** therefore chose `ps-cli secrets get -d` — the path the repo already runs
against a live tenant. Wrong about the secret: a file secret has no content field for
`get` to return. `-d` cannot rescue it, because the flag only adds `decrypt=true` and
there is nothing in the projection for it to fill.

**Pass three** moved to `ps-cli secrets download-secret-file`, the verb that does read a
file attachment. Right about the verb and **still wrong about the transport**, which is
the one that would have shipped: every route ps-cli offers decodes the body to text before
the caller sees it — the library hands back `response.text`, and `raw` falls through its
JSON parse to print `response.text` too. The endpoint itself is byte-faithful; ps-cli is
not. A PEM bundle survives that. **A `.pfx` is corrupted rather than refused.**

That last one is the dangerous shape. The corruption is silent where it happens and loud
three steps later, at the DER check, which reports *"this does not look like a PKCS#12"* —
sending somebody to look for a wrong bundle in Secrets Safe when what is wrong is the
transport. `services/secrets_backend_service._read_bt_file_secret` refuses binary payloads
for exactly this reason and names the way out: call the endpoint directly.

**So the worker calls `GET Secrets-Safe/Secrets/{id}/file/download` itself.** Two calls,
because the download takes an id and an operator knows a reference — resolve, then fetch.

**And the costs the ps-cli route carried are simply gone**, which is worth stating because
the previous draft of this note argued both of them away at length:

* **the unpinned pip package** on every agent host — `beyondtrust-bips-cli` moves under a
  rebuild, and the opt-in install flag that existed to contain it is deleted;
* **the client pair passing through a subprocess environment** — a tension against this
  cell's own rule against env vars that had to be argued rather than resolved. There is no
  subprocess now, so there is nothing to argue.

What replaces them is smaller and better: Secrets Safe is part of Password Safe, so the
session opened for the passphrase reaches the bundle unchanged. **One sign-in, both halves
of one identity.**

### The one place something touches disk

**The bundle is a file secret, not text.** An earlier draft had it coming back as a
string and being held in memory, on the grounds that keeping a credential off disk was
worth something. That was wrong about the secret, and it was also the weaker design:
`openssl` needs a file to open a PKCS#12 and Python's `ssl` needs file paths for a client
certificate, so a blob in memory would have been written out three lines later anyway.

So there is no version of this that stays off the filesystem, and the episode bounds it
instead of pretending otherwise: **one** `0700` temporary directory, opened before
anything is fetched, holding the bundle, the certificate and the key; `0600` on the key
and on the bundle; the passphrase through `PFXPASS` rather than argv, mirroring
`ci-fetch-cert.yml`; and the whole directory removed on the failure path too. The probe
takes that directory as an argument rather than making its own, which is what makes "one
guarded place" a fact rather than a manner of speaking. It is the one unavoidable
exception to "nothing is stored on this host", and the code says so where somebody would
otherwise find it and conclude the cell is careless about the thing it argues for.

### The human has to be real, not assumed

The passphrase goes through §5d's approval-gated request, so a person decides before the
agent gets an identity. But **whether Password Safe actually asks anybody is the access
policy's decision, not this code's** — and on an auto-releasing policy the episode would
fetch, probe and print a success line indistinguishable from the approved one. The
operator would conclude a gate was in force; the audit trail would show a request nobody
was asked about.

That is the one way this demo can mislead, so the worker refuses it: released on the first
ask means no person was consulted, and the episode stops with exit code 5 naming the
policy to change. It cannot *create* the gate — that is BeyondInsight's — but it can
decline to pretend there was one. `--no-require-approval` opts out, deliberately loudly.

**The same check now covers the cluster episode.** §5d's page claims the agent "cannot
authorise its own access", and on an auto-releasing policy that claim was equally untrue
there — so this is a correctness fix to an existing statement rather than a new rule for
one episode.

It matters most here, though, and for the reason this whole section is about: a
certificate cannot be revoked out from under the agent. With the PAT you can change your
mind afterwards; with the cluster token you can wait out a TTL you chose. **Here the
approval is the last decision anybody makes about that identity until it expires.**

### Still unproven

No CA, no Password Safe tenant, no mTLS endpoint. The probe is exercised against a
generated CA, a real PKCS#12 and a local mutual-TLS server in
`tests/test_agentcell_cert_episode.py` — which is considerably more than the other
episodes get, and still not a live run. **One shape is genuinely unverified**: how
`Secrets-Safe/Secrets` resolves the reference. The worker sends a reference containing `/`
as `path` (with `separator`) and one without it as `title`, mirroring how the
`beyondtrust.secrets_safe` lookup resolves `folder/title`. That is a reading of a
documented endpoint rather than a guess at a flag — the previous draft's unverified value
was a ps-cli output flag nobody had run — and the refusal names which of the two lookups
came back empty, so a tenant that disagrees says so in one line.

**And a floor §5d already records applies here too:** below BeyondInsight 26.1.0.878, file
secrets downloaded through the API came back larger than the original, so this episode
would retrieve a corrupt bundle however careful the client is. It is already a
[Certificate Lab prerequisite](../workload-lab/certificate-lab.md#password-safe), which is
why it is not a new one — but it is the version this episode silently depends on, and the
DER check is not a guard against it: a too-large bundle still starts `0x30`.

The worker also checks the downloaded bytes are DER before treating them as a PKCS#12, and
names PEM specifically when it sees it: PEM is a perfectly good file secret and a
perfectly useless one here, and `openssl` would otherwise complain about the passphrase —
sending somebody to debug the wrong half of a two-half identity.

## 6. What is deliberately not proposed

- **A vendor-access cell.** Third-party access into a network they should not have is
  already the OT cell's flagship (`profiles/demo/personas/ot.md`). A second cell making
  the same argument for IT would split the story rather than add one.
- **A database cell.** `dba` is well covered and the databases are real; a purpose-built
  environment would add scenery, not a premise.
- **A Kubernetes cell.** `sre` has five cards and the clusters are genuinely provisioned.
  Same reasoning.

## 7. Suggested order

1. ~~**Cloud governance persona**~~ — **done.** Shipped as `finops`; see §4.
2. ~~**The agent cell**~~ — **done.** Shipped with the `aiops` persona; see §3.
3. **Windows EPM integration, then the Windows endpoint cell** — the last item on this
   note, and the biggest demo payoff per unit of new thinking. EPM-L is a working template
   to copy rather than a design to invent, and the image prep, the WinRM runner and the
   bake-then-activate pattern are all already here.

The agent cell was built before the Windows one despite this list's earlier ordering, and
the reason is worth recording: the risk it carried was **unproven infrastructure**, which
a preview flag and an honest "not yet run against live infrastructure" note handle. The
Windows item's risk is a **missing integration**, which nothing but building it removes.
Shipping the one whose risk could be bounded first was the cheaper order.

**`itops` cloud cards are not on this list**, and an earlier draft was wrong to put them
there — see §5. The gap is one card, not a piece of work worth sequencing.

**The Workload Lab consumer is not on this list**, because it is built — see §5b. Note
that the section had to be corrected twice, the second time reversing the first: it was
called blocked on the SPIFFE bridge, and it is not. Workload Credentials' `entra` auth
mode removes the static secret without that bridge, which is what the suite is for.

## Verification

Nothing here is testable until something is built. What *is* checkable now, and what any
of the above must keep true:

```bash
python tests/test_personas.py        # card targets, flags, clouds all resolve
python tests/test_persona_docs.py    # every persona has exactly one doc, titles match
python tests/test_persona_nav.py     # nav pins are real ids, within budget
python tests/test_docs_conventions.py
```

The orphan-flag measurement in §1 is reproducible:

```bash
python - <<'PY'
from web_dashboard.services import personas as P, feature_flags as ff
claimed = {f for p in P.all_personas() for c in p.use_cases
           for f in (*c.requires_flags, *c.requires_any_flag)}
print(sorted({k for k in ff.flags() if k.endswith("_enabled")} - claimed))
PY
```

Re-run it after adding a persona: the list should shrink by exactly the flags that
persona's cards claim, and by nothing else.
