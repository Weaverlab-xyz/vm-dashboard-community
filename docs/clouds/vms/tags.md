# Tags and labels on cloud VMs

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want to see, filter or edit cloud VM tags, across one cloud or the whole estate.

Part of [Cloud VMs](../vms.md).

Every cloud VM listing shows the resource's own tags as chips under its name, and each
page has a **Filter by tag** box that matches a key (`env`) or a key and value
(`env=prod`). Filtering narrows select-all and bulk power with it, so a power button
always acts on what is on screen.

Each platform names the idea differently and the dashboard keeps that vocabulary — the
chip's tooltip tells you which word applies:

| Platform | What it calls them | Shape |
|---|---|---|
| AWS, Azure | tags | key/value |
| GCP | labels | key/value, **lowercase only**, no colons |
| OCI | freeform tags | key/value |
| Proxmox | tags | bare labels, no values |
| Password Safe | attributes | typed values, shown on [Inventory](../../operations/inventory.md#password-safe-attributes) rather than here |
| PRA | tag | not surfaced yet |

Chips are coloured by **who owns the tag**, not by which cloud it came from:

- **Grey with a padlock** — the dashboard wrote it and something depends on it.
  `managed-by` is how `/costs` attributes spend and how unmanaged-VM discovery tells
  its own VMs from yours; `povEnvironment` is what POV teardown selects on;
  `dashboard:desktop_pool` is how a virtual-desktop pool finds its seats. Editing one of
  these in the cloud console will break the thing named in the tooltip.
- **Indigo** — `workgroup`, which decides who can see the resource. Change it through the
  reassign action, which validates the workstream exists, not by hand.
- **Everything else** — your own, coloured per key. The same key is the same colour on
  every page and every cloud, so `env` looks the same on `/aws` and `/proxmox`.

A VM with more than three tags shows the first three and a `+N` chip; hover it for the
rest.

## Filtering the whole estate on one tag

The `/inventory` page has its own **Tags** column and a **Tag** dropdown listing
every key in the estate, so one selection narrows every provider at once. That is the page
to use for "show me everything tagged `pci`" across clouds and hypervisors together.

Its cloud rows are built from deploy jobs, which record what the dashboard *asked for*
rather than what the provider holds now, so their tags are joined in from the per-cloud
instance caches those consoles already fill. The join is **cache-only** — it never makes a
cloud call, because /inventory is a cheap database aggregation and must stay one. The
practical effect: for the first minute after a restart, before the caches warm, cloud rows
may show no tags. Hypervisor rows are unaffected; they carry their tags natively.

**Where tags do not appear yet.** Among the hypervisors only Proxmox reports tags today —
vSphere tags, Nutanix categories and XCP-ng tags need reads the remote agent does not
perform yet, and Hyper-V has no native tag concept. Databases, Kubernetes clusters and
cloud functions show no tags on Inventory. PRA jump-item tags are not read at all yet.
Password Safe **attributes** are a separate column on Inventory, with their own filter and
editor: see [Inventory — Password Safe attributes](../../operations/inventory.md#password-safe-attributes).

## Editing tags

The four cloud VM pages can add and remove tags: **Tags** on a row edits that one VM,
and **Edit tags** on the bulk toolbar applies one change to everything selected. Both open
the same editor and both go through the same route, so one VM or fifty behaves identically.
The cap is fifty, the same as a bulk power operation.

Changes apply immediately — there is no job to watch. Each VM succeeds or fails on its own
and failures are listed **by name with the provider's reason**, because "3 failed" means
checking all fifty. A VM that already had exactly the change asked for is reported as
*already had it* rather than as a success.

**The dashboard's own keys are refused, and the message says why.** The editor never offers
them for removal, and the server refuses them independently — the UI hiding a control is a
promise about one page, while the refusal covers the REST API and the MCP server too:

```
'managed-by' cannot be edited here: it is how /costs attributes this resource
and how the dashboard tells its own VMs from discovered ones
```

`workgroup` is refused with a pointer to the reassign action, which validates the workstream
exists and will not hand a resource to a team you are not in. The full list is the grey
padlocked chips: `managed-by`, `povEnvironment`, `dashboard:desktop_pool`, `clouddb-id`
and the rest.

**Provider rules are checked before anything is written**, so a whole batch is refused with
one sentence instead of collecting fifty identical provider errors. This matters most on
GCP, whose labels are lowercase-only and reject a colon — `Env=Prod` is valid on AWS and
rejected on GCE, and the message says so.

Every change is written to the [audit log](../../admin/audit-log.md), one entry per VM, under the
action `tags.update` — who, which resource, and what changed. Nothing is recorded for a VM
that failed or that was already in the requested state.

Editing needs the cloud's existing **write** permission (`aws:write` and friends); there is
no separate tag permission.

**Proxmox can be edited too** — see [Agent-brokered hypervisors](../../admin/remote-agents/hypervisors.md#the-verbs)
for the one difference that matters: a connection the dashboard dials directly is written
immediately, while an agent-bound one is *queued* as a job. The other hypervisors report
no tags to edit.
