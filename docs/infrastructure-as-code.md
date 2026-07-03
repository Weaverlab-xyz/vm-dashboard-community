# Infrastructure as Code

This document explains how the dashboard does infrastructure as code —
the philosophy that drives the design, the best practices the codebase
encodes, and how each cloud's deployment path fits the same model.

The companion docs:

- [Image Management](image-management.md) — how the *images* the
  Terraform deploys consume are built, hubbed in a single storage
  backend, and one-click promoted to AWS / Azure / GCP via the
  per-target promote runners
- [Config Management](config-management.md) — what to install on the
  infra you've stood up
- [Storage Management](storage-management.md) — where your IaC-side
  artefacts (playbooks, Packer manifests) live
- [Secrets Management](secrets-management.md) — credentials feeding
  the IaC layer
- [Policy Guardrails](policy-guardrails.md) — optional pre-action OPA
  checks that can block a deploy before it starts (allowed regions,
  instance-size caps, change-freeze windows)

---

## Philosophy

Infrastructure-as-code makes deployment a property of code, not of
operator memory. We try to bake the principles into the dashboard
rather than leave them to user discipline.

**1. Declarative, not procedural.** You describe the resources you
want; the IaC tool figures out what to create, change, or leave alone.
A Terraform module that says "an EC2 instance with these tags, this
AMI, this subnet" is right whether the instance exists yet or not. A
shell script that says "run `aws ec2 run-instances`" is wrong on the
second run, leaks tags between runs, and offers no rollback.

**2. Version-controlled definitions.** Templates are code: in git,
reviewed, rolled back when wrong. The dashboard ships HCL templates in
[`terraform/`](../terraform/) under source control; runtime values
(AMI ID, region, subnet) come from the deploy form, not from inline
edits to the HCL.

**3. Plan before apply, recorded after.** Every cloud deploy goes
through `terraform init → apply` with the variables the operator
filled in, and the result is captured as a tracked Job with `extra_data`
containing the resulting instance ID, IP, ARN, etc. That makes the
deploy reproducible (re-run with the same inputs) and the destroy
deterministic (fed by the recorded state, not by hand-typed IDs).

**4. Idempotent destroy.** A resource the dashboard created can be
destroyed by the dashboard. The path is closed: every deploy records
the state needed for tear-down; every destroy is fed by that recorded
state. No "ssh into the cloud console and click delete" lifecycle.

---

## How the dashboard implements these

| Principle | Where it shows up |
|---|---|
| Declarative | Each cloud uses a small Terraform module ([`terraform/ec2_instance/`](../terraform/ec2_instance/), [`terraform/azure_vm/`](../terraform/azure_vm/), [`terraform/gce_instance/`](../terraform/gce_instance/)) with a fixed resource shape. The deploy form provides variable values; the module is unchanged across deploys. |
| Version-controlled | All HCL is in the repo. Runtime variables flow through `services/terraform.py`'s `apply()` as `-var key=value`, never spliced into the template. Compromise an HCL template via PR review, not by hand-edit. |
| Plan-then-record | Deploys run as background jobs (`/jobs`) with progress and final state saved to `Job.extra_data`. Failed apply leaves the deploy job marked `failed` with the Terraform stderr captured for forensics. |
| Idempotent destroy | Each deploy's state is keyed per job (`terraform-state/{job_id}/`) in your active storage backend; destroy replays that exact state through `terraform destroy -auto-approve`. Purely state-driven — no "re-derive the resource ID from live cloud state" step. Because the state is remote, destroy still works after the container is recreated. |

---

## The IaC surfaces

The dashboard exposes IaC through several distinct surfaces. They
overlap in concept but each has its own state model and lifecycle.

### Cloud VM deployment (Terraform per-job)

The most-used IaC surface. AWS / Azure / GCP deploy forms feed a
small per-cloud Terraform module:

| Cloud | Module | Resources it creates |
|---|---|---|
| AWS | [`terraform/ec2_instance/`](../terraform/ec2_instance/) | `aws_instance` + key-pair material; tags include `dashboard-deployed=true` |
| Azure | [`terraform/azure_vm/`](../terraform/azure_vm/) | NIC + (optional) public IP + virtual machine |
| GCP | [`terraform/gce_instance/`](../terraform/gce_instance/) | `google_compute_instance` with `ssh-keys` metadata |

