# BeyondTrust Password Safe

## What is it?

**BeyondTrust Password Safe / Secrets Safe** is on-demand checkout of SSH keys and
passwords. Target credentials (AWS keys, Azure service principal secrets, SSH private
keys) are fetched at the moment the dashboard needs them and discarded after use, rather
than stored in the dashboard's encrypted database. Driven by `ps-cli`.

It also **onboards** the resources the dashboard builds — VMs, cloud databases, and
Kubernetes ServiceAccount tokens — as managed systems + accounts, so their credentials
rotate on the tenant's schedule instead of living forever as whatever the deploy minted.

Gated by `password_safe_enabled`. This is one of three independently-gated BeyondTrust
products; see [BeyondTrust Integrations](beyondtrust.md) for the map, and
[Privileged Remote Access](privileged-remote-access.md) for the jump-item and Gateway
half of the story.

---

## Use cases

- **Vault-backed cloud credentials** — instead of entering AWS access keys,
  Azure service principal secrets, or SSH private keys into the dashboard
  (where they would be stored encrypted in the application database), the
  dashboard fetches them from Password Safe at runtime. Rotate credentials in
  one place; the dashboard always gets the current value.
- **Audit trail** — every secret checkout creates a Password Safe audit record.
  You know who (the dashboard service account) requested what credential and
  when.
- **SSH key checkout for cloud VMs** — the Ansible config-management runner and
  BeyondTrust Gateway container retrieve SSH keys from Password Safe managed
  accounts, so the private key never touches the host filesystem.
