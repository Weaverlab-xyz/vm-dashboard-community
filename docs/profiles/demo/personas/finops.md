# FinOps / cloud governance

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to whoever gets asked why the cloud bill went up and who approved it.

Answers for the infrastructure nobody decided to keep. Not the person who builds things —
the person who finds out what was built, what it is costing, and who still has the
standing access that made it possible.

Their problem is not that anything went wrong. Everything was approved, once. A lab was
stood up for a POC that finished in March; an engineer was made an administrator for a
migration that shipped; a VM was launched in the provider's console because the pipeline
was down that day. **Nothing here is a mistake. It is all just still there** — because
removing it was nobody's job, and nothing forced the decision.

## Why this story lands

Because the usual answers are a spreadsheet and a quarterly review, and both arrive too
late to change anything. The cloud bill is a lagging indicator of decisions made weeks
ago by people who have since moved on.

What a demo has to show, concretely:

- infrastructure the dashboard **did not build** — because that is where the
  unaccounted-for things actually are;
- a limit expressed in **dollars**, not a reminder to go and look;
- administrator access that **ends on its own**, on the console that can spend the money;
- the whole estate's cost answerable **without exporting a billing file**.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | The record. Every console here starts from completed deploy jobs, which is what makes it a record of what the dashboard *did* — and what makes discovery necessary for everything else. |
| **PRA** | Less central for this role, and worth saying so. It governs the sessions; this persona governs what the sessions are spent on. |
| **Password Safe** | The credentials behind the resources that are still running — the ones that outlive the project are the ones nobody is rotating. |
| **Entitle** | The spine of the story. It makes *access* expire, exactly as the spend cap and the suspend schedule make *infrastructure* expire. |

The argument to make out loud: **standing access and standing infrastructure are the same
disease.** Both accumulate because removing them is nobody's job. Entitle is how you stop
the first one accumulating, and the three controls below are how you stop the second.

## Use cases

### The VMs nobody told the dashboard about

Every cloud console here starts from completed deploy jobs, so a VM someone launched in
the provider's own console — or in Terraform, or before this dashboard existed — is
invisible to it. Turn discovery on and show the second listing: the privileged machines
that exist without anyone here having decided they should.

Note what the discovered listing will *not* do: **Destroy is refused on those rows, always.**
The dashboard did not build them and does not know what depends on them.

**Guide:** [Cloud VMs](../../../cloud-vms.md)

### A cap in dollars, not a reminder to check

Put a ceiling in dollars on one VM and watch it accrue against it. The total is rate ×
elapsed on every sweep rather than a figure read off a bill — a bill lags a day on all
four clouds, which is long enough to *report* a runaway instead of *stopping* one.

**Guide:** [Cloud VMs](../../../cloud-vms.md)

### Nobody is a standing admin of the thing that spends the money

The card that joins the two halves. Grant dashboard administrator through Entitle for the
length of a change and no longer. It lands immediately and revokes immediately, for any
user — local, Entra, any OIDC — so the console that can deploy your whole estate has no
permanent owner.

Ask the room who is an administrator of their own provisioning tooling, and how they
would find out. Most people have to go and look.

**Guide:** [Entitle dashboard permissions](../../../integrations/entitle-dashboard-permissions.md)

### What it cost, without asking the cloud twice

Spend by workgroup and by resource, from the dashboard's own record of what it built — so
"whose lab was that?" has an answer that does not begin with exporting a billing CSV and
joining it to a tag convention nobody followed.

**Guide:** [Cloud hosting](../../../cloud-hosting.md)

### Business hours for a lab that is not in one

Suspend at seven, resume at seven, weekdays only, set per VM. The rule is whether a
**boundary was crossed**, not whether the machine ought to be asleep right now — which is
what stops a VM somebody deliberately woke at 21:00 from being put straight back to sleep
on the next sweep.

**Guide:** [Cloud VMs](../../../cloud-vms.md)

## What to enable

| | Why |
|---|---|
| **`cost_explorer_enabled`** | The `/costs` page. Pre-ticked when you pick this focus. |
| **`entitle_enabled`** | The Entitle integration itself. Pre-ticked. |
| **`entitle_user_jit_enabled`** | The card above needs it, and it is **not** a wizard toggle — configure it in Settings, with `entitle_rest_secret` for the REST mechanism. |
| **`cloud_unmanaged_discovery_enabled`** | Settings → Integrations → *Discover unmanaged cloud VMs*. Off by default. |
| **`vm_spend_cap_enabled`** | Settings → Integrations → *VM spend caps*. Off by default. |
| **`vm_suspend_schedule_enabled`** | Settings → Integrations → *VM suspend schedules*. Off by default. |

Four of those six are configured in Settings rather than the setup wizard, so picking
this focus pre-ticks only the two it can. The cards report the rest as *needs flag* and
point at the panel — which is deliberate: a preset naming a toggle the wizard does not
render would be a silent no-op.

Three of the five cards target a cloud console, which a POV instance does not serve — see
[the POV profile](../../pov/README.md) for why the two install profiles differ there. The
other two run on either.

## Talking to this buyer

This is the one persona where the opening question is better than any feature. Ask: **"if
I gave you an hour, could you tell me every privileged thing running in your cloud
accounts right now, and who approved it?"** Nobody can. The demo is the answer to the
question they just failed.

Two objections, both fair:

- **"We have a cloud cost tool already."** Almost certainly, and it is better at cost than
  this is. It does not know which of those resources are privileged, who holds their
  credentials, or who has standing access to the thing that created them. This is not a
  cost tool with a PAM feature; it is the privileged-access record that happens to know
  what things cost.
- **"Our tags would catch that."** Ask how tags get onto a VM somebody launched by hand in
  the console. That is the whole population this finds.

Close on the Entitle card rather than the cost one. Cost gets attention; **"your
provisioning console has no permanent administrator"** gets budget, because it is the same
sentence the rest of the platform has been making all along — and it lands harder here,
where the thing being governed is the tool doing the governing.
