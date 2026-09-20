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

**Corrected twice, and the second correction reverses the first.** An earlier draft
called this "the cheapest remaining item". A later one called it *blocked* — reasoning
that every unbuilt tab vaults its credential where a consumer would need another
credential to reach it, and concluding that the only way out was the SPIFFE bridge §3
records as unresolved.

**That conclusion was wrong**, and wrong in the direction that stops work happening. It
reasoned about Password Safe without checking what **Workload Credentials** is for.

### The chain that needs no static secret

`services/workload_credentials_service` states it in its own docstring: *"Two auth modes,
and the second one stores nothing."* `wlc_auth_mode` is either `pat` — a stored token —
or **`entra`**, where the platform vouches for the machine and no PAT exists at all:

1. the workload asks the platform for **its own identity token** (IMDS, or
   `IDENTITY_ENDPOINT`/`IDENTITY_HEADER` where the runtime injects them);
2. it presents that to **Workload Credentials** in place of a PAT, with
   `X-BT-Service-Name` naming which registered Workload Identity it satisfies;
3. Workload Credentials serves the secret — its own static store, a dynamic
   short-lived cloud credential, or a `bt_safe://` reference into Password Safe.

Everything the workload is *configured* with — site id, service name, entra resource,
base URL — is non-secret. **Nothing is stored on the host.**

The repo already records this as implemented: *"Authenticating to WC with an Azure
workload identity instead of a stored PAT — **Implemented**; client path unit-tested, the
Azure + Pathfinder wiring not yet run live."*

### Why this is the point rather than a workaround

It is the suite solving a new problem with a combination of old and new. Password Safe
(old) holds the secret and governs it. Workload Credentials (new) brokers access to it
against an identity the platform vouches for. The workload holds nothing. Neither product
does this alone, and the interesting demo is the seam between them — which is exactly the
thing a competitor with one of the two cannot show.

### What was built

The worker gained a second token source. `--token-source file` reads a 0600 file — a
static secret, honestly labelled. `--token-source wlc` holds nothing: it fetches its own
identity token and reads its dashboard PAT back out of Workload Credentials.

That removes the asterisk the cell had been carrying. "A non-human principal that holds
no standing credential" was the argument, and a PAT in a file was that argument with a
caveat. Now the caveat is a *mode*, and the default can move once the path has been run
live.

The install play refuses a `wlc` worker missing any of its non-secret configuration,
naming each one — there is no reason to be vague about a value that is not a credential
— and it **removes** any token left behind by a previous `file`-sourced install, because
"nothing is stored on this host" must not be contradicted by a file in `/etc`.

The link from the earlier draft stays: an agent is still made *answerable for* one
Workload Lab credential, and that is still a governance record rather than a capability.
What changed is that the worker can now hold its own credential without one being left
on a disk for it.

### Still unproven

The identity path is **Azure-shaped today** — IMDS and `X-IDENTITY-HEADER` — and the
in-cluster form (a pod federating its ServiceAccount token) is listed as *Planned*. The
Azure + Pathfinder wiring has not been run live by anyone. So `file` stays the default
and the page says which mode was used on every line the worker logs.

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
