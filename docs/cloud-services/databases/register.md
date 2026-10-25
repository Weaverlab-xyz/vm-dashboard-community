# Registering an existing database

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you already run a database, in a cloud or on-premises, and want the dashboard to manage access to it.

Part of [Databases](../databases.md).

Registration records a database the dashboard **didn't create**, so it can be a
[Configuration Management](../../operations/config-management.md) target. It is the database sibling of
registering a Kubernetes cluster from a kubeconfig ([Kubernetes](../kubernetes.md)): the
dashboard builds nothing, changes nothing, and stores no credential. `cloud = local` is
the on-premises case.

The registerable set is deliberately wider than the provisionable one — **provisioning
needs a Terraform module, registering needs only somewhere to reach.** Any of `local`,
`aws`, `azure`, `gcp` or `oci` may be registered, and engines are **postgres / mysql /
sqlserver / oracle / mongodb**, and Configuration Management runs cover all five — the
`ansible-cloud` runner image carries `python-oracledb` (thin mode) and `community.mongodb`
over `pymongo`; see the [database samples](../../../examples/playbooks/README.md#databases-database).
No Ansible runner resolves for `oci` at all, so an OCI row registers and lists but can't be
a run target.

**How to register.** **Databases** page → **Register existing**, or
`POST /api/databases/register` (permission `cloud_database:write`). To register several
at once from what Password Safe already knows, use
[Import from Password Safe](import.md) instead. The button only
renders when the Password Safe integration is on (`password_safe_enabled`), because the admin
login *has* to be a Password Safe managed account. The `cloud_database_enabled` feature
toggle gates registration and provisioning alike.

| Field | Notes |
|---|---|
| **Engine** | postgres / mysql / sqlserver / oracle / mongodb |
| **Location** | `local` (on-premises) / aws / azure / gcp / oci |
| **Host** | Required — it is how the runner reaches the database. One row per engine + host; a duplicate is refused. |
| **Port** | Optional; defaults per engine (5432 / 3306 / 1433 / 1521 / 27017). |
| **Database name** | Optional. SQL Server falls back to `master`. |
| **Admin account** | A Password Safe **managed system + account**, looked up live by host — the same lookup the Configuration Management run form uses. Onboard the database in Password Safe *first*; the list is empty until you do. |

**The credential is never stored.** The row keeps the managed system id, account id and
account name — nothing else. At run time the dashboard checks the credential out and lets
the request **expire** on its duration rather than checking it back in, matching what the
inline runners already do for VMs. Contrast the provisioned path, which reads its admin
credential from the provisioning job's Terraform variables and the encrypted config store.

> **The account must be *requestable* by the dashboard's Password Safe API identity** —
> onboarding it is necessary but not sufficient. The checkout goes through a Password Safe
> *request*, so that identity's group needs the **Requestor** role, with an access policy
> granting **View** (API-only is fine), on a Smart Rule containing the account. Without it
> the row registers and lists happily and the run fails in the worker with `Password Safe
> checkout failed for database …: 4031, statuscode: 403`. Two things make this easy to
> misdiagnose: a *database* Smart Rule needs its own grant — the ones covering
> Linux/SSM/Waagent accounts do not extend to it, so the VM path working tells you nothing
> — and Smart Rule membership is recomputed on a schedule, so a freshly created account is
> not requestable the instant it exists. Check with
> `ps-cli access-policies test -s-id <system> -a-id <account>`; an empty list means no
> policy applies yet.
>
> **The import catches this early.** Because
> [Import from Password Safe](import.md) sources its account list from
> the *requestable* accounts, a managed system showing **no requestable account** there is
> this same problem, surfaced before the row is ever created rather than hours later in a
> worker log. If a database you know is onboarded shows that reason, it is the Requestor
> role or the Smart Rule — not the onboarding.

**Where the run executes** follows the database's location, exactly as it does for
clusters — an on-premises database runs in a sibling container on the dashboard host,
because nothing in a cloud has a route to your LAN; a cloud-hosted one runs on that
cloud's transient in-subnet runner. See
[Ansible → Kubernetes-cluster and database targets](../../platform/ansible/kubernetes-runner.md#kubernetes-cluster-and-database-targets-localhost-runs).

**Every runner takes the just-in-time credential, AWS and GCP included.** A database run is
a localhost play, and its connection variables ride the runner's *inline* environment
channel as `CONN_VARS_B64` — the ECS task override env, ACI `secure_value`, the Cloud Run
env, or a `0600 --env-file` locally — decoded to a `0600` file and passed as `-e @…`.
Nothing needs to pre-exist in a cloud secret store, so the ephemeral-store opt-in that
governs a **VM SSH** managed-account run (`ansible_cloud_ephemeral_secrets_enabled`, see
[Secrets Management](../../admin/secrets-management.md#ephemeral-cloud-secrets)) does **not** apply
here. That opt-in exists only because the SSH path injects named/become secrets by
*reference* (`valueFrom`) on ECS and Cloud Run; the database path never does.

**What registration does not give you.** A registered row has no Terraform state and no
provisioning job, so none of the three layers apply: no PRA protocol tunnel, no Layer-2
Password Safe onboarding of a rotated DB user, and no Layer-3 Entitle integration (Entitle
onboarding reads the provisioning job's admin credential, which such a row doesn't have).
Nothing is created in the cloud, and no gateway host is brokered on its behalf.

**Delete deregisters.** Removing a registered row drops it from the inventory and leaves
the database running — the dashboard holds no Terraform state for someone else's database,
and destroying it would be the wrong verb. The row's action button says **Deregister**
rather than Decommission, and the confirm dialog says the database itself is untouched.
The reverse is refused too: a provisioned database is decommissioned, never deregistered.
