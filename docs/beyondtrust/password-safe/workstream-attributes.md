# Password Safe workstream attributes

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want what a workstream builds tagged with Password Safe attributes, and filtered by them.

Part of [BeyondTrust Password Safe](../password-safe.md).

A workstream can name Password Safe **attributes** that every managed system onboarded from
it carries — for example `Workstream = team-a`. Write a Smart Rule on that attribute and
Password Safe access follows the dashboard's workstreams, so nobody keeps a second list of
which systems belong to which team.

## Setting them

**Admin only.** On **RBAC → Workstreams**, edit a workstream and open **Password Safe
attributes**. Pick an **Attribute type**, then a **Value**; both lists come from your
Password Safe tenant. Read-only types are listed and refused. Up to 16 per workstream.

Saving checks every entry against the tenant. A type or value Password Safe does not have
is refused by name. To have the dashboard **create** it instead, choose **New type…** or
**New value…** and tick **Create a type or value Password Safe does not have yet**. Only
then does saving write anything new into the tenant. The new type or value is created on
the workstream's oldest onboarded system, so a workstream with nothing onboarded yet is
refused. In that case, create it in the Password Safe console first.

Each save is written to the [audit log](../../admin/audit-log.md) as
`workstream_ps_attributes_update`, including anything it created.

## When they are applied

* **Onboarding.** When a VM deployed into the workstream is onboarded as a managed system
  (see [Onboarding VMs](vm-onboarding.md)), its attributes are assigned to
  that system. The job's progress line reports how many were applied, skipped or failed,
  and the deploy result carries each one under `ps_workstream_attributes`. A failure here
  never fails the deploy or marks the onboarding as failed.
* **Reassign.** Moving an AWS, Azure or GCP VM to another workstream removes the old
  workstream's values from its managed system and assigns the new one's. A value that both
  workstreams set is left alone. The route's response lists each outcome under
  `ps_attributes`.
* **Existing systems.** Systems onboarded before the attributes were set do not carry them.
  **Apply to existing systems** writes them to the managed systems the workstream's
  resources match, using the same matching as the [inventory](../../operations/inventory.md#password-safe-attributes)
  attributes column. It handles 50 systems per click; click again for the next 50. A
  resource that matches two records is listed and left alone. You can pick a Smart Rule to
  re-run afterwards. Each run is audited as `workstream_ps_attributes_apply`.

Every attribute is reported as **ok**, **skipped** or **failed**. Skipped is not success: it
means no call was made, for example because Password Safe is off, or the value was
unchanged on a reassign.

## Writing a Smart Rule on them

In BeyondInsight, create or edit a Smart Rule of type **Managed Systems** (or **Assets**):

1. In the selection criteria, add an attribute filter on your type and value, such as
   `Workstream` = `team-a`. This is the filter the API calls `FilterAssetAttribute`.
2. In the actions, give the team what it should have, for example by making the matched
   systems a Smart Group that the team's Password Safe group has a role on.
3. Save, then re-run the rule. Use **Apply to existing systems** with the rule selected, or
   **Re-run a Smart Rule** on the inventory page.

Use one attribute **type** for workstreams (for example `Workstream`) and one **value** per
workstream. The rule then reads the same way the dashboard does, and a reassign moves a
system from one rule to the other.

The managed-system attribute calls (`POST`/`DELETE ManagedSystems/{id}/Attributes/{id}`,
and creating types and values) have not yet been checked against a live tenant from here.
See [What the public API can and cannot do](public-api.md). If
your appliance refuses them, the per-attribute outcome says so.
