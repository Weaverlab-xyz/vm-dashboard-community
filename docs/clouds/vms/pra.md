# PRA Shell Jumps for cloud VMs

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want a deployed VM reachable through PRA, added after deploy, or need to know which Asset Group and Gateway its jump lands in.

Part of [Cloud VMs](../vms.md).

When `pra_enabled` and PRA is configured (`bt_api_host`, `bt_client_id`,
`bt_client_secret`, `bt_jump_group_name`, `bt_jumpoint_name`), a Linux deploy brokers a
PRA **Shell Jump** via `terraform_pra_service.provision_jump(tag=<cloud>)` (the `beyondtrust/sra`
provider), routed through the cloud's gateway host. The jump is removed on destroy from its
stored state.

## PRA is an opt-in on the deploy form

Each cloud's deploy form has one **Add to BeyondTrust PRA** checkbox, ticked by default (or
as the VM's [workstream](#workstream-deploy-defaults) sets it) and shown only while PRA is
enabled. Its Asset Group (called a Jump Group before PRA 26.1), Gateway and PRA credential
overrides sit in a collapsed section under it; leave them alone and the configured defaults
apply. The credential is picked from any configured secrets backend and replaces
`bt_client_secret` for that VM's shell jump only. The AWS batch form shows the same overrides, shared by every
VM in the batch. Untick it
and the VM deploys with no jump (`enable_pra: false` on the request; a request that omits
the field keeps the jump unless its workstream's default says otherwise, so API callers and
older clients are unchanged). On a Windows
server, unticking also skips the Windows jump wiring; it can be added later from the row, as
long as the deploy kept the administrator password or Password Safe manages the account —
see below.

PRA is on when **Settings** turns it on (`pra_enabled`), on every cloud. Azure used to read
only the environment variable, so switching PRA off in Settings left Azure deploys still
building jumps.

The gateway host is ensured either way. That keeps the VM wired to the same address it would
have had with PRA on, so a jump added later points where a deploy-time one would.

## Workstream deploy defaults

A workstream can carry its own defaults for the cloud VMs deployed into it. Set them on
**RBAC → Workstreams → Edit → Deploy defaults**; every field can say *Global default*,
which is what an unset one means.

| Default | What it does |
|---|---|
| Add new VMs to PRA | The default for the deploy form's **Add to BeyondTrust PRA** checkbox, and for an API request that leaves `enable_pra` out. |
| PRA asset group, PRA Gateway | Where this workstream's jumps land, unless the deploy (or the row action) names one. |
| Register in Entitle, Onboard into Password Safe | Pre-ticks those boxes on the deploy form. |
| RDP jump for Windows, Microsoft Entra ID | Pre-fill the Windows choices on the deploy form. |

Picking a workstream in a deploy form fills these in, and you can still change any of them
for that one deploy. The server applies the PRA default and the jump target itself, so an
API caller gets them too.

Resolution, most specific first:
- **PRA on or off:** the request's `enable_pra` → the workstream default → on.
- **Asset group and Gateway:** the request → the workstream default → the per-cloud setting
  → the global `bt_*` setting.

Anyone who may edit the workstream (an administrator, a member, or its creator) may set
these, and every change is audited. The list is fixed and holds no secrets on purpose: an
SSH key secret or a PRA credential for a workstream is not something a member should be able
to point at. The defaults are stored on the workstream itself and go when it is deleted.
The API is `GET`/`PUT /api/workgroups/{name}/profile`; on a `PUT`, a field left out is
unchanged and `null` sets it back to the global default.

## Workstream network defaults

A workstream can remember the network its VMs usually go into, per cloud and region:
the subnet and security groups on AWS, the subnet and network security groups on Azure, the
subnetwork on GCP, and the subnet on OCI. Pick them on a deploy form and click **Save as
this workstream's network for this region** under the subnet. The next deploy into that
workstream and region starts with them filled in.

