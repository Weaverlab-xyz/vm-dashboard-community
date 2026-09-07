"""Which cloud VMs may carry a suspend schedule, and why the rest may not.

Pure policy — no database, no clock, no cloud — so the reasoning can be tested on a dict
(mirrors ``expiry_policy``, ``suspend_schedule`` and ``pov_spend``, which split the same
way). ``suspend_schedule`` answers *when*; this answers *whether*.

The short version: **a scheduled suspend is only safe where the VM comes back at the same
address, and where nothing else needs to reach it while it is down.** POV never had to ask
either question — its environments are short-lived and its provisioner allocates addresses
statically on purpose. An estate VM is neither.

Three refusals, each one a thing that breaks quietly rather than loudly:

1. **Azure and OCI are excluded.**
   ``azure_service._deploy_vm_sync`` allocates the private address ``Dynamic`` (POV's
   allocates ``Static``, deliberately), so a deallocated Azure VM can return on a different
   one. By then the wire-up has written the old address into a PRA jump item, a Password
   Safe managed system and an Entitle integration — and ``terraform_pra_service`` exposes
   ``provision_jump`` and ``remove_jump`` and no update, so there is no repair short of
   destroy-and-recreate, which mints a new Shell Jump and drops the association.
   OCI is worse and differently: ``oci_vm_service`` wires the **public** address, and an
   OCI ephemeral public IP is released on stop. After one cycle the jump item can point at
   an address that now belongs to somebody else's instance. That is a security break, not
   an inconvenience.

2. **A VM whose wire-up used its public address is excluded even on AWS and GCP.**
   Both prefer ``private_ip`` and fall back to ``public_ip``. The private address survives
   a stop on both clouds; an auto-assigned public one does not.

3. **A VM under Password Safe auto-management is excluded.**
   AWS onboards via the ``ssm`` plugin and GCP via ``gcpvm`` — both reach the instance
   through the cloud's own agent, and neither can reach a stopped one. Password Safe
   rotates on its own clock, which this dashboard does not know and cannot pause, so a
   nightly suspend produces a nightly rotation failure in somebody's Password Safe. The
   operator who wants this VM scheduled can detach auto-management; that is their call to
   make deliberately, not ours to make for them by staying quiet.

Every refusal returns a reason, because a greyed-out control with no explanation is the
thing an operator files a bug about.
"""

# The two clouds whose address survives a stop. Azure and OCI are excluded for the
# reasons in the module docstring — reasons about how THIS dashboard provisions and wires
# them, not about the clouds themselves, so both are fixable later rather than forever.
SCHEDULABLE_CLOUDS = ("aws", "gcp")

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


def schedulable(job_type: str, meta: dict) -> tuple:
    """``(ok, reason)`` — may this VM carry a suspend schedule?

    ``reason`` is empty when ``ok``; otherwise it is a sentence for the operator, naming
    what to change where that is possible.
    """
    meta = meta or {}
    cloud = cloud_of(job_type)
    if not cloud:
        return (False, "Only cloud VMs can carry a suspend schedule.")

    if cloud == "azure":
        return (False,
                "Azure VMs are excluded: this dashboard allocates their private address "
                "dynamically, so a deallocated VM can come back on a different one and "
                "invalidate its PRA jump item, Password Safe system and Entitle "
                "registration — none of which have an update path.")
    if cloud == "oci":
        return (False,
                "OCI instances are excluded: their wire-up uses the public address, and "
                "an ephemeral public IP is released on stop. After one suspend the jump "
                "item could point at an address that now belongs to somebody else.")
    if cloud not in SCHEDULABLE_CLOUDS:      # pragma: no cover — defensive
        return (False, f"{cloud} VMs cannot be scheduled.")

    # Wired into PRA / Entitle / Password Safe at a PUBLIC address? Then the address does
    # not survive the stop even here.
    wired = any(meta.get(k) for k in
                ("bt_tf_state", "ps_registration_tf_state", "entitle_registration_tf_state"))
    if wired and not meta.get("private_ip"):
        return (False,
                "This VM was wired into BeyondTrust at its public address, which is "
                "released when it stops. Only VMs reached on a private address can be "
                "scheduled.")

    # Password Safe auto-management reaches the instance through the cloud's own agent
    # (ssm on AWS, gcpvm on GCP) and cannot reach a stopped one.
    if meta.get("ps_managed_system_id") or meta.get("ps_registration_tf_state"):
        return (False,
                "This VM is managed by Password Safe, which rotates on its own schedule "
                "and cannot reach a stopped instance — a nightly suspend would produce a "
                "nightly rotation failure. Detach auto-management to schedule it.")

    return (True, "")


def describe(job_type: str, meta: dict) -> dict:
    """The schedulability answer in the shape a page can render without re-deriving it."""
    ok, reason = schedulable(job_type, meta)
    return {"schedulable": ok, "reason": reason, "cloud": cloud_of(job_type)}
