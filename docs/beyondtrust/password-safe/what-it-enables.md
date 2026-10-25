# What Password Safe enables in the dashboard

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want to know everything turning Password Safe on changes: checkout, onboarding, rotation and the pages that use it.

Part of [BeyondTrust Password Safe](../password-safe.md).

| Feature | Description |
|---|---|
| **Vault-backed cloud credentials** | AWS, Azure, and SSH credentials resolved from Password Safe at runtime rather than stored in the application database |
| **SSH key checkout** | Ansible and BT Gateway tasks retrieve SSH private keys from Managed Accounts on demand |
| **Managed-account checkout for playbook runs** | A Config-Management run can use a Password Safe managed account as its login identity — the operator picks an account from a live list and the credential is checked out **just-in-time** at run time, never shown and scrubbed from job output. See [below](#managed-account-checkout-for-config-management-runs) |
| **Resource onboarding** | VMs and cloud databases the dashboard builds are onboarded as Password Safe managed systems + accounts, and removed again on destroy |
| **Hypervisor credentials for a remote agent** | An on-prem agent brokering vCenter/Proxmox/Hyper-V can hold no credential at all: the dashboard checks one out per job, seals it to that agent, checks it back in and rotates it on release. See [below](#hypervisor-credentials-for-a-remote-agent) |
| **Secret audit log** | Every checkout creates an immutable record in Password Safe |
| **Attributes on Inventory** | Each matched resource's Password Safe attributes (`Status = Online`, `Business Unit = Finance`) as a filterable column on `/inventory`. Admins can assign and remove them, for one resource or up to 50 at once, and re-run the Smart Rule that uses them. See [Inventory — Password Safe attributes](../../operations/inventory.md#password-safe-attributes) |

PRA Vault accounts minted for tunnels can themselves be onboarded here for rotation —
see [Privileged Remote Access](../privileged-remote-access.md).

## Managed-account checkout for Config-Management runs

Instead of referencing a *stored* secret, an operator running a playbook can pick a
Password Safe **managed account** and have its credential checked out just-in-time. Two
details are worth knowing because they are not obvious:

- **Across many hosts, the account is matched by name.** A managed account reference
  pins a system id *and* an account id, both specific to one managed system — reusing
  one across a fleet would check out a single machine's credential and connect to every
  host with it. A [bulk run](../../operations/config-management/bulk-runs.md)
  therefore sends the account **name**, and each job resolves it against the host it is
  configuring, so every host checks out its own credential.
- **On the ECS / Cloud Run runners it needs an opt-in.** Those runners *reference* a
  store secret rather than taking a value inline, which a just-in-time credential has
  no place in — so they are rejected unless **Ephemeral cloud secrets** is enabled, at
  which point the credential is briefly written to that cloud's store as a short-lived,
  RBAC-locked secret and force-deleted after the run.

Full walkthrough in
[Ansible → Managed-account checkout](../../platform/ansible/secrets.md#managed-account-checkout-beyondtrust-password-safe).

## Hypervisor credentials for a remote agent

A [remote agent](../../platform/spiffe/remote-agents.md) brokering hypervisor operations inside a private network
has two ways to use Password Safe, and they differ in which host holds the OAuth client:

| | Agent-side checkout | Dashboard-side checkout |
|---|---|---|
| Set in | `connections.yaml` → `ps_managed_account: <id>` | Connections tab → `ps_account://<id>`, plus `dashboard_secret: true` in `connections.yaml` |
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

## Power-state attributes

You can have a VM's Password Safe records follow its power state. When a start or stop
completes, the dashboard applies one attribute and removes the other on the VM's **managed
system and its linked asset**. A Smart Rule keyed on that attribute can then act on it, for
example to keep a stopped host out of a rotation that would only fail.

**Set it up**

1. In Password Safe, create an attribute type such as *Power State* with two values,
   *Online* and *Offline*. The dashboard never creates attribute types or values.
2. In **Settings → Integrations → Password Safe → VMs**, turn on **Set a power-state attribute
   when a VM is started or stopped**.
3. Pick the value to apply **on start** and the one to apply **on stop**. If you leave one
   blank, only that direction is stamped.

**Which operations it covers**

| Operation | Attribute applied |
|---|---|
| Start, power on, resume, unpause | Online |
| Stop, shut down, pause, save, suspend | Offline |
| Reboot, reset, restart | Nothing changes |

This covers every path that runs one of those operations: single and bulk buttons on every
cloud and hypervisor page, agent-brokered hypervisors, the Workstation page, suspend
schedules, the spend cap, change-window schedules, and a POV environment's suspend and
resume. A POV stamps each of its guests.

The stamp runs only after the power job has **completed**. A power operation that failed
changes nothing in Password Safe. The result is written as one line in the power job's
Live Output, naming each record written or the reason nothing was. A Password Safe error
never turns a successful power operation red.

**How a VM is matched to its records**

- A VM the dashboard onboarded is found by the managed-system id it recorded at onboarding.
- Any other VM, such as a hypervisor guest or a discovered VM, is matched by IP address:
  the last-known address from the synced inventory.
- An address that matches **more than one managed system is skipped**. Private addresses
  repeat across labs, and stamping another host offline because this one stopped is worse
  than not stamping at all.
- A cloud VM the dashboard did not deploy has no address on record where the power job runs,
  so it is reported as not applied.

**POV environments** have their own Password Safe tenant, where the attribute ids differ.
There the configured values are looked up **by name**: the same type name and value text.
If that POV's tenant has no such value, its guests are skipped and the job log says what is
missing.

Every write is audited as `attributes.assign` or `attributes.remove`, the same actions as
an edit from [Inventory](../../operations/inventory.md), with `trigger: power` in the details.
