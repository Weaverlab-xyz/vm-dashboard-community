# DevOps Engineer

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to whoever owns the pipeline and the automation on it.

Owns the pipelines and the automation that runs against everything else. Their code has to
authenticate to servers, databases, clouds and registries — and unlike a person, it cannot be
handed a password at the moment it needs one.

Their access problem is **secrets in automation**. The path of least resistance is a
long-lived credential in a variable, a vault file, or a CI secret store, and the reason it
persists is that every alternative has historically been harder than the thing it replaces.

## Why they care

A static credential in a pipeline is not one risk, it is three: it does not expire, nobody
knows every place it has been copied to, and rotating it breaks builds — so it never gets
rotated. The fix is not a better place to hide it. It is for the credential to be **fetched
at run time and scoped to the run**, so there is nothing durable to leak.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | The targets the automation runs against, and the runner it runs on. |
| **PRA** | Reaching a private host from a runner without a route to it. |
| **Password Safe** | The credential the playbook looks up as it executes — nothing on disk, nothing in the repo. |
| **Entitle** | Accounts and grants that exist for the length of the job and are destroyed after. |

## Use cases

### A playbook with no credential in it

Run an Ansible playbook that looks its own credential up from Password Safe as it executes.
Nothing in the repository, nothing in the inventory file, nothing on disk when it finishes.

**Guide:** [Ansible](../../../integrations/ansible.md) ·
[Password Safe](../../../integrations/password-safe.md)

### A workload that mints its own cloud credential

Replace a long-lived access key with a short-lived one the workload requests when it needs
it — the secret nobody can leak because nobody is holding it.

**Guide:** [Workload Credentials](../../../integrations/workload-credentials.md)

### SSH accounts that exist only for the run

A pipeline requests an account, gets it for the length of the job, and the account is
destroyed on completion. There is no build user to audit, rotate or forget about.

**Guide:** [Entitle user JIT](../../../design/entitle-user-jit.md)

### A pipeline that authenticates with a certificate nobody has to renew

The credential a build presents to a deployment API is exactly as privileged as the password
it replaced, and it usually has none of the same governance: issued in 2023 by someone who
has left, sitting in a CI secret store, expiring on a date nobody recorded.

Issue one under approval instead, have the build retrieve it over the API with no human in
the loop, and — the step demonstrations skip — prove it completes a real mTLS handshake.
Then rotate it and show the build never notices.

**Guide:** [Certificates](../../../certificates.md)

### Every workload identity in a trust domain, inventoried and governed

The certificate above governs one identity at a time. A platform team running SPIFFE has
hundreds, issued automatically on attestation, and no list of them anywhere a security
team can read — which is the point of the model and also its blind spot.

Stand up a SPIRE trust domain and discover every registration entry as a managed account.
The number is the demonstration: eleven entries go in and eight come back, because an
agent identity is not a thing to vault and control-plane privileges sitting in a workload
entry are almost always a leftover. Those exclusions are counted separately, so a number
that climbs is somebody granting rights nobody reviewed.

Then mint an audience-scoped JWT-SVID for the one consumer that cannot run an agent —
deliberately bypassing attestation, deliberately inert until an operator names the
namespace it may mint in, and deliberately the only part of this that touches a private
key.

**Guide:** [SPIFFE and SPIRE](../../../spiffe.md)

### A build server that reaches the cluster without a kubeconfig

The trust domain above is the better answer and most customers cannot have it. Configuring a
cluster to accept a SPIFFE identity means passing `--authentication-config` to the API
server, and no managed control plane lets you — so on EKS, AKS and GKE that path is not
merely inconvenient, it is unavailable.

What those teams have instead is a long-lived kubeconfig sitting in a CI system: no expiry,
no revocation, and no record that anyone ever read it. Kubernetes cannot revoke a client
certificate either, and since 1.24 it no longer creates the forever-tokens people used to
reach for.

So broker a **bound ServiceAccount token** through Password Safe: the API server mints it,
Password Safe rotates and audits it, and the build fetches it once per run with the build id
attached to the retrieval. The demonstration is not the token — it is the scope. A Deployer
deploys into one namespace and is **refused** in every other; a Reader reads the whole
cluster and **cannot read a Secret**. Both refusals are asserted inside the shipped
playbooks, because a step in a runbook gets skipped and an assertion does not.

Say the limit out loud while you are there: the vault authenticates whoever can retrieve,
not the workload. Anyone who can retrieve *is* the workload. That is precisely the axis the
trust domain above wins on, and it is why both live on one page.

**Guide:** [Workload access to Kubernetes](../../../workload-kubernetes.md)

### A build that reaches AWS with no access key anywhere

The two stories above give a workload an identity inside a cluster. This one gives it access
to the cloud account — and it is the credential most customers actually have a problem with.
Ask how a build server reaches AWS and the answer is an access key pasted into a CI secret
store: no expiry, no revocation in practice, and no record of which build ever read it.

Mint one per run instead. Workload Credentials issues a short-lived assumed-role credential
against a dynamic secret, hands back a lease, and bills for the issuance — so the credential
that existed for that build stops existing afterwards.

The demonstration is the *second* run. Wait out the lease and make the same call: it is
refused, with nothing revoked and nobody rotating anything. An access key would still be
working. That refusal is asserted inside the shipped playbook rather than described, and the
wait is real — a demo that faked the clock would prove it can print an error, not that the
credential died.

Two things to say out loud while you are there. The scope lives in the dynamic secret's own
definition, not in the dashboard — so a secret that assumes an administrator role produces a
short-lived skeleton key and demonstrates nothing. And revocation is asymmetric: Azure leases
can be released early, **AWS leases cannot**, because STS will not withdraw a credential it
has already signed. On AWS the TTL is the only control there is, which makes a short one
matter more rather than less.

**Guide:** [Short-lived cloud credentials](../../../workload-cloud.md)

### A serverless function that fetches its secret at cold start

Deploy a cloud function with no environment secret and show it pull what it needs on first
invocation — the same pattern as the playbook, in a runtime with no filesystem to leave
anything on.

**Guide:** [Cloud Functions](../../../integrations/cloud-functions.md)

### What changed on this host since the last run

Configuration drift against the last known-good run: the question every incident review opens
with, answered without logging into anything.

**Guide:** [Configuration Management](../../../config-management.md)

## What to enable

**Configuration Management** (Ansible), **Password Safe** and **Entitle**. Configuration
Management additionally requires an active storage backend — it has nowhere to read a playbook
from otherwise — so see [Storage Management](../../../storage-management.md) before enabling it.

This focus is one of the few that works essentially unchanged on a
[POV instance](../../pov/README.md): Ansible, Password Safe, Entitle and the remote agent are
all profile-neutral.

## Talking to this buyer

They have heard "put it in a vault" before and it did not solve their problem, because the
pipeline still needed a credential to talk to the vault. Lead with the *bootstrap* question —
what authenticates the workload in the first place — because that is the part they have not
been offered a good answer to.