State is keyed per deploy (`terraform-state/{job_id}/`) in your active
storage backend — isolated per job, never reused or merged, so a destroy
can't accidentally target a sibling deploy. The container-side
`terraform/deployments/{job_id}/` holds only the working copy (module +
provider cache); the canonical state lives in the backend (see
[State](#state-the-thing-that-makes-iac-work)).

The `services/terraform.py` wrapper handles `init` / `apply` /
`destroy`, captures stderr on failure, and runs everything via
`asyncio.to_thread()` so the FastAPI event loop stays responsive
through 60-second applies.

### BeyondTrust Shell Jump (Terraform `sra` provider)

Implemented in
[`services/terraform_pra_service.py`](../web_dashboard/services/terraform_pra_service.py).
Each VM deploy automatically provisions a BT PRA Shell Jump with
matching name + jump-group; the destroy path tears it down. Same
pattern as cloud VMs — Terraform module + per-deploy state — just
applied to a different cloud-of-clouds (BeyondTrust's PRA platform).

### Image building (Packer)

For the "build me a custom image" path, the dashboard wraps Packer:
[`services/packer_service.py`](../web_dashboard/services/packer_service.py).
Three builders are supported (`amazon-ebs`, `azure-arm`,
`googlecompute`); per-build templates are generated in-process from
form input rather than pre-staged in repo, because image-build inputs
(source AMI, instance type, provisioning script) are too varied to
template statically.

This is IaC adjacent — it produces an artefact (the image) which
becomes the input to a Terraform deploy later. Treat the image like a
git tag: build once, deploy from it many times.

### Sandbox bootstrappers (bash / PowerShell)

The [`scripts/sandbox/`](../scripts/sandbox/) bootstrappers stand up
fully-isolated lab VPCs / VNets / VPCs across AWS / Azure / GCP. They
intentionally use cloud-CLI calls rather than Terraform — for
laboratory, throw-away infrastructure, the bash + tags-driven cleanup
model is cheaper and has fewer moving parts than maintaining a
parallel Terraform tree. See [docs/CLOUD_SANDBOX.md](CLOUD_SANDBOX.md)
for the topology and tear-down semantics.

The two patterns coexist deliberately: bootstrappers create the
network you'll deploy into; the dashboard's Terraform-backed deploy
form drops your VMs onto that network.

---

## State: the thing that makes IaC work

Terraform state is the canonical record of what a deploy created — lose
it and the destroy path can no longer target those resources. The
dashboard keeps state in **two places, by design**.

**Most state lives in your active storage backend.** Cloud VM,
cloud-database, and Kubernetes-cluster deploys write their state to the
same backend the [/storage](storage-management.md) system uses (AWS S3 /
Azure Blob / GCS), keyed per job at
`terraform-state/{job_id}/terraform.tfstate`, authenticated with the same
credentials. It's remote and **locked**: S3 uses native state locking
(the `use_lockfile` mechanism, Terraform ≥ 1.10 — no DynamoDB table
needed), Azure Blob uses a blob lease, GCS is natively consistent. Two
operators pressing Deploy on the same target serialise safely instead of
corrupting state. Wiring lives in
[`services/terraform.py`](../web_dashboard/services/terraform.py)
(`_backend_settings`). If no storage backend is configured, state falls
back to the local deploy directory.

Because that state is remote, the per-job directory inside the container
(`terraform/deployments/{job_id}/`) holds only the **working copy** — the
HCL module copied from `terraform/{cloud}/` plus the pinned
`.terraform/providers/`. If the container is recreated and that directory
is lost, a destroy still works: the module is re-materialised from its
template and `terraform init` re-attaches to the remote state. (Under the
local-backend fallback, losing that directory orphans the resources — so
configure a storage backend for durability.)

**Some state lives in the dashboard database — deliberately.** The
BeyondTrust PRA tunnel state (the Terraform `sra` provider, via
[`services/terraform_pra_service.py`](../web_dashboard/services/terraform_pra_service.py))
is a security carve-out: raw `sra` state contains **live PRA /
vault-account credentials**, so the dashboard **scrubs those secrets and
stores the scrubbed state on the job/resource row in the database**, never
in the storage bucket. A remote backend would persist the *unredacted*
state and leak credentials into object storage; the scrubbed-state-in-DB
model keeps secrets out of the bucket while staying recreate-durable (it
lives in Postgres). That's why *some* state is in the database and *most*
is in the storage backend.

**Switching backends is guarded.** Changing your active storage backend
while deployments have live state would strand them, so the dashboard
provides an explicit migrate step
(`storage_service.migrate_terraform_state`) that copies the
`terraform-state/*` objects from the old backend to the new one.

Two operating rules follow:

- **Back up the state backend, not the container.** The durable record is
  the bucket (plus the DB for PRA state); the container's deploy
  directories are disposable working copies.
- **Don't run Terraform out-of-band** against these deploys. Hand-editing
  state (local dir or bucket object) desyncs it from the dashboard's job
  tracker, which reads outcomes from `Job.extra_data`, not by re-running
  `terraform refresh`.

Note the deliberate asymmetry with [config-management.md](config-management.md):
**Ansible runs are ephemeral by design; Terraform state is persistent
by necessity.** The runner that *does* the apply is short-lived (one
Terraform process, exits when done); the *state* that apply produces
outlives it — in the storage backend, or the DB for PRA tunnels.

---

## Workflow

A typical deploy:

1. **Form fill** — operator picks AMI/image, instance type, network on
   the per-cloud page (`/aws`, `/azure`, `/gcp`).
2. **Job created** — the dashboard creates a `Job` row in the DB,
   queues a background task, returns the job ID immediately.
3. **Provision Jumpoint** (if BeyondTrust enabled) — the cloud's
   ephemeral Jumpoint container is spawned first so PRA can register
   it before the user VM comes up. State for the Jumpoint deployment
   is recorded under the same job's extra_data.
4. **Terraform apply** — the per-cloud module is copied into a fresh
   `terraform/deployments/{job_id}/` working dir; `init` configures the
   backend (state → your storage backend) and attaches the cached
   providers; `apply` runs with the form variables passed via `-var`.
   Stderr / stdout are captured.
5. **Record outcome** — instance ID, IP addresses, AMI/image ID, all
   land in `Job.extra_data` for later destroy.
6. **Provision Shell Jump** (if BT enabled) — a separate Terraform
   apply against the BT `sra` provider, recorded with its own state.

A typical destroy:

1. **Job lookup** — dashboard finds the deploy job by instance name +
   job_type, reads `extra_data.bt_tf_state` (the scrubbed, DB-held Shell
   Jump state) and re-attaches to the instance state in the storage
   backend (`terraform-state/{job_id}/`).
2. **Shell Jump destroy first** — the Terraform PRA apply uses the
   stored tf_state to delete the Shell Jump cleanly.
3. **Instance destroy** — `terraform destroy -auto-approve` against
   the per-job state (in the storage backend; the working dir is
   re-materialised from the module template if the container lost it).
4. **Sibling-aware Jumpoint cleanup** — if no other active deploys
   reference the same Jumpoint, the cloud Jumpoint container is
   stopped too; otherwise it stays running for the others.
5. **Mark deploy job as destroyed** — `Job.extra_data["destroyed"] =
   true` so it's filtered out of "active" lists.

---

## Best practices

**Always destroy via the dashboard.** If you delete a resource through
the cloud console, the dashboard's state directory is now lying — it
still believes the resource exists. The next destroy will fail with a
"resource not found" error, the directory has to be hand-cleaned, and
any sibling-aware logic (Jumpoint cleanup) gets confused. The path is
designed to be closed.

**Tag everything you create.** The dashboard tags every resource with
`managed-by=dashboard` (cloud VMs) or `managed-by=dashboard-sandbox`
(sandbox infrastructure). If you bring your own tags via the deploy
form, that's fine; just don't drop the dashboard's. Tag-based cleanup
is what makes "delete every resource the dashboard created" possible
without an audit-log scrape.

**Use the sandbox bootstrappers for lab infra.** Don't try to
hand-build a VPC + subnets + IAM + secret store every time you spin up
a test environment. Run `scripts/sandbox/Linux/setup-aws.sh` (or the
PowerShell variant), get repeatable scaffolding in 90 seconds, tear
down with `rollback.sh --cloud all` when you're done.

**Keep templates minimal.** The Terraform modules in
[`terraform/`](../terraform/) intentionally do one thing each — `aws_instance`,
nothing more. Anything else (VPCs, IAM, security groups) is the
operator's problem before they hit deploy. Resist the temptation to
let the dashboard own the entire stack; it makes destroy paths
explode in scope and turns network-policy decisions into per-deploy
gambles.

**Pin Terraform versions.** The dashboard image bakes a specific
Terraform version (`hashicorp/terraform`); the per-deploy directories
inherit it. If a future image upgrade bumps Terraform, *existing
deployments still work* (Terraform reads state files written by older
versions), but state files written by newer versions are not
backwards-compatible. Don't downgrade once you've upgraded.

**Treat Packer images like git tags.** Build immutably, name
deterministically (`hardened-base-ubuntu-2026-04-12`), deploy from
named versions rather than `:latest`. The dashboard supports the
build/deploy split natively but doesn't enforce naming hygiene.

---

## Where this is heading on SaaS

Remote state with locking already ships in community (see
[State](#state-the-thing-that-makes-iac-work) above), as do pre-action
policy guardrails ([Policy Guardrails](policy-guardrails.md)). A few
things the community edition still leaves to the hosted edition — see
[docs/saas-comparison.md](saas-comparison.md) for the philosophy.

- **Continuous drift detection.** Community's view of a deployed VM
  is whatever was true at apply time; if someone resizes the instance
  in the cloud console, the dashboard doesn't notice. SaaS reconciles
  the Terraform state against live cloud state on a schedule and
  flags differences ("this EC2 instance no longer matches the
  module's `instance_type` — was it changed out-of-band?").
- **AI-assisted module refactoring.** Suggestions like "you have
  twelve almost-identical `ec2_instance` modules; here's a single
  parameterised module that replaces them" — same shape as the
  AI-assisted Ansible playbook generation the SaaS edition will
  offer for config management.
- **Post-apply compliance-as-code.** Community enforces policy
  *pre-action* — the OPA guardrails block a disallowed deploy before it
  starts ([Policy Guardrails](policy-guardrails.md)). SaaS adds the
  *post-apply* half: continuously evaluating already-deployed
  infrastructure against policy and flagging resources that have drifted
  out of compliance. Pre-action gate + post-apply scan = one policy
  intent, two enforcement points.

You can be productive on community indefinitely. Move to SaaS when
concurrent operator safety, drift surfacing, or compliance auditing
matter more than self-hosting flexibility.

---

## Troubleshooting

**Deploy fails with "InvalidAMIID.NotFound" / similar AMI/image error.**
The AMI you picked isn't accessible from your account in the chosen
region. Use the per-cloud page's AMI search or paste an AMI ID you
know your account can reach (the
"[Deploy from AMI ID](../web_dashboard/templates/aws/index.html#L15)"
button supports arbitrary IDs).

**Destroy fails with "Failed to load state file."**
This is a **local-backend fallback** symptom — the per-deploy working
directory was lost (container rebuilt without persistence) and there was
no storage backend holding the state. With a storage backend configured,
the state is remote and destroy re-attaches to it, so this doesn't happen.
If you're on local state: find the orphaned resource by tag
(`managed-by=dashboard`), delete it via the cloud console, mark the job
destroyed manually — then configure a storage backend to avoid the failure
mode.

**"Provider configuration not present" during apply.**
Provider cache wasn't pre-pulled. The dashboard image bakes Terraform
+ providers; if your image is older than the providers' release date,
re-pull or rebuild.

**Shell Jump destroy succeeds but the Jump still appears in PRA.**
The Terraform PRA `sra` provider deleted the resource via the API,
but PRA may take a few seconds to propagate the deletion to the UI.
If it persists past a minute, check the job's `extra_data.bt_error`.

**"State file mismatch" after re-running a failed apply.**
The previous run partially succeeded but the dashboard didn't record
the partial outputs. Manually destroy the partial cloud resources
(again, find by tag), then `rm -rf terraform/deployments/{job_id}`
inside the container, then re-deploy.

**Two operators deploying/destroying the same target at once.**
With a storage backend configured, the backend's native state lock (S3
`use_lockfile` / Azure blob lease / GCS) serialises them — the second
waits rather than corrupting state. Under the local-backend fallback there
is no lock, so avoid concurrent operations on the same deploy (or configure
a storage backend).