- **In-playbook secret lookup (Ansible)** — a config-management playbook can fetch
  its own secrets/managed-account passwords from Password Safe at runtime via the
  `beyondtrust.secrets_safe` Galaxy collection. The dashboard reuses this same OAuth
  client (`pscli_*`) — auto-injecting it into the runner as `PASSWORD_SAFE_*` — so no
  separate credential is needed. See
  [integrations/ansible.md](ansible.md#in-playbook-password-safe-lookup-beyondtrustsecrets_safe)
  and [examples/playbooks/password-safe/](../../examples/playbooks/password-safe/).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust Password Safe | Secrets Safe licence; hosted or on-prem |
| `ps-cli` | Ships as the `beyondtrust-bips-cli` **pip dependency** (`web_dashboard/requirements.txt`), so it is present in any image built from this repo — there is no separate binary install step |

---

## Setup

### Step 1 — Password Safe OAuth application (ps-cli)

ps-cli authenticates to Password Safe with an OAuth 2.0 client-credentials
grant.

1. In **Password Safe** → **Configuration** → **API Registration** →
   **Add API Registration**:
   - Authentication type: **Client Credentials**
   - Copy the **Client ID** and **Client Secret** displayed after creation.

2. Assign the registration the following permissions (minimum):
   - **Secrets** → Read
   - **Requests** → Create
   - **Credentials** → Read

   Add Managed System / Managed Account scope for any accounts the dashboard
   will check out SSH keys from.

### Step 2 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

The wizard Step 5 lists optional integrations. Toggle **Password Safe** on and fill in
the fields.

**Option B — Settings → Integrations (after first run)**

1. Open **Settings** → **Integrations** → **Password Safe** → toggle on
   (`password_safe_enabled`).
2. Fill in the **API connection** section:

   | Field | Example |
   |---|---|
   | Password Safe URL | `https://ps.company.com` |
   | OAuth Client ID | (from API Registration) |
   | OAuth Client Secret | (from API Registration) |
   | API run-as account | The BeyondInsight user the OAuth client runs as — required by the Password Safe Terraform provider for VM onboarding |

3. Click **Save**. No container restart is required.

> These are **Settings keys**, not environment variables. The dashboard's config store
> reads from the database only; exporting an equivalently-named env var has no effect.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Vault-backed cloud credentials** | AWS, Azure, and SSH credentials resolved from Password Safe at runtime rather than stored in the application database |
| **SSH key checkout** | Ansible and BT Gateway tasks retrieve SSH private keys from Managed Accounts on demand |
| **Managed-account checkout for playbook runs** | A Config-Management run can use a Password Safe managed account as its login identity — the operator picks an account from a live list and the credential is checked out **just-in-time** at run time, never shown and scrubbed from job output. See [below](#managed-account-checkout-for-config-management-runs) |
| **Resource onboarding** | VMs and cloud databases the dashboard builds are onboarded as Password Safe managed systems + accounts, and removed again on destroy |
| **Hypervisor credentials for a remote agent** | An on-prem agent brokering vCenter/Proxmox/Hyper-V can hold no credential at all: the dashboard checks one out per job, seals it to that agent, checks it back in and rotates it on release. See [below](#hypervisor-credentials-for-a-remote-agent) |
| **Secret audit log** | Every checkout creates an immutable record in Password Safe |

PRA Vault accounts minted for tunnels can themselves be onboarded here for rotation —
see [Privileged Remote Access](privileged-remote-access.md).

### Managed-account checkout for Config-Management runs

Instead of referencing a *stored* secret, an operator running a playbook can pick a
Password Safe **managed account** and have its credential checked out just-in-time. Two
details are worth knowing because they are not obvious:

- **Across many hosts, the account is matched by name.** A managed account reference
  pins a system id *and* an account id, both specific to one managed system — reusing
  one across a fleet would check out a single machine's credential and connect to every
  host with it. A [bulk run](../config-management.md#bulk-runs-from-the-inventory)
  therefore sends the account **name**, and each job resolves it against the host it is
  configuring, so every host checks out its own credential.
- **On the ECS / Cloud Run runners it needs an opt-in.** Those runners *reference* a
  store secret rather than taking a value inline, which a just-in-time credential has
  no place in — so they are rejected unless **Ephemeral cloud secrets** is enabled, at
  which point the credential is briefly written to that cloud's store as a short-lived,
  RBAC-locked secret and force-deleted after the run.

Full walkthrough in
[Ansible → Managed-account checkout](ansible.md#managed-account-checkout-beyondtrust-password-safe).

### Hypervisor credentials for a remote agent

A [remote agent](../remote-agents.md) brokering hypervisor operations inside a private network
has two ways to use Password Safe, and they differ in which host holds the OAuth client:

| | Agent-side checkout | Dashboard-side checkout |
|---|---|---|
| Set in | `connections.yaml` → `ps_managed_account: <id>` | Connections page → `ps_account://<id>`, plus `dashboard_secret: true` in `connections.yaml` |
| Password Safe client lives on | the agent host, in `passwordsafe.yaml` | the dashboard, which already has one |
| Hypervisor password at rest | nowhere | nowhere |
| Rotate on release | no | **yes**, by default |
| Needs | nothing extra | agent image ≥ 2.1.0 |

The second leaves the on-prem host holding nothing but the agent's own identity key. That
matters because the OAuth client is usually entitled to more than the single account it is
being used for, so it is the more valuable secret of the two — and it is the one sitting on
the least-defended machine.

The two are **mutually exclusive** per connection: declare both and the agent refuses the job
rather than picking, because there would be no way afterwards to say which credential a job
had used.

Practical notes:

- **Duration** comes from `agent_ps_checkout_duration_min` (default 45). It must outlast the
  job *plus* the reaping window — if an agent container is killed, the request is released
  once the stale-job reconcile has failed the job (~10 min) and the next sweep has run
  (~1 min). A request that expires before then is closed by Password Safe itself, which is
  safe, but the release then does not appear as the dashboard's doing.
- **Approval-required accounts do not work unattended.** A `4034` fails the job rather than
  hanging, and the request it opened is checked straight back in so it does not hold the
  account's concurrent-request slot. Use auto-approve access policies.
- **Concurrent jobs on one account share a request.** `ConflictOption=reuse` is deliberate,
  so the release is reference-counted: the last job to finish is the one that checks in and
  rotates. Otherwise the first to finish would change the password under a live sibling.
- **Rotation can be turned off** with `agent_ps_rotate_on_release=false`, for the case where
  Password Safe is not the sole owner of that account and something else is configured
  statically with the same password.

---

## Password Safe VM onboarding (managed systems)

When enabled, each freshly built **Linux** VM can be onboarded into Password Safe as a
**managed system + managed account** via a per-deploy **"Onboard into Password Safe"**
checkbox on the AWS / Azure / GCP deploy forms. Turn the capability on under **Settings →
Integrations → Password Safe → Resource registration (VMs)** (`passwordsafe_registration_enabled`).
The functional account + workgroup must already exist in Password Safe; the dashboard
resolves them over the public API and creates the managed system/account with Terraform.

> This section is the authoritative reference for **VM** onboarding methods. For the full
> cloud-VM deploy story (provisioning, PRA Shell Jump, Entitle) see [Cloud VMs](../cloud-vms.md).

Three onboarding methods, chosen per cloud:

### AWS — AWS Systems Manager custom plugin (cloud-native, default)

The recommended path. Password Safe manages the Linux EC2 instance over **AWS SSM
`SendCommand`** instead of SSH, so you need **no per-VPC Resource Broker and no SSH
line-of-sight** — one Password Safe node (or a single Cloud Resource Broker on EC2) can
manage Linux instances across many accounts/VPCs.

The dashboard creates the managed system with **DNS name `{instance-id}:{region}`** (e.g.
`i-0eaa6a10886717ed:us-east-1`, the field the plugin parses) on the custom-plugin platform,
and a managed account named **`{managed_account_name};{suffix}`**. The account's credential
is an SSH private key that **Password Safe mints over SSM on a credential change** — it is
not set at creation. Auto-management rotates it on schedule; optionally the dashboard can
trigger an immediate **Change Password** right after onboarding
(`passwordsafe_ssm_change_password_on_register`, off by default).

**Prerequisites (one-time, admin):**

- Upload the **AWS Systems Manager** `.PSPLUGIN` in BeyondInsight → **Configuration →
  Privileged Access Management → Platform Plugins**.
- Create a **functional account on the *AWS Systems Manager Custom Plugin* platform** and
  point the dashboard's **Functional account — AWS** at it. Its platform is what binds the
  managed system to the plugin.
  - **IAM-user mode** (suffix `local`): the functional account password is
    `{AccessKeyID}:{AccessKeySecret}` for an IAM user with `ssm:SendCommand`,
    `ssm:ListCommandInvocations`, `ssm:GetCommandInvocation`.
  - **EC2 mode** (cross-account Resource Broker on EC2): set **SSM account suffix** to the
    remote-account **AssumeRole ARN** (`{name};arn:aws:iam::…:role/…`); auth is the broker
    EC2 instance's IAM role, so the functional account holds only placeholder credentials.
- The instance must already be **SSM-managed** — the deploy attaches
  `ec2_ssm_instance_profile`, which must grant `AmazonSSMManagedInstanceCore`. Confirm the
  instance appears in **Fleet Manager** before onboarding.

### Azure — Azure VM SSH Rotation custom plugin (cloud-native, default)

The recommended path for Azure. Password Safe writes the key onto the VM over **Azure VM
Run Command** (through the Azure control plane) instead of SSH, so you need **no Resource
Broker and no SSH line-of-sight** — one Password Safe node can manage Linux VMs across many
resource groups and regions. This is the Azure counterpart of the AWS Systems Manager path;
plugin internals are documented in **`Beekeeper-AzureVmSshRotation.docx`**.

The dashboard creates the managed system with **address
`tenantId/subscriptionId/resourceGroup/vmName`** (tenant + subscription from the dashboard's
Azure config, resource group + VM name from the deploy — the field the plugin parses) on the
custom-plugin platform, and a managed account named after the baked-in **`adminuser`** Linux
user (no `;suffix`). The account's credential is an SSH key the plugin **generates and writes
onto `adminuser`'s `~/.ssh/authorized_keys` via Run Command** on a credential change. Because
`adminuser` has no key baked in, the dashboard triggers an initial **Change Password** right
after onboarding by default (`passwordsafe_azure_change_password_on_register`, on) so the
account is immediately usable.

**Prerequisites (one-time, admin):**

- Upload the **Azure VM SSH Rotation** `.PSPLUGIN` in BeyondInsight → **Configuration →
  Privileged Access Management → Platform Plugins**.
- Create a **functional account on the *Azure VM SSH Rotation Custom Plugin* platform** and
  point the dashboard's **Functional account — Azure** at it. Its platform is what binds the
  managed system to the plugin. The credentials are the Azure service principal:
  **Username = Application (client) ID**, **Password = client secret**.
- Grant that service principal **Virtual Machine Contributor** on the target resource group
  (covers `Microsoft.Compute/virtualMachines/read` + `runCommand/action`). You may reuse the
  service principal the dashboard already uses to deploy Azure VMs — it qualifies.
- The image must be built with the **bt-ready** provisioner so the `adminuser` account exists
  on the VM (the plugin `chown`s the key to it; it does not create the account).

### GCP — GCP VM SSH Rotation custom plugin (cloud-native, default)

The recommended path for GCP. Password Safe writes the public key into the GCE instance's
**`ssh-keys` metadata** (through the Compute Engine API); the in-guest Google guest agent then
propagates it to the user's `~/.ssh/authorized_keys`, so you need **no Resource Broker and no
SSH line-of-sight** — one Password Safe node can manage instances across many projects and
zones. This is the GCP counterpart of the AWS Systems Manager / Azure paths; plugin internals
are documented in **`Beekeeper-GcpVmSshRotation.docx`**.

The dashboard creates the managed system with **DNS name `projectId/zone/instanceName`**
(project from the dashboard's GCP config, zone + instance name from the deploy — the field the
plugin parses) on the custom-plugin platform, and a managed account named after the baked-in
**`adminuser`** Linux user (no `;suffix`). The account's credential is an SSH key the plugin
**generates and writes into the instance's `ssh-keys` metadata** on a credential change. Because
`adminuser` has no key baked in, the dashboard triggers an initial **Change Password** right
after onboarding by default (`passwordsafe_gcp_change_password_on_register`, on) so the account
is immediately usable.

**Prerequisites (one-time, admin):**

- Upload the **GCP VM SSH Rotation** `.PSPLUGIN` in BeyondInsight → **Configuration →
  Privileged Access Management → Platform Plugins**.
- Create a **functional account on the *GCP VM SSH Rotation Custom Plugin* platform** and point
  the dashboard's **Functional account — GCP** at it. Its platform is what binds the managed
  system to the plugin. The credentials are a Google **service account**:
  **Username = service-account email**, **Password = the full service-account JSON key**.
- Grant that service account **`roles/compute.instanceAdmin.v1`** on the target project (covers
  `compute.instances.get` / `setMetadata` / `list` and `compute.zoneOperations.get`).
- **OS Login must be disabled** on the target instances/project — GCE ignores instance
  `ssh-keys` metadata when OS Login is enabled, so the plugin's updates would have no effect.
  (Dashboard-built VMs have OS Login off by default.)
- The image must be built with the **bt-ready** provisioner so the `adminuser` account exists
  on the VM (the guest agent syncs metadata to that existing user; it does not create it).

### AWS / Azure / GCP when set to SSH — traditional managed system

A managed system keyed by hostname/IP on an SSH platform; the dashboard pushes the VM's own
SSH private key into the managed account and `passwordsafe_ssh_key_enforcement_mode` enforces
key-only auth. This requires SSH line-of-sight from a Resource Broker / Gateway. Select it per
cloud via the `*_registration_method` key (set to `ssh`).

### Configuration keys — VM onboarding

| Key | Default | Notes |
|---|---|---|
| `passwordsafe_registration_enabled` | `false` | Global capability flag (also per-deploy opt-in) |
| `passwordsafe_workgroup` | — | Workgroup name or id the managed system lands in |
| `passwordsafe_vm_functional_account_aws` / `_azure` / `_gcp` | — | Functional account per cloud (for AWS+SSM, the custom-plugin account) |
| `passwordsafe_managed_account_name` | `adminuser` | The onboarded account (the `{name}` part for SSM) |
| `passwordsafe_aws_registration_method` | `ssm` | AWS method: `ssm` (AWS Systems Manager plugin) or `ssh` |
| `passwordsafe_ssm_account_suffix` | `local` | SSM account-name suffix; an AssumeRole ARN for EC2 cross-account mode |
| `passwordsafe_ssm_change_password_on_register` | `false` | Trigger an initial Change Password after onboarding (mints the key now) |
| `passwordsafe_azure_registration_method` | `azurevm` | Azure method: `azurevm` (Azure VM SSH Rotation plugin) or `ssh` |
| `passwordsafe_azure_change_password_on_register` | `true` | Mint `adminuser`'s first key over Run Command right after onboarding |
| `passwordsafe_gcp_registration_method` | `gcpvm` | GCP method: `gcpvm` (GCP VM SSH Rotation plugin) or `ssh` |
| `passwordsafe_gcp_change_password_on_register` | `true` | Mint `adminuser`'s first key into GCE `ssh-keys` metadata right after onboarding |
| `passwordsafe_ssh_key_enforcement_mode` | `2` | SSH method only — 0 none / 1 auto / 2 strict |
| `passwordsafe_application_host_id` | `0` | SSH method only — >0 routes via a broker/application host |

Off-boarding is automatic: destroying the VM removes the managed system + account
(Terraform destroy from the stored state). Onboarding failures are **non-fatal** — they are
recorded on the job (`ps_error`) but never fail the deploy.

---

## Kubernetes ServiceAccount token rotation

A cluster's PRA k8s tunnel can inject a ServiceAccount bearer token at session launch
([Kubernetes → Access & identity](../kubernetes.md#access--identity)). The dashboard used to
mint that token once and never touch it again. This makes it a Password Safe **managed
account** so it rotates on the tenant's schedule, and keeps the PRA Vault copy current.

> This path needs **both** products: `password_safe_enabled` for the rotation itself and
> `pra_enabled` for the tunnel identity the token belongs to. With PRA off there is no
> injected ServiceAccount to manage.

Two custom plugins, both imported by hand:

| Plugin | Platform (default name) | Role |
|---|---|---|
| Kubernetes Service Account Token | `Kubernetes Service Account Token` | Rotates the token in the cluster |
| PRA Vault Token | `PRA Vault Token` | Writes the rotated value into the PRA Vault `opaque_token` account |

Enable **Kubernetes Token Rotation** in Settings → Integrations → Password Safe, then use the
per-cluster **Token rotation** button on `/k8s` (or tick the box on the provision form).
Registration applies the rotator RBAC, creates the managed system + account, registers the
PRA Vault Token account, **syncs** it to the token account, rotates once to prove the path,
and finally deletes the dashboard-minted `<sa>-token` Secret.

**The registration rotation is not optional here.** A managed account cannot be *created*
holding a bearer token: the REST path that sets a password on create caps it at 128
characters and a ServiceAccount JWT is 800–1,200, so the account starts out holding a
throwaway placeholder. (The cap is specific to that path — a plugin's rotation write-back
carries multi-KB values, which is how the SSH-key plugins store 3.2 KB private keys.) The
first rotation is therefore what puts a real credential in the vault, and turning
`k8s_ps_token_change_on_register` off does not skip it — registration overrides the setting
and warns, because a vault serving a placeholder to the PRA tunnel looks exactly like a
working registration.

The sync is the step that matters, and it is a Password Safe feature rather than anything the
dashboard runs: the PRA Vault account becomes a *subscriber* of the token account, and a
managed account and its subscribers always share an identical credential. The link is created
before the registration rotation on purpose — a failure there has changed nothing in the
cluster, whereas rotating first and failing to link would leave PRA holding a value that was
just revoked and that nothing would refresh.

**Why that last step matters.** The plugin's rotation sweep selects Secrets by *its own*
labels, so the dashboard-minted Secret is never swept — leaving it in place would mean
rotation revokes nothing and a permanent cluster-admin bearer token stays valid forever. The
ordering is deliberate too: it is deleted only after PRA carries a Password-Safe-issued
token, because until then it is the credential live sessions are using.

### Managed system address

The address is the plugin's only per-cluster configuration surface (a `.psplugin` is
checksum-sealed, so its packaged settings cannot be edited), capped at 249 characters:

```
eks;<region>;<clusterName>[;option…]
aks;<subscriptionId>;<resourceGroup>;<clusterName>[;option…]
gke;<projectId>;<location>;<clusterName>[;option…]
k8s;<apiServerUrl>[;option…]
```

The dashboard builds it from the cluster row plus its deploy job's Terraform variables. For
a **registered** cluster those are unknown, so the modal accepts a cloud cluster name and
(for AKS) a resource group. **OKE and on-prem clusters use the generic `k8s;` form** — the
plugin has no OCI provider. For GKE the `location` must be the **zone** for a zonal cluster
and the **region** for a regional one; mixing them is the documented cause of a 404.

Options appended automatically: `;longlived` or `;bound` (+ `ttl=`), `;ns=<namespace>`, and
anything in `k8s_ps_token_address_options` (e.g. `dnsEndpoint=true`, `serverName=`,
`allowHostnameMismatch=true`, `ca=` on generic addresses only).

### Functional accounts

One per cloud, created by the operator — the dashboard references them by name and never
holds a cloud secret. The managed system inherits the functional account's platform, so a
functional account on the wrong platform is refused with the platform named.

| Cloud | Username | Password |
|---|---|---|
| GKE | service account email (or `impersonate:<target>`) | **base64 of the whole JSON key file** |
| EKS | AWS access key id (or `InstanceProfile` / `WebIdentity`) | `<secret>` or `<secret>:<sessionToken>` |
| AKS | `SP:<tenantId>:<clientId>` — tenant **first** | Entra client secret (`-` for managed identity) |
| Generic / OKE | `token`, `cert` or `kubeconfig` | bearer token, PEM/PKCS#12, or `b64kubeconfig:<base64>` |
| PRA Vault Token | PRA OAuth **Client ID** | PRA OAuth **Client Secret** |

### In-cluster rotator RBAC

Applied automatically (`k8s_ps_rotator_apply_rbac`). LongLived needs `serviceaccounts`
get/list + `secrets` create/get/list/delete; Bound needs `serviceaccounts/token` create and
**no** access to Secrets at all. The ClusterRole always applies; the binding only when a
subject is configured, because **the subject differs per cloud and is the single most common
onboarding mistake**:

| Cloud | Binding subject | Config key |
|---|---|---|
| GKE | the service account's **email** | `k8s_ps_rotator_gke_sa_email` (blank → derived from the functional account) |
| AKS | the service principal's **object id** (`oid`), *not* the client id | `k8s_ps_rotator_aks_sp_object_id` |
| EKS | the username the access entry maps the principal to | `k8s_ps_rotator_eks_username` |
| Generic / OKE | a bootstrap ServiceAccount | `k8s_ps_rotator_bootstrap_sa` / `_namespace` |

On EKS the access entry is created too when `k8s_ps_rotator_eks_principal_arn` is set —
without it the binding's `User` subject matches nothing and the API server returns 401,
which is invisible from inside the cluster. The dashboard never edits `aws-auth`; a bad edit
there can lock every principal out. If the ARN is unset the job result prints the command:

```
aws eks create-access-entry --cluster-name <cluster> \
  --principal-arn <arn> --type STANDARD --username passwordsafe-rotator
```

**The cluster's authentication mode must include `API`, or there is no access-entry API to
call.** EKS defaults new clusters to `CONFIG_MAP`, where the command above is rejected and
the only way in is the `aws-auth` ConfigMap the dashboard deliberately never edits. Clusters
provisioned here are built `API_AND_CONFIG_MAP` (`authentication_mode` in
`terraform/k8s_cluster/aws_eks`); an older or hand-built cluster is converted in place, and
EKS allows the upgrade but never the reverse:

```
aws eks update-cluster-config --name <cluster> \
  --access-config authenticationMode=API_AND_CONFIG_MAP
```

Symptom when this is missed: registration succeeds, then the **first rotation** fails with a
`400` from `Credentials/Change` whose body is the plugin's attempt log ending in an API-server
`401`/`403` — nothing in the cluster looks wrong, because the binding is fine and it is the
*identity* that was never mapped.

Run **Verify Functional Account** in Password Safe after registering: it names every missing
verb, prints the ClusterRole to apply, and logs the correct AKS object id on every run.

### Keeping the PRA Vault copy in sync

Password Safe does it. Registration calls
`POST ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}` — `{id}` is the **parent** (the
Kubernetes Service Account Token account), `{syncedAccountID}` the **subscriber** (the PRA
Vault Token account). `ps-cli synced-accounts create -ma-id <parent> -sa-id <subscriber>` is
the same operation and is the quickest way to check it by hand. From then on every rotation of
the token account is applied to the subscriber too, which runs the PRA Vault Token plugin's
PATCH into PRA. Both accounts stay Password Safe-managed and audited, and no credential passes
through the dashboard at all.

`GET …/ps-token/status` re-reads the link from Password Safe on every open rather than caching
it, because an admin unlinking in the Password Safe console is exactly the condition an
operator opens that panel to diagnose. `DELETE …/ps-token` unlinks before off-boarding either
account.

> **Removing the PRA tunnel does not unlink the pair.** The plugin resolves its PRA Vault
> account by *name* (`k8s-<cluster>-sa`), so re-provisioning the tunnel re-creates the account
> the link already points at and syncing resumes with no operator action. The cost is that a
> rotation landing while no tunnel exists fails the PRA half — visibly, in Password Safe's
> change log.

**The LongLived break window.** Rotation revokes the old token immediately. Password Safe
applies the new value to the subscriber as part of the same change, but change operations are
queued, so there is a short window where PRA still holds the revoked token. Set the cluster's
token mode to **Bound** on tunnels that must not break: Bound never revokes, and the old token
stays valid until its TTL.

**Why this works when the parent mints a JWT rather than accepting a password.** Password
Safe's published shared-credential behaviour describes ordinary password accounts, where it
generates a password from the policy and pushes it outward. The sync actually copies whatever
is *stored* as the parent's password, and the parent's plugin decides what that is: the
"Kubernetes Service Account Token" plugin ignores the password policy — it is minting a JWT,
not a password — and reports the token it got from the cluster, so the token is the stored
credential and the token is what the subscriber receives.

### Operator prerequisites

1. Import both `.psplugin` packages and confirm the platform names.
2. Create the functional accounts above.
3. Grant the API identity **Requestor** plus an access policy granting **View** on a Smart
   Rule containing both managed accounts. There is no Smart Rule API, so this is out-of-band —
   and it is the failure every Password Safe consumption path here hits first (`POST /Requests`
   → `4031` / 403).
4. Grant the API identity **Password Safe Account Management (Full control)** — what the
   sync link needs, per the REST reference. (`ps-cli synced-accounts -h` claims *Role
   Management (Read/Write)* instead; that looks like an error in the CLI help, since the
   operation acts on managed accounts rather than roles. If the link 403s with Account
   Management already granted, try Role Management before assuming a different cause.)
5. **Leave "Change Password After Release" OFF** on *both* accounts. A credential change on
   either member of a synced pair re-rotates the pair, so with it on, every release of the PRA
   copy would rotate the real cluster token — an endless loop with a dead-credential window
   each time. There is no circuit breaker for this any more; the access policy is the fix.
6. Give the Password Safe host or Resource Broker network reachability to the cluster's API
   server. For private clusters this is a real constraint — Password Safe does not route
   through the PRA Gateway.

### Configuration keys — token rotation

| Key | Default | Notes |
|---|---|---|
| `k8s_ps_token_rotation_enabled` | `false` | Master gate (row action, provision checkbox) |
| `k8s_ps_token_platform` | `Kubernetes Service Account Token` | Plugin platform (name or id) |
| `k8s_ps_pravault_token_platform` | `PRA Vault Token` | Subscriber plugin platform |
| `k8s_ps_functional_account_aws` / `_azure` / `_gcp` / `_local` | — | Per cloud; `_local` also covers OKE and on-prem |
| `k8s_ps_pravault_functional_account` | — | PRA Config-API OAuth client for the PRA Vault account |
| `k8s_ps_workgroup` | — | Blank → `passwordsafe_workgroup` |
| `k8s_ps_token_mode` | `longlived` | `longlived` (revokes) or `bound` (TTL expiry, no revoke) |
| `k8s_ps_token_ttl_seconds` | `3600` | Bound mode; clamped up to the API server's 600s floor |
| `k8s_ps_token_change_on_register` | `true` | Rotate once on register — proves the whole path immediately |
| `k8s_ps_token_delete_legacy_secret` | `true` | Retire the dashboard-minted Secret the plugin's sweep never touches |
| `k8s_ps_token_register_on_provision` | `false` | Pre-tick the provision-form checkbox |
| `k8s_ps_pravault_mirror_enabled` | `true` | Register and sync the PRA Vault Token account when a PRA vault account exists |
| `k8s_ps_token_checkout_duration_min` | `15` | Password Safe request duration for token reads |
| `k8s_ps_token_address_options` | — | Extra `;key=value` appended to every address |
| `k8s_ps_rotator_apply_rbac` | `true` | Apply the rotator ClusterRole + binding on register |
| `k8s_ps_rotator_gke_sa_email` | — | Blank → derived from the GCP functional account's name |
| `k8s_ps_rotator_aks_sp_object_id` | — | The `oid` claim; the plugin logs it on every run |
| `k8s_ps_rotator_eks_username` | `passwordsafe-rotator` | Access-entry username = the RBAC `User` subject |
| `k8s_ps_rotator_eks_principal_arn` | — | IAM principal behind the functional account's key |
| `k8s_ps_rotator_eks_create_access_entry` | `true` | Create the access entry when the ARN is set |
| `k8s_ps_rotator_bootstrap_namespace` / `_sa` | `beyondtrust` / `password-safe-rotator` | Generic-path bootstrap ServiceAccount |

The managed account name is `<namespace>/<serviceaccount>`, taken from `pra_k8s_namespace` /
`pra_k8s_sa_name` on the
[Privileged Remote Access](privileged-remote-access.md#kubernetes-tunnel-identity) panel —
PRA owns that identity, Password Safe rotates its token.

Off-boarding removes both managed systems and the rotator RBAC; it runs automatically when the
cluster is decommissioned or deregistered. Design rationale, including why each of these
choices is what it is: [k8s-sa-token-rotation](../design/k8s-sa-token-rotation.md).

---

## Databases

The dashboard also provisions **managed cloud databases** (AWS / Azure / GCP / OCI), reaches
them through a PRA protocol tunnel, and can optionally **onboard AWS and Azure databases into
Password Safe** for credential rotation (via the `{engine} SSM Custom Plugin` /
`{engine} Azure Run Command Plugin` and the shared `PRA Vault Username Password` plugin). That
whole feature — base provisioning, per-cloud prerequisites, and the Password Safe onboarding —
is documented separately in **[Databases](../databases.md)**. The tunnel half needs
[Privileged Remote Access](privileged-remote-access.md).

The dashboard can also **register** a database it did not create — on-premises or in a cloud —
so it can be a Configuration Management target. That path has no tunnel and no onboarding: its
admin login is a Password Safe **managed account**, checked out just-in-time per run and never
stored, so the database has to be onboarded in Password Safe *before* it can be registered.

### Importing databases from Password Safe

Since Password Safe's own discovery scanner already found and onboarded these databases —
with managed credentials, so it knows the platform, port, instance and accounts — the
Databases page can read that inventory directly instead of asking an operator to retype it.
**Databases** → **Import from Password Safe** lists the candidates and registers the ones you
tick. It **reads only**; nothing in Password Safe is created or changed.

Two things worth knowing here rather than in the feature doc:

- **This path uses the public REST API, not `ps-cli`.** It reads `Platforms`,
  `ManagedSystems`, `Databases` and `ManagedAccounts` over HTTPS with the same
  `pscli_api_url` / `pscli_client_id` / `pscli_client_secret` OAuth client configured in
  [Step 1](#step-1--password-safe-oauth-application-ps-cli), so it works in an image with no
  `ps-cli` binary. The run-time credential *checkout* still goes through `ps-cli`.
- **The account list comes from the accounts the API identity can `request`.** That is the
  same permission surface the checkout uses, so a missing **Requestor** role or Smart Rule
  shows up as a greyed-out candidate instead of a `4031, statuscode: 403` in a worker log
  hours later.

Configuration keys (Settings → Integrations → Password Safe → *Database Import*):
`clouddb_ps_import_workgroup`, `clouddb_ps_import_default_cloud`,
`clouddb_ps_import_max_systems`, `clouddb_ps_import_platform_map`. All optional and all
documented in **[Databases → Importing from Password Safe](../databases.md#importing-from-password-safe)**.

---

## Advanced configuration

`bt_ps_deploy_key_title` — the title of the Password Safe secret holding the Gateway Docker
deploy key — is documented with the Gateway keys it belongs to, on
[Privileged Remote Access → Advanced configuration](privileged-remote-access.md#advanced-configuration).
Password Safe is only the storage mechanism there; starting a Gateway is a PRA concern.

---

## Troubleshooting

**"ps-cli not found"** — `ps-cli` comes from the `beyondtrust-bips-cli` pip package in
`web_dashboard/requirements.txt`, so this means the image was built without installing
requirements, or something is shadowing `PATH`. Confirm with
`docker compose exec app ps-cli --version`; rebuild the image if it is genuinely absent.

**"Authentication failed" from ps-cli** — verify the Client ID and Client Secret
in **Settings → Integrations → Password Safe** match the API Registration in
Password Safe and that the registration has not expired.

**Secrets retrieved are empty** — check that the API Registration has **Secrets →
Read** and **Credentials → Read** permissions, and that the specific secret is
in scope for the registration.

**A checkout returns `4031` / 403** — usually the API identity is missing the **Requestor**
role or an access policy granting View on a Smart Rule containing the account. There is no
Smart Rule API, so this is out-of-band; see [Operator prerequisites](#operator-prerequisites).

Password Safe returns the same 4031 when the account is not **API-enabled**, and when the
`SystemID` on the request does not own the `AccountID` — `POST Requests` authorises the
*pair*. So a 4031 that survives a correctly granted Requestor role is not the grant. The
dashboard sends the account's real managed system (read from the account when the caller
does not already hold it) and quotes Password Safe's response body in the job error, which
is where the numeric code that separates these lives: `4034` is a request awaiting approval
and `4035` the account's concurrent-request cap — both also 403.
