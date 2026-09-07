"""Cloud VMs this dashboard did not deploy: what counts as one, and what may be done to it.

Pure policy — no cloud SDK, no database, no clock — so the reasoning can be tested on
dicts, the way ``vm_suspend_policy``, ``expiry_policy`` and ``suspend_schedule`` already
split. The per-cloud listing calls live in each ``*_service``; this decides what to do
with what they return.

**Why the estate needs this.** Every cloud console here is *job-driven*: it starts from
completed ``*_deploy`` jobs and fetches live state for exactly those identifiers. That is
the right default — it is what makes the console a record of what the dashboard did — but
it means a VM somebody launched in the console, in Terraform, or before this dashboard
existed is invisible, and the operator's only lever on it is the cloud provider's own UI.
The four clouds' ``/power/*`` endpoints already exist; they simply had nothing to point at.

**The rule: discovered, therefore powerable — never destroyable.**

Power is reversible and its blast radius is one VM that comes back. Destroy is neither, and
on a resource the dashboard did not create it is worse than irreversible: the dashboard
holds no record of what the thing was, so there is nothing to reason about afterwards and
nobody expects this tool to have been the one that removed it. So an unmanaged VM is
offered start and stop and nothing else — and not by hiding a button, which is a UI
promise rather than a guarantee, but by never resolving one into a destroy path at all.
:func:`assert_not_unmanaged` is the guarantee, and ``api/azure`` calls it on the one
destroy route that can act without a deploy job.

**Two sets, disjoint by construction.** A row is *unmanaged* when the dashboard has no
deploy job for it **and** it carries none of the dashboard's own tags. The tag half
matters: a VM this dashboard created whose job row was pruned, and a VDI pool seat, are
both dashboard-managed and both already appear in the managed listing. Without the tag
check they would appear in both lists and — worse — a tagged VM would start refusing the
destroy that is legitimately its own.
"""

# What the dashboard stamps on everything it creates. The canonical pair plus the legacy
# one kept for resources created before the #194 tag normalization — the same two
# ``azure_service._is_dashboard_managed`` accepts, spelled once here so the four clouds
# cannot come to disagree about what "ours" means.
MANAGED_TAGS = (
    ("managed-by", "vm-dashboard"),
    ("ManagedBy", "vm-cli-dashboard"),
)
# Deliberately NOT here: `vm-dashboard-node` and `dashboard-sandbox`. Both are written by
# this dashboard, but `tests/test_managed_by_tag_values.py` records that the estate already
# treats them as outside dashboard scope — /costs excludes them. Discovery follows the
# scope the product already draws rather than drawing a second one: a container node or a
# sandbox resource shows up as discovered, which is honest (the VM console has never listed
# it) and safe (discovered means powerable, never destroyable).

# The tag an operator can put on their own VM to place it in a workgroup. Same key the
# dashboard writes on the VMs it deploys, so an estate that already tags for workgroups
# gets non-admin visibility with no extra work.
WORKGROUP_TAG_KEYS = ("workgroup", "Workgroup")

# The identifier each cloud's rows are keyed on — the one a power call needs.
ID_KEY = {
    "aws": "instance_id",
    "azure": "vm_name",
    "gcp": "instance_name",
    "oci": "instance_ocid",
}


class UnmanagedVMError(Exception):
    """Raised when something is attempted on a VM the dashboard did not deploy that is
    only defensible on one it did."""


def is_dashboard_tagged(tags: dict) -> bool:
    """True when a cloud resource carries a tag this dashboard writes."""
    tags = tags or {}
    return any(tags.get(k) == v for k, v in MANAGED_TAGS)


def workgroup_of(tags: dict) -> str:
    """The workgroup an operator tagged this VM with, lowercased, or ``""``.

    Read rather than assigned: the dashboard never writes a workgroup onto a VM it did not
    deploy. An untagged VM stays workgroup-less, which — by the rule every cloud module
    already applies in ``_assert_can_act`` — makes it admin-only. That is the conservative
    default and it is not a new rule, so an operator who already tags for workgroups gets
    the behaviour they expect and one who does not gets no surprise widening.
    """
    tags = tags or {}
    for key in WORKGROUP_TAG_KEYS:
        value = (tags.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def is_unmanaged(row: dict, managed_ids: set, cloud: str) -> bool:
    """Is this live cloud row one the dashboard did not deploy?

    Both halves are required — see the module docstring. ``managed_ids`` is every
    identifier the deploy jobs know about, destroyed ones included: a VM whose destroy job
    failed halfway is still the dashboard's business, and listing it as somebody else's
    would invite exactly the wrong repair.
    """
    identifier = row.get(ID_KEY[cloud])
    if identifier and identifier in managed_ids:
        return False
    return not is_dashboard_tagged(row.get("tags") or {})


def partition(live_rows: list, managed_ids: set, cloud: str) -> list:
    """The unmanaged subset of a cloud listing, each row carrying its workgroup.

    ``managed`` is stamped ``False`` on every row rather than left implied. A consumer that
    has to infer it from the absence of ``job_id`` gets it wrong the first time a managed
    row arrives with a pruned job, and this list is the one place where being wrong means
    offering an action that should not exist.
    """
    out = []
    for row in live_rows or []:
        if not is_unmanaged(row, managed_ids, cloud):
            continue
        out.append({**row,
                    "cloud": cloud,
                    "managed": False,
                    "workgroup": workgroup_of(row.get("tags") or {}) or None,
                    "job_id": None,
                    "deployed_by": None})
    return out


def visible_to(rows: list, accessible) -> list:
    """Filter to what this caller may see. ``accessible=None`` means admin — everything.

    An unmanaged VM with no workgroup tag is admin-only, which is the same answer
    ``_assert_can_act`` gives an untagged managed resource. Stated here so the listing and
    the power endpoint cannot disagree about who owns what — the invariant
    ``api/vms.py:233`` names: what you can act on and what you can see must not diverge.
    """
    if accessible is None:
        return list(rows or [])
    return [r for r in rows or [] if r.get("workgroup") and r["workgroup"] in accessible]


def assert_not_unmanaged(tags: dict, what: str) -> None:
    """Refuse a destructive action on a VM the dashboard did not deploy.

    Called on the destroy routes that can act **without** a deploy job — the ones that
    resolve a VM by name against the live cloud, where "no job row" is a legitimate state
    (a VDI pool seat, a VM whose job was pruned) rather than proof of ownership. Those
    routes were reachable for any VM in a listed resource group before discovery existed;
    discovery is what makes the name easy to find, so the guard belongs here whether or not
    the caller came through it.
    """
    if not is_dashboard_tagged(tags):
        raise UnmanagedVMError(
            f"{what} was not deployed by this dashboard and carries none of its tags, so "
            f"it cannot be destroyed from here. Power actions are available; destroy it "
            f"wherever it was created, where whatever else depends on it is also recorded.")
