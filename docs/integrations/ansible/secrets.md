# Secrets in a Remote Worker run

> **Audience:** operator · **Profile:** `both` · **Read this when:** a playbook needs a credential and you do not want it in the playbook.

Part of [Remote Worker](../ansible.md). Hardened per-provider lookups, Password Safe managed-account checkout, and in-playbook lookups.

## Using a Secrets-Management secret in a run

Beyond the SSH key, a run can pull secrets from
[Secrets Management](../../secrets-management.md) — a DB-stored secret or an external
vault reference (`aws_sm://`, `gcp_sm://`, `azure_kv://`, `bt_safe://`) — **without
the operator ever seeing the value**. The **Use a secret** panel on `/config-mgmt`
offers three bindings:

| Binding | Becomes | Runners |
|---|---|---|
| **Named variable** | an extra var (`-e`) — redacted from job output | local + cloud |
| **Become / sudo password** | `ansible_become_password` (Ansible `no_log`s it) | local + cloud |
| **SSH private key** | the connection key (replaces the configured key) | local + cloud |

Using a secret requires the **`secrets:use`** permission (admins and legacy
unrestricted users bypass). The use is audited — kinds + var names only, never the
source refs or values — and any resolved value is scrubbed from the job output.

### Cloud runners: hardened per provider (and the store requirement)

On the cloud runners the value is **not** placed in the task's plaintext env or on
the command line. Each secret is delivered through the provider's own secret
channel, and the container decodes a non-secret manifest into a `0600` vars file
before running `ansible-playbook -e @file`:

| Runner | Channel | Requirement |
|---|---|---|
| **ECS** (AWS) | container `secrets` → `valueFrom` (SM ARN); the **execution role** fetches it at launch | secret must live in **AWS Secrets Manager** (`aws_sm://…`); role needs `secretsmanager:GetSecretValue` |
| **Cloud Run** (GCP) | secret-env `secret_key_ref` (`version: latest`); the **service account** fetches it | secret must live in **GCP Secret Manager** (`gcp_sm://…`); SA needs `roles/secretmanager.secretAccessor` |
| **ACI** (Azure) | `secure_value` env (inline, hidden from the portal) | any secret — the value is injected inline |

Because ECS and Cloud Run **reference** a store secret rather than carrying its
value, a variable/become secret used on those runners must already live in that
cloud's store. If it doesn't, the run is **rejected up front** with an actionable
message — move it there via **Secrets → migrate**, then reference it as
`aws_sm://<name>` / `gcp_sm://<name>`. ACI has no such requirement. The SSH-key
secret always rides the existing `SSH_KEY_B64` channel and needs no migration.

### Managed-account checkout (BeyondTrust Password Safe)

When **Password Safe is enabled** (`password_safe_enabled`), a run can also use a
Password Safe **managed account** as the login identity — instead of referencing a
*stored* secret, the operator picks an account from a **live list** and the
dashboard checks out its credential **just-in-time** at run time. The operator
never sees the value; the checkout is scrubbed from output and audited exactly like
the secret path above (and needs the same **`secrets:use`** permission).

