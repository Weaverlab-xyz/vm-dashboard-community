# Design — the demo cells we do not have, and the roles they would serve

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are deciding what to build after the network cell, or wondering why a role you expected to find has no page.

> **Status:** Design note, v1. **Nothing here is built.** It records a measurement, a
> definition, and three candidates in the order they are worth doing — so the next person
> to ask "what else could we demo?" starts from evidence rather than from a brainstorm.
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

## 3. Candidate A — the agent cell (recommended)

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

**Open question to answer before building:** what the agent actually does. A worker that
only fetches a credential proves the plumbing; one that performs a recognisable task —
remediating a finding, rotating something, running a change — proves the story. The
second is materially more work and should be a deliberate choice rather than a drift.

## 4. Candidate B — cloud governance (a persona, deliberately not a cell)

**Role:** FinOps / cloud governance. **Owns:** privileged infrastructure nobody is
accounting for.

Four orphan flags sit together — `cost_explorer`, `vm_spend_cap`, `vm_suspend_schedule`,
`cloud_unmanaged_discovery` — and `admission_control` / `resource_expiry` are touched only
lightly by `cloudops` and `security`. "What privileged thing exists that nobody
onboarded, and what is it costing?" is a real role with real machinery behind it.

**It gets no cell, by §2.** The story is the estate, not a place. Standing up an
environment to demonstrate discovery would be staging the answer — you would be
discovering the thing you just built, which proves nothing about a real estate.

So: a persona whose cards point at existing surfaces (`/inventory`, `/costs`,
`/settings`). Cheap, and it closes four orphan flags at once.

**Counter-argument worth recording:** `cloud_unmanaged_discovery` may belong to
`security` as a missing card rather than to a new role — "find the privileged thing
nobody onboarded" is oversight, and `security` already owns oversight. If the FinOps
persona is not built, **add that card to `security` rather than leaving the flag
orphaned.**

## 5. Candidate C — a Windows endpoint cell, and the two things blocking it

**Role:** `itops` already exists. This is a cell for an existing persona, which is why it
is third.

The gap is sharper than "no cloud VDI" — cloud VDI is built and reasonably mature
([Virtual Desktops](../virtual-desktops.md), preview). The gap is that **the Windows
endpoint story is nearly untellable on a cloud-only instance**, for two independent
reasons:

| Blocker | Detail |
|---|---|
| **Windows seats are Azure-only** | AWS and GCP provision Linux seats only, and credential injection is Windows-only. The per-cloud table in `virtual-desktops.md` explains why: Azure vaults a generated password before the VM exists; EC2 returns password data encrypted to the launch key pair; GCE delivers through `windows-keys`. Only the first is wired. |
| **EPM is Linux-only** | `epml_enabled` is the only EPM flag in the registry, and `integrations/epml.md` is EPM **for Linux**. There is no Windows EPM integration at all. |

The second is the bigger one. **"Remove local admin from a Windows endpoint and elevate
per-application" is among the most recognisable demos BeyondTrust has, and this repo
cannot tell it.** That is worth knowing before anyone plans a Windows-centric demo
around this dashboard.

`itops` feels the consequence today: three of its five cards require `vmware_enabled`, so
on a cloud-only estate instance most of the IT-engineer catalog reads as not-ready.

**Therefore:** a Windows endpoint cell is worth building *after* a Windows EPM
integration exists, not before. Until then the cheaper fix is to give `itops` one or two
cloud-reachable cards against the existing Azure VDI path, so the persona is not mostly
unavailable on the instance most demos run on.

## 6. What is deliberately not proposed

- **A vendor-access cell.** Third-party access into a network they should not have is
  already the OT cell's flagship (`profiles/demo/personas/ot.md`). A second cell making
  the same argument for IT would split the story rather than add one.
- **A database cell.** `dba` is well covered and the databases are real; a purpose-built
  environment would add scenery, not a premise.
- **A Kubernetes cell.** `sre` has five cards and the clusters are genuinely provisioned.
  Same reasoning.

## 7. Suggested order

1. **Cloud governance persona** — cheapest, closes four orphan flags, no new subsystem.
2. **The agent cell** — highest value, most new work; scope §3's open question first.
3. **`itops` cloud cards** — small, fixes the weakest persona on a cloud-only instance.
4. **A Windows endpoint cell** — blocked on a Windows EPM integration that does not exist.

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
