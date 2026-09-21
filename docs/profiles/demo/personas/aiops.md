# AI / agent platform

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to whoever is being asked to let an autonomous thing touch production.

Owns the principals that act without a person at the keyboard. Not the pipeline — a
pipeline runs when someone merges — but the worker that decides what to do next, holds
whatever it was given, and keeps holding it.

Their problem is that nobody asked the access questions before the thing was switched on.
An agent was given an API key so it could "just read a few things", the key went into an
environment variable, and the answers to *what may it reach*, *what did it do*, and *can
you stop it right now* are all some version of "we'd have to go and look".

## Why this story lands

Because the honest answer in most organisations is a long-lived key in a `.env` file, and
everyone in the room knows it. Every other identity in the estate got a decade of
governance thinking; this one arrived last quarter and skipped all of it.

What a demo has to show, concretely:

- a worker that **holds no identity document** and still proves who it is;
- authorization that is **scoped to something you can point at**, not "the API key";
- a record of **what it actually did**, with a name against each call;
- and the one that settles the room: **stop it, now, while everyone watches.**

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | The host. Deliberately an ordinary VM this dashboard already deployed — the [agent cell](../agent-demo-cell.md) attaches rather than creating, so the worker inherits an auto-delete timer and a Destroy button. |
| **PRA** | Not central, and worth saying so. PRA governs sessions humans open; there is no human in this workflow at all. |
| **Password Safe** | The authority behind the SPIRE trust domain that attests the worker, via the SPIFFE SVID plugin. |
| **Entitle** | Where this goes next rather than what it does today — time-boxing a non-human principal's grant is the same argument, and the [agent cell](../agent-demo-cell.md) says plainly what is not built yet. |

The sentence to land: **an agent is a principal, and everything you already believe about
privileged access applies to it.** Nothing here is a new product category. It is the
existing argument, pointed at the newest thing in the estate.

## Use cases

### Revoke it mid-task, and watch it stop

A worker is reading your estate on a loop, one line per poll. Revoke its token with the
log on screen: the next poll is refused, the worker says so in its own words, and the unit
stops. Then ask the room how they would do that to an agent they are running today.

Start on **Workload Lab → Agent**, which is where the whole beat lives: mint, the token
shown exactly once, the install commands with this agent's values already in them, and
Revoke. Show it from Settings → API Tokens instead if the point you want is that an
agent's authorization is one more row among everyone else's.

**Guide:** [Agent Demo Cell](../agent-demo-cell.md)

### An identity it had to earn

The worker holds no identity document. It attests itself to a SPIRE trust domain on every
loop and names the SPIFFE ID it was given — so deleting the registration entry is enough
to make it anonymous, and nothing has to be revoked from the host.

**Guide:** [SPIFFE and SPIRE](../../../workload-lab/spiffe.md)

### What the agent could see, and why

Every MCP tool resolves the token's user and applies that user's permissions, so "what can
this agent reach" is a row in the RBAC table rather than a guess. The cell refuses to mint
an agent token against an administrator, which is the one choice that would quietly make
the whole demo say the opposite of what it means to.

**Guide:** [MCP Server](../../../integrations/mcp-server.md)

### Every call it made, and when

Three records that agree: the token's own `last_used_at`, the job trail, and the worker's
log naming the identity behind each call. For a principal most estates are not watching at
all, that is the difference between an incident and an investigation.

**Guide:** [Audit log](../../../audit-log.md)

### A credential that was never a secret to begin with

For the other shape of agent — one reaching a cloud API rather than this dashboard. A
credential minted per run and leased, with its own issuance audit. Carry the caveat
honestly: on AWS a lease **cannot be revoked**, so the TTL is the only control there is.
Azure honours the revoke.

**Guide:** [Short-lived cloud credentials](../../../workload-lab/cloud.md)

## What to enable

| | Why |
|---|---|
| **`agentcell_enabled`** | The cell itself, and a **preview** — off by default. Settings → Preview features → *Agent Demo Cell*. |
| **`mcp_server_enabled`** | What the worker calls. With it off the cell refuses to deploy rather than installing a worker that would 404 on every poll. |
| **`spire_lab_enabled`** | The trust domain that attests the worker, and the gate on the Workload Lab page two of these cards point at. |
| **`workload_credentials_enabled`** | Only for the last card. |
| A **SPIRE lab on the host** | Stood up before the agent — the worker attaches to a host that is already a SPIRE agent node. |

**Nothing here is a setup-wizard toggle**, which is why picking this focus pre-ticks
nothing. All four are configured in Settings, so the cards report them as *needs flag* and
point at the panel — the arrangement `vdesktops` and `notifications` already have.

This focus **needs an estate instance**; on a [POV instance](../../pov/README.md) most of
its cards report as unavailable by design. The reason is worth stating rather than
discovering live: `spire_lab_enabled` is estate-owned because the lab's administrative
credential is written into Secrets Safe through the global `pscli_*` singletons, which on
a POV instance would land in the wrong customer's tenant. `agentcell_enabled` is masked
along with it — not for the same tenancy reason, but because the cell cannot mint an
agent without a trust domain to attest it against, and only that lab creates one. An
unmasked toggle there would be a switch that turns on and can never work.

## Talking to this buyer

Open with the question, not the product: **"how many non-human principals are running in
your estate right now, and could you stop one of them in the next sixty seconds?"** The
second half is the one that lands, because it is answerable and the answer is usually no.

Two objections worth preparing for:

- **"Our agents only read."** So does this one. The demo is deliberately a read-only
  worker, because the argument does not depend on the blast radius being frightening — it
  depends on nobody being able to say what the blast radius *is*. A read-only agent with an
  unscoped, non-expiring key is still an unscoped, non-expiring key.
- **"That's an application security problem, not a PAM problem."** It is a principal with
  a credential, reaching privileged systems, with no owner and no expiry. Every word of
  that is the PAM problem. The only thing that changed is that the principal is not a
  person.

Be straight about what is not built: the worker's **identity and its authorization are
two separate things** and nothing mints one from the other yet. That gap is stated on the
cell's page, and stating it is worth more than glossing it — this audience has been sold
autonomy before and is listening for what you leave out.
