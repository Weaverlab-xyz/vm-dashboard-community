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
