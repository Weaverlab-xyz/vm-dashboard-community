"""Which cloud VMs may carry a suspend schedule, and why the rest may not.

Pure policy — no database, no clock, no cloud — so the reasoning can be tested on a dict
(mirrors ``expiry_policy``, ``suspend_schedule`` and ``pov_spend``, which split the same
way). ``suspend_schedule`` answers *when*; this answers *whether*.

The short version: **a scheduled suspend is only safe where the VM comes back at the same
address, and where nothing else needs to reach it while it is down.** POV never had to ask
either question — its environments are short-lived and its provisioner allocates addresses
statically on purpose. An estate VM is neither.

Three refusals, each one a thing that breaks quietly rather than loudly:

1. **A VM whose address does not survive a deallocate.**
   AWS and GCP keep the private address across a stop, so both are schedulable as
   deployed. Azure does not: ARM allocates the private address dynamically and releases it
   on deallocate, so the VM can return on a different one — and by then the wire-up has
   written the old address into a PRA jump item, a Password Safe managed system and an
   Entitle integration, none of which have an update path (``terraform_pra_service``
   exposes ``provision_jump`` and ``remove_jump`` and nothing between). So an Azure VM is
   schedulable **once its address is pinned** and refused until then;
   ``azure_service.pin_private_address`` does the pinning, new deploys pin themselves, and
   ``api/suspend.py`` pins an older VM the first time somebody schedules it. Pinning
   ratifies the address ARM already chose, so it changes nothing that has been told it.

   OCI is different and cannot be fixed the same way: ``oci_vm_service`` wires the
   **public** address, and an OCI ephemeral public IP is released on stop. After one cycle
   the jump item can point at an address that now belongs to somebody else's instance.
   That is a security break, not an inconvenience, and pinning a private address does not
   touch it.

2. **A VM whose wire-up used its public address is excluded on every cloud.**
   All three prefer ``private_ip`` and fall back to ``public_ip``. The private address
   survives a stop; an auto-assigned public one does not.

3. **A VM under Password Safe auto-management is excluded.**
   AWS onboards via the ``ssm`` plugin, GCP via ``gcpvm`` and Azure via ``azurevm`` — all
   three reach the instance through the cloud's own agent, and none can reach a stopped
   one. Password Safe rotates on its own clock, which this dashboard does not know and
   cannot pause, so a nightly suspend produces a nightly rotation failure in somebody's
   Password Safe. The operator who wants this VM scheduled can detach auto-management;
   that is their call to make deliberately, not ours to make for them by staying quiet.

Every refusal returns a reason, because a greyed-out control with no explanation is the
thing an operator files a bug about.
"""

# The clouds whose address survives a stop, given how this dashboard provisions them.
# Azure is here on the strength of the pin — see refusal 1 and the ``private_ip_static``
# check below, which is what actually holds it to that. OCI is excluded for a reason that
# pinning does not reach.
SCHEDULABLE_CLOUDS = ("aws", "gcp", "azure")

# deploy job_type → cloud, for the four that can carry a schedule at all.
_CLOUD_OF = {
    "ec2_deploy": "aws",
    "gce_deploy": "gcp",
    "azure_deploy": "azure",
    "oci_deploy": "oci",
}


def cloud_of(job_type: str) -> str:
    """The cloud a deploy job belongs to, or ``""`` if it is not a cloud VM deploy."""
    return _CLOUD_OF.get(job_type or "", "")


def deploy_types_for(clouds) -> tuple:
    """The deploy job types belonging to ``clouds``, in declaration order.

    So a caller that has to name job types in a query — ``suspend_sweeper``'s row
    selection — derives them from this map rather than restating them. A cloud missing
    from such a filter is never even looked at, which is a silent way for a schedule to
    do nothing.
    """
    return tuple(t for t, c in _CLOUD_OF.items() if c in clouds)


def schedulable(job_type: str, meta: dict) -> tuple:
    """``(ok, reason)`` — may this VM carry a suspend schedule?

    ``reason`` is empty when ``ok``; otherwise it is a sentence for the operator, naming
    what to change where that is possible.
    """
    meta = meta or {}
    cloud = cloud_of(job_type)
    if not cloud:
        return (False, "Only cloud VMs can carry a suspend schedule.")

    if cloud == "oci":
        return (False,
                "OCI instances are excluded: their wire-up uses the public address, and "
                "an ephemeral public IP is released on stop. After one suspend the jump "
                "item could point at an address that now belongs to somebody else.")
    if cloud not in SCHEDULABLE_CLOUDS:      # pragma: no cover — defensive
        return (False, f"{cloud} VMs cannot be scheduled.")

    # Wired into PRA / Entitle / Password Safe at a PUBLIC address? Then the address does
    # not survive the stop on any of these clouds.
    wired = any(meta.get(k) for k in
                ("bt_tf_state", "ps_registration_tf_state", "entitle_registration_tf_state"))
    if wired and not meta.get("private_ip"):
        return (False,
                "This VM was wired into BeyondTrust at its public address, which is "
                "released when it stops. Only VMs reached on a private address can be "
                "scheduled.")

    # Password Safe auto-management reaches the instance through the cloud's own agent
    # (ssm on AWS, gcpvm on GCP, azurevm on Azure) and cannot reach a stopped one.
    if meta.get("ps_managed_system_id") or meta.get("ps_registration_tf_state"):
        return (False,
                "This VM is managed by Password Safe, which rotates on its own schedule "
                "and cannot reach a stopped instance — a nightly suspend would produce a "
                "nightly rotation failure. Detach auto-management to schedule it.")

    # LAST, deliberately: every refusal above is one that pinning cannot fix, and a VM
    # about to be refused for one of them must not have its NIC written to first. That
    # ordering is the whole reason `needs_address_pin` below can be a one-liner.
    if cloud == "azure" and not meta.get("private_ip_static"):
        return (False,
                "This Azure VM's private address is still dynamic, so a deallocated VM "
                "could come back on a different one. Setting a schedule pins the address "
                "it already has — the address does not change.")

    return (True, "")


def needs_address_pin(job_type: str, meta: dict) -> bool:
    """True when an unpinned Azure address is the **only** thing between this VM and a
    schedule.

    Asked before pinning, so that a VM which would be refused anyway — for Password Safe,
    or for a public-address wire-up — never has its NIC written to. Phrased as a question
    to :func:`schedulable` rather than a second copy of the rules: *would this be
    schedulable if its address were pinned?* Two copies of a policy drift invisibly, and
    this one decides whether to make a cloud call.
    """
    meta = meta or {}
    if cloud_of(job_type) != "azure" or meta.get("private_ip_static"):
        return False
    ok, _ = schedulable(job_type, {**meta, "private_ip_static": True})
    return ok


def describe(job_type: str, meta: dict) -> dict:
    """The schedulability answer in the shape a page can render without re-deriving it."""
    ok, reason = schedulable(job_type, meta)
    return {"schedulable": ok, "reason": reason, "cloud": cloud_of(job_type),
            # So a caller can say what pressing Save will do before it is pressed, rather
            # than reporting a pin after the fact.
            "needs_address_pin": needs_address_pin(job_type, meta)}
