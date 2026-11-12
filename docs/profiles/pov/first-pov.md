# Your first POV, end to end

> **Audience:** presenter · **Profile:** `pov` · **Read this when:** you have been given a POV Presenter login and are about to build your first customer environment.

A POV is one customer's evaluation: a small environment on a lab platform, wired into that
customer's own BeyondTrust tenants, with a checklist of use cases they tick off as you prove
them. This page takes you from a login to a finished evaluation once, in order, and says what
each step is actually for.

Nothing here installs the dashboard. If you are standing up the POV *instance* — the second
dashboard itself, its tenants and its JWT key — that is operator work and it lives in
[Standing up a POV instance](standing-one-up.md).

Target time for a first build, with the tenants already registered: **about an hour**, most of
it waiting on the lab platform and on installs that run inside the environment.

| If you want | Go to |
|---|---|
| The shape of the whole thing, in one diagram | [POV architecture and workflow](../../architecture.md) |
| The list of setup steps and what each is for | [Set it up, step by step](#set-it-up-step-by-step) below |
| What the customer gets at the end | [What the customer sees](customer-access.md) |
| Why a POV needs its own dashboard | [A POV Instance](README.md) |

---

## Before you start

**A login with the POV Presenter role.** You see only the POVs you own: the ones you created,
plus any handed to you or added to your POV access picker. Other people's POVs are not in your
list and not in your search. A **POV Manager** sees every POV, and a **POV Template Builder**
makes the blueprints and creates none. See [Permissions → Roles](../../admin/permissions/roles.md).

Two things a Presenter deliberately cannot do, so you know who to ask rather than hunting for a
missing button: you cannot change instance-wide settings, and you cannot power the lab platform
around outside your own POVs.

**The customer's tenants, already registered.** A POV points at *that customer's* PRA appliance,
Password Safe Cloud tenant and Entitle tenant — not at your own. They are registered once, on
the **Tenants** tab, before the POV that uses them is built. You can build a POV and choose
"later" for a product whose appliance is still being provisioned; the steps for that product sit
grey in the ladder until a tenant is named, and the step text says so rather than failing.

**A blueprint.** A saved recipe carrying the template, project, control-node VM and idle timer.
You supply only the name and the tenants. If none fits, **Build by hand** opens the full form.

## Build it

On **POV → Build**: pick the platform, name the POV, pick a blueprint, tick the products this
evaluation will test, and choose a tenant for each.

Name it after a reference, not the customer — `poc-014`, not the account name. The name shows up
in the lab platform, in jump item names and in the share link.

The product ticks are not cosmetic. They decide which steps appear in your setup ladder at all:
a Password Safe-only POV never asks you about a PRA Gateway. **Build** stays disabled until every
product you ticked has a tenant, so the server's refusal is never the first you hear of it.

## Set it up, step by step

Open the POV and go to its **Setup** tab. A POV that is not finished opens there by default, and
every row on the POV list has a **Continue setup →** link to it.

The tab lists every step in order with one button each, highlights the one to do next, and greys
out a step that depends on an unfinished one — naming the step to finish first. It refreshes
itself while a step is running. Each row carries a **Guide →** link to the page explaining that
step, which is the fastest way out of this page and into the detail.

Only a real dependency locks a step. **Only "Environment running" holds up the customer share
link** — the Resource Broker, the Entitle agent and the Password Safe link are optional halves,
and a correctly scoped evaluation finishes without them.

| # | Step | What it is for | Needed before you can share? |
|---|---|---|---|
| 1 | Use cases chosen | Which products this POV tests, and which use cases you will actually run. Decides the rest of the ladder. | no |
| 2 | Environment running | The guests exist on the lab platform and are powered on. | **yes** |
| 3 | Every guest's OS known | The dashboard knows what each guest runs, so the wire-up builds the right kind of session. | no |
| 4 | Control node agent enrolled | The agent inside the environment that every later install is leased by. | no |
| 5 | PRA Gateway | The outbound-dialling Gateway that brokers sessions into the environment. | no |
| 6 | Resource Broker installer fetched | The installer and its key, pulled out of Password Safe for the next step. | no |
| 7 | Resource Broker | Lets Password Safe reach the guests to rotate and inject credentials. | no |
| 8 | Entitle agent | Runs in the environment so Entitle can grant just-in-time access. | no |
| 9 | VMs wired into PRA / PS / Entitle | Jump items, vaulted accounts and JIT grants, per guest. | no |
| 10 | Password Safe credentials in PRA | PRA can inject this POV's vaulted accounts into its jump items. | no |
| 11 | OT HMI access through Entitle | Only on a POV carrying an OT simulator guest. | no |
| 12 | Customer share link | The password-protected link you give the customer. | — |

A step reading **Not part of this POV** in grey is not a problem: the products you did not tick,
and the steps a platform cannot do, settle that way and count as finished.

Two of the twelve are judged on an artifact rather than on a job finishing, and both are worth
knowing before they bite — see [What "done" means here](#what-done-means-here).

### Choose the use cases

The first step, and a choice rather than a job. Pick what this POV tests — Password Safe, PRA,
Total PASM (both), Entitle, or a mix — then untick any individual use case you will not run.

Two things follow from it. The ladder drops the steps for products you left out, and the
customer's checklist shows only what is in scope; an out-of-scope card is not drawn at all, on any
surface. So scoping honestly here is what stops the customer's page reading as a wall of work
nobody intends to do.

You can change it later on the same step. The full model is in
[Design: use cases per POV](design/use-cases.md#choosing-the-povs-use-cases), and what the
customer does with the result is in [Use cases, per POV](customer-access.md#use-cases-per-pov).

### Enrol the control node agent

Every install that has to happen *inside* the customer's environment — the Gateway, the Resource
Broker, the Entitle agent — is leased by one agent on one guest: the control node. Until it is
enrolled, those three steps have nowhere to run and stay locked.

The provision installs and enrols it inline, so by the time the environment is active there is
nothing still coming. A control node reading **none** means the inline install did not take, and
the usual cause is the step above it: auto-detection is blind to a guest whose OS is unknown.
Set the OS on the VMs tab, then press **Install control node**.

An agent that reads `offline` or `revoked` existed and no longer answers. Installing again
replaces it; it does not stack a second one.

Which guest it lands on, and how it is built, differ by platform:

- On Skytap — [the Skytap control node VM](skytap/control-node.md), including the
  resolution order and the blank-OS trap.
- On a public cloud — [the POV control node on a cloud](public-cloud/control-node.md), where it
  is built by cloud-init and rebuilding recreates the VM.

### What "done" means here

Two steps can never report a green tick, and that is deliberate.

The **PRA Gateway** and the **Resource Broker** report *configured*: everything this dashboard
needs is in place, and whether the install actually ran is a live question it cannot read back.
The Gateway's **Check** asks the appliance whether a node is connected. Inventing a tick there
would be the one genuinely misleading thing the ladder could do.

The **wire-up** is judged on guests actually wired, never on the job going green — because a POV
whose guests all report no OS will run the wire-up, skip every one of them, and finish
**completed** with nothing wired. If the ladder says *no guest has a known OS*, fix the OS first;
the job would otherwise lie to you. The detail is in
[What is skipped, and why](wiring.md#what-is-skipped-and-why).

## Getting ready for the call

Book a **warm-up** rather than pressing Start when the customer joins — a suspended lab can take
several minutes to resume. The booking fires at most once and is only as precise as the reconcile
pass, so a 20-minute lead really means 10 to 20. It also books a ready check for five minutes
before the demo, so the badge you open the POV to is current. See
[Warming a POV up for a demo](lifecycle.md#warming-a-pov-up-for-a-demo).

The **Ready for demo** check on the Overview tab is the one that answers "will this work in ten
minutes": the worst row wins, so a single amber is the thing to go and look at.
[Before a demo](lifecycle.md#before-a-demo) lists what it covers.

## What the customer sees

The **share link** is password-protected and expires; re-sharing replaces it. From it the
customer reaches an accessor page with their use-case checklist, which they tick off and comment
on as you prove each one. Those comments are the evidence the evaluation produces, so the rows on
their page are deliberately not click-to-tick.

[What the customer sees](customer-access.md) covers all of it;
[the share link](customer-access/share-link.md) covers the link itself.

## When it is over

A POV suspends itself on the platform's idle timer, or on a schedule you give it. On a public
cloud this matters more than on Skytap: stopping a cloud VM halts compute only, while disks,
public IPs and the network bill run for the whole evaluation. See
[idle suspend](public-cloud/idle-suspend.md) and [the suspend schedule](public-cloud/suspend-schedule.md).

The auto-delete timer reaps the POV at its expiry. Destroying it keeps the use-case results and
the close-out report — see [What a POV leaves behind](customer-access.md#what-a-pov-leaves-behind)
— so reaping an evaluation does not throw away what it proved.

Handing the POV to another SE is a control on the **Created by** column, not a rebuild. The
dashboard refuses a handoff that would leave the new owner unable to open it.

## Where to look when something breaks

Every page carries its own **Failure modes** section. The router by symptom is at the foot of
[A POV Instance](README.md#where-to-look-when-something-breaks).