How to use it: on `/config-mgmt`, pick **Target → On-prem host (IP / hostname)** and
enter a system registered in Password Safe (a cloud VM's IP works too). The
dashboard looks up that host's managed systems + accounts and shows an account
picker (each tagged **[SSH key]** or **[password]**). Selecting one:

- sets `ansible_user` to the account name;
- injects its credential as the **connection** secret — an **SSH-key** account
  becomes the connection key; a **password** account becomes
  `ansible_ssh_pass` / `ansible_password` (Windows targets need
  `ansible_connection: winrm` in the play);
- optionally, a **second** managed account can be picked for the become/sudo
  password (`ansible_become_password`).

**Across many hosts (bulk runs).** The picker above pins `system_id` **and**
`account_id`, and both belong to one managed system — so the same reference cannot be
reused across a fleet: it would check out a *single machine's* credential and connect
to every host with it. Correct only if the account happens to be domain-linked, wrong
for a local account, and nothing would report which. A [bulk run](ansible-runner.md#bulk-runs-one-asset-many-targets)
therefore sends the account **name**, and each job resolves it against the host it is
actually configuring before checking anything out. Consequences:

- Each host checks out **its own** credential. Because the resolved reference replaces
  the submitted one wholesale, that host's own **[SSH key] / [password]** nature
  decides the checkout mode — the sample host's flag is never trusted.
- A host with no account by that name fails **only its own job**, with a message
  naming the host and the account.
- Matching accepts the `{user};{suffix}` form that cloud-native onboarding registers
  (the AWS Systems Manager plugin appends a scope suffix), so picking `svc-ansible`
  matches `svc-ansible;local`. Domain-linked accounts resolve too.
- The account list you choose from is read from one selected target as a **sample** —
  it supplies the names, not the ids.

**Not available for Kubernetes / database batches.** Those run a `localhost` play with
no SSH connection to authenticate, and the run path silently ignores the
connection-identity fields. A single run can absorb that quietly; a batch would leave
you believing a credential had been applied to every cluster — so `managed_account`,
`managed_become`, `secret_ssh_key_source` and `secret_become_source` are **refused**
outright for a non-VM batch. Named `secret_vars` are honored there and stay available.

**Local and Azure (ACI) runners** inject the credential inline — the local runner
via a `0600` vars file, ACI via `secure_value` — so a checked-out managed account
works on either out of the box.

**ECS and Cloud Run** *reference* a store secret (the task identity fetches it at
launch), which a checked-out (ephemeral) credential has none of — so they're
**rejected unless "Ephemeral cloud secrets" is enabled** (Settings → Ansible). When
on, the credential is written to that cloud's store as a short-lived, RBAC-locked
secret, injected via the provider's channel, then force-deleted after the run — see
[Ephemeral cloud secrets](../../secrets-management.md#ephemeral-cloud-secrets).

SSH-password targets require `sshpass` in the runner image (already true for the
built-in on-prem SSH path). The lookup and checkout go through `ps-cli`,
authenticated by the configured Password Safe OAuth client (`pscli_api_url` /
`pscli_client_id` / `pscli_client_secret`).

### In-playbook Password Safe lookup (`beyondtrust.secrets_safe`)

The managed-account checkout above is **out-of-band**: the dashboard fetches the
credential and injects it. The **complementary** pattern is an *in-playbook* lookup —
the play fetches its own secrets from Password Safe at runtime via the
[`beyondtrust.secrets_safe`](https://galaxy.ansible.com/ui/repo/published/beyondtrust/secrets_safe/)
Galaxy collection's `secrets_safe_lookup` plugin (and the `beyondtrust.password_safe`
management modules). Use it for **app** secrets, API tokens, or DB credentials a task
consumes — as opposed to the **connection** credential, which the checkout path handles.

Ready-to-run starters live in
[`examples/playbooks/password-safe/`](../../../examples/playbooks/password-safe).

**Several shipped samples support this optionally.** Rather than only living in the
dedicated demos, the plays that consume an app secret each declare an optional
`…_secret` var — set it to a SECRET path (`folder/title`) and the value is fetched
mid-run; leave it blank and the play behaves exactly as before:

| Playbook | Optional var |
|---|---|
| `windows/win-create-local-admin.yml` | `new_admin_password_secret` |
| `database/postgres-create-role.yml` | `target_role_password_secret` |
| `database/mysql-create-user.yml` | `target_user_password_secret` |
| `portainer/*.yml` | `portainer_pat_secret` |

Two implementation notes that matter if you adapt the pattern:

- The fetch writes to a private `_ps_*` fact and is resolved at the use site, **never**
  back onto the caller-supplied variable. Ansible extra vars outrank `set_fact`, so
  writing back would be *silently ignored* whenever the value was also supplied via an
  extra var or a "Use a secret" binding — the play would quietly use the wrong one.
  When both are set, the Password Safe path wins.
- Connection credentials (`ansible_password`, become, SSH keys) deliberately stay with
  the run-form panel, which covers cases a lookup can't (SSH keys, `sshpass`, ephemeral
  cloud secrets). `tests/test_playbook_ps_lookup.py` pins these invariants across the
  samples.

**Auto-injected credentials.** The lookup runs on the Ansible controller (the runner
container) and reads `PASSWORD_SAFE_API_URL` / `PASSWORD_SAFE_CLIENT_ID` /
`PASSWORD_SAFE_CLIENT_SECRET`. When **Password Safe is enabled** (`password_safe_enabled`)
and the ps-cli OAuth client is configured, the dashboard **auto-injects** those three env
vars into **every** runner (Local, ECS, ACI, Cloud Run) — reusing the same
`pscli_api_url` / `pscli_client_id` / `pscli_client_secret` config as the checkout path,
so there's nothing extra to set per run.

- The client secret rides the **same per-run env channel each runner already uses for the
  SSH private key** — the ECS `runTask` override (not the task-def revision history), the
  Cloud Run job env, an ACI `secure_value`, or a `0600` `--env-file` locally — never on a
  command line, and scrubbed from job output. **No `ansible_cloud_ephemeral_secrets_enabled`
  gate is required** (that gate is only for the checked-out managed-account path).
- Both runner images (`chrweav/ansible-winrm`, `chrweav/ansible-cloud`) ship the two
  collections (via `beyondtrust-bips-library`), so the lookup resolves on either — rebuild
  + push them before relying on it.
- The OAuth client needs the usual API-registration permissions (Secrets → Read,
  Requests → Create, Credentials → Read, plus scope for the paths the play touches).

---
