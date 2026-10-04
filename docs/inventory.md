# Inventory

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want one list of everything the dashboard deployed or manages, across every cloud and hypervisor — to filter it by tag or Password Safe attribute, act on a selection, or see what is about to expire.

**Inventory** in the nav — the **Deployment Inventory** page at `/inventory` — is the one
list that crosses providers. Every other console shows one cloud or one hypervisor; this
page shows them together, so "everything tagged `pci`" or "every host Password Safe calls
`Status = Retired`" is one filter rather than ten pages.

It is a **read of the dashboard's own records**, not a live sweep of every provider. A
cloud VM appears because a deploy job created it; a hypervisor VM appears because its
connection was synced. That is what keeps the page fast, and it is also why it can briefly
lag the provider (see [Tags](#tags) below). Rows are filtered by **workgroup**: you see
what your workgroups own, and an admin sees everything. Reading it needs the
`inventory:read` permission — see [Permissions](access/permissions.md).

---

## Filtering

The filter bar has **Provider**, **Kind**, **Region**, **State** and **Workgroup**, each
listing only the values present in your estate. Two more appear only when they have
something to offer:

* **Tag** — every tag key in the estate. See [Tags](#tags).
* **Attribute** — every Password Safe attribute type on a matched row, when Password Safe
  is enabled. See [Password Safe attributes](#password-safe-attributes).

The counter under the title reads *N of M resources*, so you can always tell how much a
filter hid.

---

## Choosing columns

The table has up to thirteen columns, more than fit. **Columns** above the table opens the
picker:

* **Name** and **Details** always show — Name is pinned to the left, Details is how you
  leave the page.
* A column that is **empty on every row** starts hidden, and the picker says so. That is
  recomputed on every load and never saved, so a column that gains data reappears on its
  own.
* A column you are **filtering on** is never auto-hidden, so a filter is never left with
  nothing on screen to explain it.
* What **you** hide is remembered in this browser. It stores what is hidden, not what is
  shown, so a column added in a later release appears for you rather than staying hidden.
* **Show every column** resets your choice.

---

## Tags

The **Tags** column shows each resource's own cloud tags or hypervisor tags, with the same
chips, colours and padlocked dashboard-owned keys as the per-cloud pages. Cloud rows take
their tags from the per-cloud caches those pages already fill, so for about a minute after
a restart they can show none. Hypervisor rows carry their tags natively.

Tags are **edited** on the cloud and Proxmox pages, not here. Which platforms report tags,
and how editing works, is in [Cloud VMs — Tags and labels](cloud/vms.md#tags-and-labels).

---

## Password Safe attributes

When the [Password Safe integration](integrations/beyondtrust/password-safe.md) is enabled
(`password_safe_enabled`) and has API credentials, the **Password Safe** column shows each
matched resource's **attributes** as `Type = Value` chips — `Status = Online`,
`Business Unit = Finance`. Attributes are how Password Safe's **Smart Rules** decide which
systems a policy applies to, so this column is where you see, and change, why a host is or
is not in a rule.

**How a row is matched.** First by the Password Safe id recorded when the dashboard
onboarded the system, then by address. The id comes first because a cloud-native
managed system has no usable address of its own. A row can match an **asset**, a
**managed system**, or both. A row two Password Safe records both claim is shown as
ambiguous rather than guessed.

**What the column can tell you**, beyond the chips:

* *not fetched* — this row matched, but its attributes were not read this time. A read
  covers at most 200 matched objects, and the page says when it hit that cap.
* A header note when the tenant-level read did not fully work: Password Safe is
  unreachable, this appliance version does not serve an endpoint, or the credentials lack
  permission. An empty column never silently means "no attributes".
* Admins also see a panel of **Password Safe records nothing here corresponds to**. These
  are usually hosts that were decommissioned without being removed from Password Safe.

The read is cached for 15 minutes. **Refresh** re-reads the dashboard's records, not
necessarily Password Safe.

### Assigning and removing attributes

**Admin only.** Use **Attributes** on a row for one resource, or select rows and use
**Edit attributes on N** for up to 50 at once. The count covers only the rows that have a
writable Password Safe record, so it never promises more than it can do.

* Pick an **Attribute type**, then a **Value**. There is no free text: each type owns a
  fixed list of values in Password Safe. Read-only types, such as `Criticality` on many
  tenants, are named in the editor and refused, not silently left out.
* The editor lists the records it will write to **before** you apply. A resource matched
  to both an asset and a managed system is written to both, because they are separate
  records in Password Safe and the chip shows them merged.
* Each target succeeds or fails on its own, and failures are listed by name. Removing an
  attribute the record no longer carries counts as success, so retrying a partly failed
  apply is safe.
* Every change is written to the [audit log](access/audit-log.md) as `attributes.assign` or
  `attributes.remove`, one entry per record.

### Re-running a Smart Rule

Changing an attribute does not re-evaluate the Smart Rules that use it. **Re-run a Smart
Rule** in the same editor asks Password Safe to process one rule now. It is a separate
button on purpose: you choose when a rule acts on your change. Password Safe processes it
asynchronously, so give it a moment before checking the result.

---

## Acting on a selection

Tick rows to select them. A selection is **one kind** at a time; other kinds are locked
while it is active. With a selection you can:

* **Run a playbook** on every selected resource — one job per resource. To add a
  Secrets-Management secret or a Password Safe managed account to the run, use **Continue
  on the Config Management page**, which carries the selection over. See
  [Config Management](config-management.md).
* **Edit attributes on N**, as above.

---

## Expiry and auto-delete

When the [auto-delete timer](operations/auto-delete-timer.md) feature is on, a banner at the top says
whether anything is actually being deleted. The **Expires** column shows each resource's
expiry, and you can change a resource's timer from its row. Read the auto-delete page
before enabling it: it destroys infrastructure.

---

## See also

* [Cloud VMs](cloud/vms.md) — tags, power and deploys per cloud.
* [Password Safe](integrations/beyondtrust/password-safe.md) — setup, and the API behaviour behind the
  attributes column.
* [Scheduling](scheduling.md) — running a change later or on a window.
* [Permissions](access/permissions.md) — workgroups, and who sees which rows.
