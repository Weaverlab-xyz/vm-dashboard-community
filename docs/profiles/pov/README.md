# A POV Instance

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are about to run customer proof-of-value work and want to know why it needs its own dashboard.

A **second** dashboard, next to the one managing your own estate. Same image, same code,
its own database, its own users, its own agents — and a different set of features.

It exists because of one thing: **tenancy.** An estate instance resolves its BeyondTrust
tenant from the global singletons (`bt_api_host`, `pscli_api_url`, `entitle_api_key`). A POV
instance holds a *registry* of many named tenants, because several POVs run at once and each
has its own PRA appliance and Password Safe Cloud tenant. An instance claiming both roles
would have two answers to "which tenant?" at every call site — and the wrong answer is
silent, not loud: an estate VM deploy onboarding into a customer's Password Safe, or a POV
onboarding into your own tenant. Nothing errors, both paths "work".

So the two profiles are **mutually exclusive**, and the exclusion is enforced in code rather
than left to discipline.

The gate that makes them exclusive, and the per-feature matrix, are in
[Demo and POV profiles](../README.md).

A POV instance also needs a **lab platform** — the thing a POV environment actually runs
on. Skytap is the first one; see [Skytap](skytap.md) for its
prerequisites, the API token it needs, and the
[template contract](skytap.md#the-template-contract) its control node VM has to
satisfy.

**Who sees which POV.** A **POV Presenter** sees only the POVs they own: the ones they
created, plus any added to their POV access picker. A **POV Manager** sees and manages every
POV. A **POV Template Builder** builds the templates and saves the blueprints presenters
build from, and creates no POV itself; a presenter picks a blueprint and chooses the
tenants when they press Build. See [Permissions → Roles](../../admin/permissions/roles.md) and [who a presenter sees](../../admin/permissions/customer-read-access.md#a-named-person-who-should-also-be-a-real-user).

**Handing a POV to another SE.** The **Created by** column on the POV list has a
`hand off` control. Type the new owner's dashboard username and they become the owner,
exactly as if they had created it. The dashboard refuses the handoff, and changes nothing,
when the new owner could not then open the POV. That covers four cases:

- they hold no POV role
- their POV access picker leaves this POV out
- they are not in the POV's workgroup
- the account is an accessor or a service account

The previous owner keeps access only through a role that reaches every POV, or through
their picker. A Presenter who hands their POV on stops seeing it. Each handoff is written
to the audit log as `pov_owner_changed`. It also sends a `pov.owner_changed` notification
(warning severity), which is on by default.

And it needs somewhere to put the many tenants the first paragraph of this page is about.
That is [the tenant registry](standing-one-up.md#the-tenant-registry). The platform registry lives in
`services/lab_platforms.py`, and `GET /api/pov/platforms` reports what each one can do so
the UI can degrade visibly rather than offering a button that fails.

## Setting a POV up, step by step

Open a POV and go to its **Setup** tab. A POV that is not finished opens there by default,
and every row on the POV list has a **Continue setup →** link to it. The tab lists every
step in order:

1. environment running
2. every guest's OS known
3. control node agent enrolled
4. PRA Gateway
5. Resource Broker
6. Entitle agent
7. VMs wired
8. Password Safe credentials in PRA
9. customer share link

Each step has one button. The step to do next is highlighted. A step that depends on one
that is not finished yet is greyed out and names the step to finish first. Only a real
dependency locks a step, and the optional parts (Resource Broker, Entitle, Password Safe in
PRA) never hold up the share link. The tab refreshes itself while a step is running.

Steps that need a form, such as the Resource Broker's installer, zone and key, open that
form on the POV list page.

The tab also shows everything in the POV list's row, so you don't have to scroll that wide
table sideways:

- **The strip at the top** shows the columns that belong to no single step: power and when
  it was last confirmed, VM count, the PRA, Password Safe and Entitle tenants, use cases
  done, expiry, the sleep timer or schedule, spend, template and who created the POV.
- **The small line under each step** shows what that step's column says on the list. For
  example, the control node's status and VM, the Gateway's name and whether it has a deploy key,
  the Resource Broker's installer, zone and key, the wired counts for PRA, Password Safe
  and Entitle, and whether the share link is live.

These are read-only. To edit them, use the controls on the POV list page.

## The pages

| Page | What's in it |
|---|---|
| [Standing up a POV instance](standing-one-up.md) | Its own JWT key, its own env file, and the registry of customer tenants it resolves against. |
| [Running POVs on a public cloud](public-cloud.md) | Choosing clouds, what gets created, what it costs, and the per-provider differences. |
| [The Gateway and the Resource Broker](gateway-and-broker.md) | The outbound-dialling Gateway that brokers sessions in, and the Resource Broker that lets Password Safe reach the guests. |
| [Wiring a POV's VMs into PRA, Password Safe and Entitle](wiring.md) | Jump items, vaulted accounts and just-in-time grants, per POV and per tenant. |
| [Demo cells in a POV](demo-cells.md) | Carry the OT simulator or a VyOS network device into a POV as a guest with a cell role. |
| [What the customer sees](customer-access.md) | The use-case checklist, the ephemeral accessor login, and the share link. |
| [Keeping a POV true, and reaping it](lifecycle.md) | Reconciling against the platform, and the auto-delete timer. |

Two more pages sit beside these: the POV lab platform [Skytap](skytap.md),
and the [Password Safe POC runbook](ps-runbook.md) a POV can be run against.
Why some of these subsystems are shaped the way they are is in
[design/](design/README.md) — including
[the home page on a POV instance](design/home-page.md), which is where this profile's
landing page stops being an overview of an estate it does not have.

## What this stack deliberately does not mount

Unlike the estate stack, neither service gets `/var/run/docker.sock` or the `runner_work`
volume. The estate stack needs the socket to launch sibling Ansible and kubectl/helm
containers. A POV instance runs Ansible **on the remote agent inside the customer
environment**, and Kubernetes is masked off entirely, so handing this stack the host's Docker
daemon would grant a great deal and buy nothing.

If you later want local Config Management runs here, the socket has to come back — and that
is a deliberate decision to make, not an omission to fix.

## Where to look when something breaks

Every page carries its own **Failure modes** section, because the symptom and
the mechanism belong together. By symptom:

| It looks like | Look in |
|---|---|
| A nav link or Settings toggle refuses | [Demo and POV profiles](../README.md#failure-modes) |
| A tenant is missing, wrong, or rejected | [Standing up a POV instance](standing-one-up.md#failure-modes) |
| A tunnel times out, or a broker install hangs | [The Gateway and the Resource Broker](gateway-and-broker.md#failure-modes) |
| A VM was skipped, or a jump item does not work | [Wiring](wiring.md#failure-modes) |
| A share link or an accessor login fails | [What the customer sees](customer-access.md#failure-modes) |
| A POV did not expire, or expired wrongly | [Keeping a POV true](lifecycle.md#failure-modes) |
