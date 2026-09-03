# Cloud Ops Engineer

Owns infrastructure that is created and destroyed daily. Their estate has no stable
inventory — an instance that existed this morning may be gone, and the one that replaced it
has a different name, a different address and a fresh set of credentials.

Their access problem is **speed of onboarding**. Any process that requires a human to
register a new machine is a process that will be skipped under load, and every machine it
skips is an unmanaged root account with a network route.

## Why they care

The honest version of the cloud credential story is not "we have a vault". It is that
between a machine existing and a machine being under management there is a window, and in a
cloud estate that window opens hundreds of times a week. Close it by making onboarding part
of provisioning rather than a follow-up task.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Deploys the VM, database or cluster. The layers below are wired in the *same job*, not afterwards. |
| **PRA** | Shell Jump and Web Jump to something with no public IP, brokered rather than routed. |
| **Password Safe** | Takes ownership of the admin credential over the cloud's own control plane — no agent to install, no port to open. |
| **Entitle** | Time-boxed access, including to the cloud console itself. |

## Use cases

### One deploy, three PAM layers

Deploy a cloud VM and watch Shell Jump, Password Safe onboarding and an Entitle grant wire
themselves in a single job. The machine is under management before anyone could have logged
into it.

**Guide:** [Cloud VMs](../../../cloud-vms.md)

### Onboard a cloud VM with nothing installed on it

Rotate the admin credential on a VM with no agent and no inbound port, over the cloud's own
control plane — Systems Manager on AWS, Run Command on Azure, ssh-keys metadata on GCP. The
objection this answers is "we cannot install anything on those images".

**Guide:** [Password Safe](../../../integrations/password-safe.md)

### Time-boxed access to the cloud console itself

Grant an engineer the console for two hours through Entitle, then show the role binding
remove itself. Worth doing because most organisations have solved server access and left
standing administrators in the cloud IAM.

**Guide:** [Cloud identity JIT](../../../design/cloud-identity-jit.md)

### Bake a golden image once, promote it everywhere

Build a hardened image with Packer in one cloud and promote it to the others, so every VM in
every region starts from the same audited baseline.

**Guide:** [Image Management](../../../image-management.md)

### Guardrails: refuse the deploy, then reap it

Admission policy turns down a non-compliant request up front; the auto-delete timer removes
what did get built. The two halves of not accumulating privileged infrastructure by accident.

**Guide:** [Policy Guardrails](../../../policy-guardrails.md) ·
[Auto-delete Timer](../../../auto-delete-timer.md)

## What to enable

**Privileged Remote Access** and **Password Safe** cover the first two cards. Add **Entitle**
for the console-JIT story, and turn on **Admission control** and the **Auto-delete timer**
for the guardrails card — both default off, and the guardrails card will tell you so rather
than failing quietly.

At least one cloud must be configured. On a [POV instance](../../../pov-instance.md) the cloud
consoles are masked, so most of this focus reports as unavailable by design — that is the
tenancy split doing its job, not a fault.

## Talking to this buyer

They will be sceptical of anything that adds a step to a deploy. Frame every layer as
something that happens *inside* the pipeline they already run, and be specific that the
agentless onboarding path exists precisely because their images are not yours to modify.
