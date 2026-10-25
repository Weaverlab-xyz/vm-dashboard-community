# Bulk runs from the inventory

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want one playbook run against a selection of hosts from the inventory.

Part of [Config Management](../config-management.md).

The Config Management page runs one asset against one target. To apply a playbook
across a fleet, use the **Inventory** page (`/inventory`): filter to what you want,
tick the rows, and a run panel appears. Each selected resource becomes its **own
job**, all tagged with a shared `batch_id` — so one host failing doesn't roll back
the others, and each job keeps its own log and output scrubbing.

Runs are **queued work**, not request-side work: the endpoint writes a job row and the
job runner claims it. So a batch survives a dashboard restart mid-flight, and its jobs
spread across `WORKER_REPLICAS` (default 3) instead of executing one at a time. Worth
knowing before firing a large batch — three playbooks run against three hosts at once,
and raising `WORKER_REPLICAS` raises that concurrency.

Queueing a batch lands you on **`/jobs?batch_id=…`**, filtered to just that run, with
a rollup across the whole batch — *N total · N running · N failed* — rather than the
one page of rows the table happens to show. The URL is shareable, and any job that
belongs to a batch carries a **batch** badge linking back to its siblings, so you can
still find a run after the toast is gone.

**One kind per run.** Selecting a VM locks the checkboxes on Kubernetes clusters and
databases, and vice versa. This isn't a UI convenience — the kinds are not
interchangeable at any level. A VM run SSHes to a host; k8s and database runs are
`localhost` plays that reach *out* over a kubeconfig or DB login. Different request
fields, a different runner, and a playbook written for one is meaningless against
another. A mixed selection could only ever produce a pile of failed jobs, so it is
refused rather than attempted.

Rows that can't be a target at all are disabled, with the reason on hover:

| Row | Why it's disabled |
|---|---|
| Virtual desktops | No Ansible target exists behind a seat. |
| Proxmox / Nutanix VMs | Their deploy records a node + VMID, not an address. Target them through their hypervisor **group** on the Config Management page instead. |
| Databases with an unsupported engine | The runner image ships client libraries for postgres / mysql / sqlserver only. |
| Clusters or databases in a cloud with no runner | See [Runners](runners.md). |

Those reasons come from the server, computed by the same rule the endpoint enforces,
so the page can never offer a checkbox the API would reject.

Two limits worth knowing. A batch is capped at **50 targets** — each one is a job, so
a mis-clicked "select all" against a large estate would otherwise fan out unbounded
work. And while *selection* problems refuse the whole request before any job exists,
a **per-target** failure at dispatch does not: several checks depend on the target's
cloud, so a mixed-cloud VM batch can be valid for one host and not another. Those
targets come back in the response's `failed` list and are named in the toast; the
rest still run.

## Secrets and managed accounts in a bulk run

The inventory panel covers the common case — asset, SSH user, extra vars. For a run
that needs a Secrets-Management secret or a Password Safe managed account, use
**Continue on the Config Management page →**. It carries the selection over and the
full run form applies to it: named secret vars, become password, SSH key, and the
managed-account picker. The page opens on the **Run** tab with the **Use a secret**
section already expanded.

**Every target picks its own managed account.** The form lists each selected VM with
its own Password Safe dropdown, read from that target's own address. Pick a **default**
above the table to pre-select every row, then override only the rows that differ — a
fleet whose hosts carry `svc-ansible-web01`, `svc-ansible-db02` and so on is expressed
directly, rather than needing one account name to be true everywhere.

The suggestion for each row is made server-side, in three tiers: the account matching
the default name you chose; the Password Safe system that VM was actually onboarded
into (recorded at registration — the only thing that finds a plugin-onboarded system,
which carries a `127.0.0.1` placeholder and no usable address); or a single unambiguous
candidate. Rows say which tier they came from, so an inference is distinguishable from
your own pick. There is deliberately **no fuzzy name matching**: a near-miss here checks
out the wrong machine's credential.

*Why the ids matter.* A `ManagedAccountRef` pins `system_id` + `account_id`, and both
belong to one managed system — one such ref shared by a whole batch would check out a
*single machine's* credential and connect to every host with it. Per-target refs are
pinned because each one came from its own host's live list, and the server checks every
key against the selection it resolved for itself. The **default** is sent as a name
only, for the same reason: it applies to hosts whose own list was never read. A
**scheduled** batch sends names throughout — an id pinned today can name an account
that has been removed by the time the window opens.

**A target with no match still runs.** It is flagged amber in the table and falls back
to the default account name; if that host does not have it, it fails **only its own
job**, with a message naming the host and the account. The rest of the batch runs.

Name matching works for domain accounts too — the Password Safe lookup already falls
back to domain-linked accounts — and it matches the `{user};{suffix}` form that
cloud-native onboarding registers (the AWS Systems Manager plugin appends a scope
suffix), so picking `svc-ansible` matches `svc-ansible;local`.

**Connection credentials are refused for Kubernetes and database batches.** Those run
a `localhost` play that reaches out over a kubeconfig or DB login — there is no SSH
connection to authenticate, and the run path silently ignores `managed_account`,
`managed_become`, `secret_ssh_key_source` and `secret_become_source`. A single run can
absorb that quietly; a batch would leave you believing a credential had been applied
to fifty clusters, so `/run-bulk` rejects the combination with a 400. Named
`secret_vars` are honored on those targets and stay available.

**Databases have a per-object account of their own.** A *registered* database already
connects as the Password Safe account it was registered with, checked out just-in-time.
A *provisioned* one connects as the stored admin by default, but if it has been
onboarded into Password Safe you can switch it to its **own** managed account with
**Settings → Integrations → Password Safe → Databases tab → _Ansible runs connect as the database's own
managed account_**. That account is unique per database by construction, so a bulk run
over several databases uses a different credential for each with nothing to pick.

> The setting is **off by default and should stay off until you have granted the
> account.** The managed user the dashboard creates (`psafe_<id>`) is built with a bare
> login and **no privileges at all** — it exists to be rotated, not to read anything
> (on Azure SQL it only gets a contained user in `master`). Turn this on without
> granting it what your playbooks need and every run fails on permissions.

Kubernetes clusters have no equivalent: a cluster records no single managed
system/account pair, so there is nothing per-cluster to select. Use named `secret_vars`
there.