- **They fill the form, nothing more.** Only empty fields are filled, plus fields still
  holding a default the form put there itself (AWS's configured default subnet). A subnet
  you picked is never replaced. The server applies none of it, and the deploy is validated
  as usual.
- **Regions are separate.** Changing region clears the network fields, as before, then
  fills them from the workstream's defaults for the new region. GCP keys them by region,
  taken from the zone; OCI has one region.
- **The workstream editor** lists them under **Network defaults**, with **Remove** for each.

Like the deploy defaults, any member of the workstream, its creator, or an administrator can
change them, and every change is audited. The API is `GET`/`PUT
/api/workgroups/{name}/network-defaults`; the `PUT` takes `{"cloud": "aws", "region":
"us-east-1", "values": {"subnet_id": "...", "security_group_ids": ["..."]}}`, changes only
that region, and `null` values clear it.

## Workstream secret bindings

An administrator can bind secret **references** to a workstream: the SSH key pair secret its
VMs use on each cloud, and the PRA credential its jumps are provisioned with. Only names and
references are stored (a Secrets Manager name, an `aws_sm://…` reference); the values stay in
your secret manager. Set them on **RBAC → Workstreams → Edit → Bound secrets**. Members see
what is bound, because their deploys use it, but only an administrator can change it.

| Bound secret | Where it applies |
|---|---|
| SSH key secret (AWS, Azure, GCP, OCI — one each) | Every deploy into the workstream on that cloud: the key injected into the VM, and the key Entitle and Password Safe registration use. |
| PRA credential | The Shell Jump of every deploy into the workstream, on all four clouds, and **Add to PRA** on its VMs' rows. |

How a deploy uses them:

- **Left at the default**, the deploy gets the bound reference. The job records the
  reference it actually used.
- **A different one named by a non-admin** is refused: *this workstream's SSH key secret is
  set by an administrator*. The deploy form disables that field when a key is bound and
  shows which one is used.
- **An administrator** may still name a different one for a single deploy.
- **A workstream with nothing bound** behaves as before: the global default, or any
  override the deployer picks.
- **The SSH key box** at the top of the AWS, Azure and GCP deploy forms shows the key the
  deploy will use — the bound one, marked *bound to this workstream*, once you pick the
  workstream.
- **Config-Management runs** on the VM, post-deploy runs included, connect with the same
  key, on all four clouds.
- **An OCI override** is checked when the deploy is submitted, as on the other clouds: a
  secret that can't be read or holds no public key is refused before anything launches.

On AWS, Azure and GCP the editor offers each cloud's own secret list, the same one the
deploy form's override offers. OCI has no listing, so its secret is typed (an OCID, or a
name in the configured vault).

Binding is administrator-only on purpose. A member who could bind a secret could point
their team's deploys at any secret the dashboard's cloud identity can read. Every change is
audited. The API is `GET`/`PUT /api/workgroups/{name}/secret-bindings`; the `PUT` takes
`{"ssh_key_secret": {"aws": "..."}, "pra_credential_ref": "..."}`, changes only the clouds
it names, and `null` unbinds.

The PRA credential can come from the workstream's
[own vault](../../admin/secrets-management.md#vaults-and-a-workstreams-own-vault), which only
that workstream's deploys can read.

## Workstream post-deploy runs

An administrator can give a workstream an ordered list of Config-Management assets
(playbooks, scripts, `.rpm`/`.deb` packages) to run on **every cloud VM deployed into it**:
an agent install, a hardening baseline. Set them on **RBAC → Workstreams → Edit →
Post-deploy runs**, choosing from the assets on the Storage page.

When a deploy into the workstream completes, each asset is queued as an ordinary
Config-Management job:

- **Ordinary jobs.** It is the same `ansible_local` job a run from the Config Management
  page creates, aimed at the new VM's address, so it appears on /jobs and is cancellable
  like any other run.
- **Scheduled, not immediate.** The jobs are queued in list order, scheduled a few minutes
  out so the VM's SSH (or a Windows server's OpenSSH) is up first.
