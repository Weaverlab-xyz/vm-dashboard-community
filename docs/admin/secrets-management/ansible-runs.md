# Using a secret in an Ansible run

> **Audience:** operator · **Profile:** `both` · **Read this when:** a playbook needs a secret and nobody should have to read it to run the job.

Part of [Secrets Management](../secrets-management.md).

Stored secrets aren't only consumed by cloud-credential lookups — an operator can
inject one directly into a **Config Management** run without ever seeing the value.
On `/config-mgmt` the **Use a secret** panel offers three bindings, resolved
just-in-time, scrubbed from job output, and audited (kinds + var names only, never
the value):

| Binding | Becomes | Runners |
|---|---|---|
| Named variable | an extra var (`-e`) | local + cloud |
| Become / sudo password | `ansible_become_password` (`no_log`) | local + cloud |
| SSH private key | the connection key | local + cloud |

- **Permission:** requires **`secrets:use`** (admins bypass).
  The value is never shown, stored on the job, or placed on a command line.
- **Cloud runners are hardened per provider:** the value is delivered through
  ECS `valueFrom` / Cloud Run secret-env / ACI `secure_value`, not plaintext task
  env. Because ECS/Cloud Run *reference* a store secret, a variable/become secret
  used there must already live in that cloud's store (`aws_sm://` / `gcp_sm://`) —
  otherwise the run is rejected with a *move it via Secrets → migrate* message.

**Managed-account checkout (Tier 3):** with BeyondTrust enabled, the same panel
also lets the operator pick a Password Safe **managed account** from a live list
and use it as the login identity — the credential is checked out just-in-time (an
SSH-key account → the connection key; a password account → `ansible_ssh_pass`).
Managed-account checkout works on the **local and Azure (ACI) runners** (both
inject inline); on **ECS / Cloud Run** it requires the opt-in below.

Full operator detail lives in
[Ansible secrets → Using a Secrets-Management secret in a run](../../platform/ansible/secrets.md#using-a-secrets-management-secret-in-a-run).

## Ephemeral cloud secrets

The ECS and Cloud Run runners inject a secret by *referencing* one in that cloud's
store (the task identity fetches it at launch); a just-in-time managed-account
credential has no such store entry. Enabling **Ephemeral cloud secrets**
(Settings → Ansible, **off by default**) bridges that: for the run, the dashboard

1. checks the credential out of Password Safe,
2. writes it to the cloud store as a **short-lived, RBAC-locked** secret,
3. injects it via the provider's secret channel (ECS `valueFrom` / Cloud Run
   secret-env), and
4. **force-deletes** it in a `finally` when the run ends.

**RBAC lock-down — only the runner can read it:**

| Cloud | How access is restricted |
|---|---|
| **GCP** | The Cloud Run job runs as a configured **runner service account**, and `roles/secretmanager.secretAccessor` is granted **on that one secret** to **only** that SA (no project-level accessor). Requires `gcp_ansible_runner_service_account`. |
| **AWS** | A resource policy scopes `GetSecretValue` to the **ECS execution role**. For a *cryptographic* lock, set `ansible_ephemeral_kms_key_id` to a CMK whose key policy grants `kms:Decrypt` to only that role — then reading requires decrypt permission even for account IAM admins. |

**Rotate-on-release (best-effort).** The dashboard uses a **long-enough Password
Safe request** (`ansible_managed_request_duration_min`, default 60 min — must
outlast the run) so the request is still open to flag **rotate-on-check-in** and
then **check it in** after the run. Rotation isn't enforceable (it depends on the
account being auto-managed), so the reliable backstop is enabling **Change Password
After Release** on the managed account — either way a missed cleanup then leaves a
rotated, dead credential.

**Garbage collection.** Each run reaps its own ephemerals; a sweeper (on startup and
before each run) force-deletes any tagged ephemeral older than the TTL, covering a
crash between create and cleanup. The TTL is **GC TTL** on the same panel
(`ansible_ephemeral_secret_ttl_min`, default 30 minutes), and must be at least the longest
run, or a sweep deletes a secret a task is still reading.

**The trade-off.** This briefly writes a PAM-vaulted credential into the cloud store
(a second trust domain) for the task's lifetime. Mitigations shrink but don't erase
that: the per-secret RBAC lock, optional CMK, short request duration, rotate-on-
check-in / Change-After-Release, force-delete + GC, and dashboard-side audit of the
use. If that trade-off isn't acceptable, use the local or ACI runner (inline, no
store copy) — the default.

Full operator detail:
[Ansible secrets → Managed-account checkout](../../platform/ansible/secrets.md#managed-account-checkout-beyondtrust-password-safe).

## Collect from the dashboard (no store copy)

The alternative to the copy above, and the one to prefer when the runner can reach the
dashboard. With **Collect credentials from the dashboard** on (Settings → Ansible,
**off by default**), the credential never goes into a cloud store. Instead:

1. The dashboard checks it out and parks it under a **single-use, ten-minute token**. Only
   the token's SHA-256 is stored, and the credential is encrypted with the dashboard's own
   key.
2. The task gets the token in its per-run environment: the RunTask override on ECS, never
   the task definition. It also gets a small fetch script.
3. The task **proves its own cloud identity, bound to that token**:
   - **ECS:** a presigned `sts:GetCallerIdentity`, signed with the **task role**. The
     token's hash is in a signed header.
   - **Cloud Run:** a Google-signed ID token for the runner **service account**. The
     token's hash is in its audience.
4. The task calls `POST <agent URL>/api/agent/runner-credential`. The dashboard burns the
   token first, then sends the presigned request to STS (only ever a regional STS host) or
   verifies the Google token. It checks that the identity is the configured runner and
   answers with the credential **sealed** to a key the task generated (the
   [remote agents'](../remote-agents/credentials.md#central-storage-with-spire-the-recommended-model)
   format).
5. The task merges it into the run's `0600` vars file. The grant is deleted when the run
   ends, whether redeemed or not.

| Needs | Why |
|---|---|
| The agent URL pinned (Settings → Remote Agents) and reachable from the runner | The task calls `/api/agent/runner-credential` there. That vhost publishes only `/api/agent/*`. |
| ECS: `ansible_ecs_task_role_arn` | The identity the task proves. It needs no permissions; it only has to be *who* the task runs as. |
| Cloud Run: `gcp_ansible_runner_service_account` | The same, for Cloud Run. |

A copied token is worth nothing without the runner's own cloud identity, and a proof made
for one token is refused with another. Every collection and refusal is audited
(`agent.runner_credential` / `agent.runner_credential_refused`). Rotate-on-check-in still
applies. When both settings are on, collect-from-dashboard wins wherever it is fully
configured. It is **not SPIFFE**: a Fargate or Cloud Run task cannot run a SPIRE agent, so
the platform's own attestation is the stronger statement.
