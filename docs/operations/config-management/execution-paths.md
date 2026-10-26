# How a Config Management run executes

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want to know which path a run takes (local runner, cloud task or remote agent) and what each one isolates.

Part of [Config Management](../config-management.md).

The dashboard surfaces three distinct execution paths, all reaching the
same `Config Management` page. The first two SSH/WinRM *to* a host; the
third runs a `localhost` play that reaches *out* to a managed service.

## On-premises hypervisors

If you've enabled any of the on-prem hypervisor integrations (Proxmox VE,
vSphere/ESXi, Hyper-V, Nutanix AHV, XCP-ng, VMware Workstation), the
dashboard auto-builds an Ansible inventory from each integration's
configured host list. Targets appear in the run-asset dropdown as group
keys (e.g. `proxmox`, `vsphere`).

Behind the scenes (see [`services/ansible_local_service.py`](../../../web_dashboard/services/ansible_local_service.py)):

- `build_inventory()` returns a JSON inventory grouped by hypervisor type,
  with hostvars wired for the right Ansible connection plugin
  (`ansible_connection=ssh` or `winrm` depending on the hypervisor).
- `get_configured_targets()` powers the UI dropdown — only enabled +
  configured hypervisors appear, no empty groups.
- The Local Docker runner (default) executes playbooks against this
  inventory directly from the dashboard host.

Two limits of that path are worth knowing before you rely on it, and a
[remote agent](../../platform/spiffe/remote-agents/config-runs.md#agent-executed-config-management) is the answer
to both:

- **It targets the hypervisor *hosts*, not their guests.** `build_inventory()`
  emits one Ansible host per *connection* — the vCenter, the Proxmox node — so
  configuring the VMs running on them is a different thing entirely. Guests
  synced by an agent are individually selectable once the sync reports an
  address.
- **Agent-bound connections are excluded from it deliberately.** They hold no
  host and no credential on this side, so an inventory entry for one could never
  connect. Their guests are reached by the agent instead.

This is where contributors with on-prem labs help most — see
[CONTRIBUTING.md → Where the community can help most](../../../CONTRIBUTING.md#where-the-community-can-help-most).

## Cloud providers (AWS / Azure / GCP)

Cloud VMs you've deployed via the dashboard appear in the same target
dropdown, prefixed with `aws:`, `azure:`, or `gcp:`. Picking one tells
the runner three things:

- **Where the target is** — the bare IP or hostname (the prefix is
  stripped before the runner sees it).
- **Whose SSH key opens the door** — fetched from the matching cloud's
  secret store at run time. AWS Secrets Manager, Azure Key Vault, or
  GCP Secret Manager. The fetch is JSON-aware: it handles either a raw
  PEM string or a `{public_key, private_key}` envelope.
- **Which user to log in as** — the per-cloud SSH user (`ansible_aws_user`,
  `ansible_azure_user`, `ansible_gcp_user`) drives the default; the run
  form lets the operator override per job. Stock-image conventions
  (`ec2-user` / `azureuser` / `gcp-user`) are pre-populated.

Cloud runs can use any of the four runners — the choice mostly affects
*where the Ansible process executes*, not the playbook semantics. See
the runner section below.

## Kubernetes clusters & databases (localhost plays)

Registered or provisioned **Kubernetes clusters** and **databases**
appear in the same target dropdown, under their own groups. A database
[imported from Password Safe](../../cloud-services/databases/import.md)
is an ordinary registered row and behaves identically as a target.
These are *not* SSH targets — Ansible's `kubernetes.core` and
`community.postgresql`/`mysql`/`general` modules run on the controller
(`hosts: localhost, connection: local`) and reach *out* to the API
server (via a kubeconfig) or the DB endpoint (via login vars). So the
model is inverted from the VM paths, and three things follow:

- **Connection material is injected automatically.** Pick a cluster and
  the dashboard token-preps its stored kubeconfig (swapping the cloud
  exec-auth block for a short-lived bearer token) and hands it to the
  runner via `K8S_AUTH_KUBECONFIG`/`KUBECONFIG`. Pick a database and it
  resolves the admin credential server-side — the provisioning job's for a
  database it built, a just-in-time Password Safe managed-account checkout
  for a registered one — and injects
  `db_login_host`/`_port`/`_user`/`_password` (+ `db_name`) as **scrubbed**
  extra-vars. The operator never sees or types either. No SSH user or key
  field is shown.
- **Cloud-hosted targets always run on a remote in-cloud runner.** Those
  endpoints are private-only, so the run executes in-cloud, in-subnet
  (ECS / ACI / Cloud Run — the same infra the VM cloud runners use), never
  the local sibling-Docker path. This is mandatory: the dashboard host
  can't reach an RFC1918 endpoint, and its egress traverses the corporate
  TLS-inspecting proxy. Running in-cloud keeps the Ansible→endpoint data
  path entirely within the cloud. (`ansible_runner_<cloud>` selects the
  backend; `local` is rejected for these kinds.) **An on-premises target
  (`cloud = local`) inverts it** — a cluster registered from a kubeconfig,
  or a registered on-prem database, sits on your LAN, which no cloud task
  can reach, so those runs execute in a sibling container on the dashboard
  host instead. **A cloud database a Config route covers** runs on that
  route's remote agent instead of the cloud runner; see
  [Cloud databases](../../platform/spiffe/remote-agents/config-runs.md#cloud-databases).
- **A different runner image.** k8s/DB runs use `chrweav/ansible-cloud`
  (kubernetes.core + the DB collections + client libs + the helm CLI),
  selected via `ansible_cloud_image` — never the winrm VM image, which
  lacks those collections.

Runs are dispatched by the durable job worker (`ansible_cloud_run` job
type) rather than an in-process background task, since they launch a
cloud task that can outlive a request worker's recycle.

**Scope note:** the stored kubeconfig is cluster-admin and the database
credential is the admin/master login — a localhost play has full rights.
Treat these playbooks accordingly. Starters live in
[`examples/playbooks/k8s/`](../../../examples/playbooks/k8s/) and
[`examples/playbooks/database/`](../../../examples/playbooks/database/).
