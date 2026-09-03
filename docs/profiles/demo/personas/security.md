# Security / IAM Analyst

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to whoever has to answer who had access to what, and prove it.

Does not own the infrastructure. Owns the **question**: who can reach what, with which
privilege, and what did they do with it. Everyone else on this list is a source of answers,
and the answers usually disagree.

Their access problem is not access at all — it is **evidence**. They are asked to attest to a
control they do not operate, using data produced by teams with no incentive to make it
legible.

## Why they care

The recurring failure is not that controls are missing. It is that demonstrating them costs
weeks: one export from the vault, another from the directory, a spreadsheet reconciling them,
and a finding that the reconciliation was already stale when it was produced.

What changes that is a single place where privileged access is *granted*, so the record is a
by-product of the grant rather than an archaeology exercise afterwards.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Every resource carries an owner, a creator and an expiry, so inventory is a fact rather than a survey. |
| **PRA** | Sessions are recorded, which turns "who had access" into "here is what they did". |
| **Password Safe** | Credentials have a custodian and a rotation history. |
| **Entitle** | Grants have a requester, an approver and an end time — the four fields an audit actually asks for. |

## Use cases

### Who has access to what, right now

Every privileged path into the estate in one view, with the job trail that created each one.
This is the question an auditor opens with, and most organisations answer it with a
spreadsheet.

### Find the credentials somebody committed

Scan the estate for secrets sitting in configuration. It always turns something up, which is
the point — a control that finds nothing is indistinguishable from one that is not running.

**Guide:** [Secrets Management](../../../secrets-management.md)

### Refuse the deploy that would have been a finding

Policy turns down a non-compliant request before anything is built, with a reason the requester
can act on. Prevention rather than a quarterly report.

**Guide:** [Policy Guardrails](../../../policy-guardrails.md)

### Push the events somewhere that gets read

Stream privileged-access events to Slack, Teams or a signed webhook, so the record lives in the
tool the team already watches rather than one somebody has to remember to open.

**Guide:** [Notifications](../../../notifications.md)

### Nothing privileged outlives its purpose

Every resource carries an expiry, and the ones that pass it are removed. The antidote to an
estate nobody can account for.

**Guide:** [Auto-delete Timer](../../../auto-delete-timer.md)

## What to enable

**Notifications**, **Admission control** and the **Auto-delete timer** — all three default off,
and the cards that need them will say so. Add **Secrets scanning** for the discovery card.

Every one of these is profile-neutral, so this focus works identically on a demo instance and a
[POV instance](../../pov/README.md).

Read [Auto-delete Timer](../../../auto-delete-timer.md) before switching it on. It deletes
infrastructure, and it has its own second gate for exactly that reason.

## Talking to this buyer

They are the one persona who is not impressed by provisioning speed, and showing it wastes
their time. Lead with the record: what a grant looks like after the fact, who approved it, when
it ended. If they ask "how would I prove this to an auditor", you have found the demo.