- **The VM's own key.** They use the SSH key the VM was deployed with, including a
  [workstream-bound key](#workstream-secret-bindings).
- **No secrets.** A post-deploy run carries no secret variables or managed accounts.
- **Never fails the deploy.** The deploy is already done. If a run can't be queued, the
  reason is recorded on the deploy job as `post_deploy_error`.

The deploy form lists what will run, under **Runs after deploy**, with **Skip them for
this deploy** (`skip_post_deploy: true` on the request).

Limits:

- An asset kept on the dashboard's **local** storage is skipped, because the runner that
  reaches cloud VMs can't read it. Move it to a cloud backend from the Storage page.
- An asset [scoped to another workstream](../../operations/storage-management.md#scoping-an-asset-to-a-workstream)
  can't be added to the list; the editor only offers global assets and this workstream's own.

Changing the list is **administrator-only**, because each entry is code that runs on every
VM the workstream's members deploy. Members can see the list. Every change is audited, and
so is each deploy that queues runs. The API is `GET`/`PUT
/api/workgroups/{name}/post-deploy-runs` with `{"runs": [{"asset": "...",
"asset_backend": ""}]}`, at most 10, in run order.

## Adding PRA to a VM after deploy

A VM this dashboard deployed — on AWS, Azure, GCP or OCI, Linux or Windows — can be added
to PRA at any time from its row: **Add to PRA** opens the same Asset Group / Gateway / credential choices.
Once it has a jump the button reads **PRA ✓**, and the dialog offers **Re-apply** (replace
the jump with one built from new choices) and **Remove from PRA**. Each runs as a `vm_pra`
job; open it from the toast to follow it.

- The jump is recorded on the VM's deploy record under the same keys a deploy-time jump
  uses, so **Destroy** removes it exactly as it removes any other.
- Re-apply removes the old jump before creating the new one. If the create fails, the VM is
  left with no jump rather than a record of one that no longer exists; run it again.
- On GCP and Azure, adding a jump also records the shared gateway host against the VM, so
  the idle sweep keeps that gateway while the jump needs it.
- **A Windows server** gets the jumps its deploy would have built, from the same code: a
  Shell Jump over OpenSSH and/or an RDP jump, following the access recorded at deploy, each
  with a PRA Vault account holding the administrator password the deploy stored. If
  Password Safe manages the account, the jumps are created with no Vault account, as at
  deploy. A server whose password was neither kept nor handed to Password Safe is refused.
  Re-apply and **Remove from PRA** handle the RDP jump too, and **Destroy** removes it as
  before.
- The same ownership rule as Destroy applies: you need the cloud's `write` permission and
  access to the VM's workstream.

API: `POST /api/{aws|azure|gcp|oci}/instances/pra` with
`{"instance": "<EC2 id | Azure VM name | GCE name | OCI OCID>", "action": "register" | "remove",
"jump_group"?, "jumpoint_name"?, "pra_credential_ref"?}` → `202 {"job_id": ...}`.

## Asset Group and Gateway resolution

Asset Group / Gateway resolution: per-deploy form `jump_group` / `jumpoint_name` → the VM's
workstream default → the per-cloud override (`azure_bt_jump_group_name`/`azure_jumpoint_name`,
`gcp_bt_jump_group_name`/`gcp_jumpoint_name`, `oci_bt_jump_group_name`/`oci_jumpoint_name`) →
the `bt_*` defaults, each read from config and then from its environment setting. Deploys
and the post-deploy action share this resolution (`vm_pra_service.resolve_target`), so both
land in the same place. Deploys and the post-deploy action on all four clouds also accept a
`pra_credential_ref` (overrides `bt_client_secret`); a workstream can
[bind one](#workstream-secret-bindings). **Windows VMs** (AWS, Azure and GCP) get a Shell Jump over OpenSSH
too, and a PRA **Remote RDP** jump item only on request — see
[Windows servers](../windows-servers.md).

The shared gateway host, deploy keys, and PRA OAuth setup are described in the
[Privileged Remote Access](../../beyondtrust/privileged-remote-access.md) doc.
