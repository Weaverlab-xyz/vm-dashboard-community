# A POV Instance

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are about to run customer proof-of-value work and want to know why it needs its own dashboard.

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

The gate that makes them exclusive, and the per-feature matrix, are in
[Demo and POV profiles](../../../README.md).

A POV instance also needs a **lab platform** — the thing a POV environment actually runs
on. Skytap is the first one; see [Skytap](skytap.md) for its
prerequisites, the API token it needs, and the
[template contract](skytap.md#the-template-contract) its broker VM has to
satisfy.

And it needs somewhere to put the many tenants the first paragraph of this page is about.
That is [the tenant registry](standing-one-up.md#the-tenant-registry). The platform registry lives in
`services/lab_platforms.py`, and `GET /api/pov/platforms` reports what each one can do so
the UI can degrade visibly rather than offering a button that fails.

## The pages

| Page | What's in it |
|---|---|
| [Standing up a POV instance](standing-one-up.md) | Its own JWT key, its own env file, and the registry of customer tenants it resolves against. |
| [Running POVs on a public cloud](public-cloud.md) | One cloud at a time, what gets created, what it costs, and the per-provider differences. |
| [The Gateway and the Resource Broker](gateway-and-broker.md) | The outbound-dialling Gateway that brokers sessions in, and the Resource Broker that lets Password Safe reach the guests. |
| [Wiring a POV's VMs into PRA, Password Safe and Entitle](wiring.md) | Jump items, vaulted accounts and just-in-time grants, per POV and per tenant. |
| [What the customer sees](customer-access.md) | The use-case checklist, the ephemeral accessor login, and the share link. |
| [Keeping a POV true, and reaping it](lifecycle.md) | Reconciling against the platform, and the auto-delete timer. |

Two more pages sit beside these: the POV lab platform [Skytap](skytap.md),
and the [Password Safe POC runbook](ps-runbook.md) a POV can be run against.
Why two of these subsystems are shaped the way they are is in
[design/](design/README.md).

## What this stack deliberately does not mount

Unlike the demo stack, neither service gets `/var/run/docker.sock` or the `runner_work`
volume. The demo stack needs the socket to launch sibling Ansible and kubectl/helm
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
| A nav link or Settings toggle refuses | [Demo and POV profiles](../../../README.md#failure-modes) |
| A tenant is missing, wrong, or rejected | [Standing up a POV instance](standing-one-up.md#failure-modes) |
| A tunnel times out, or a broker install hangs | [The Gateway and the Resource Broker](gateway-and-broker.md#failure-modes) |
| A VM was skipped, or a jump item does not work | [Wiring](wiring.md#failure-modes) |
| A share link or an accessor login fails | [What the customer sees](customer-access.md#failure-modes) |
| A POV did not expire, or expired wrongly | [Keeping a POV true](lifecycle.md#failure-modes) |
