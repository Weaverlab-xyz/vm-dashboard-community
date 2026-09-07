"""Which cloud VMs may carry a suspend schedule, and why the rest may not.

Pure policy — no database, no clock, no cloud — so the reasoning can be tested on a dict
(mirrors ``expiry_policy``, ``suspend_schedule`` and ``spend_policy``, which split the same
way). ``suspend_schedule`` answers *when*; this answers *whether*.

The short version: **a scheduled suspend is only safe where the VM comes back at the same
address, and where nothing else needs to reach it while it is down.** POV never had to ask
either question — its environments are short-lived and its provisioner allocates addresses
statically on purpose. An estate VM is neither.

Three refusals, each one a thing that breaks quietly rather than loudly:

1. **A VM wired into BeyondTrust at its PUBLIC address**, on any cloud.
   The wire-up writes one address into a PRA jump item, a Password Safe managed system and
   an Entitle integration, and ``terraform_pra_service`` exposes ``provision_jump`` and
   ``remove_jump`` and nothing between — so an address that moves cannot be repaired short
   of destroy-and-recreate, which mints a new Shell Jump and drops the association. None of
   the four clouds guarantees an auto-assigned public address across a stop, so a VM
   reached on one is refused. A private address survives a stop on all four.

   **Which address was wired is recorded, not inferred** — see :func:`wired_address`. That
   distinction is the whole reason OCI can be scheduled at all: three runners prefer the
   private address and OCI prefers the public one, so "does it have a private address?"
   answers the question for three clouds and the wrong question for the fourth. An OCI
   instance deployed with ``assign_public_ip=False`` is wired privately and is safe; one
   with a public address is refused, and honestly has no remedy short of redeploying,
   because moving it to a reserved public IP would change the address that has already been
   written into all three systems.

2. **An Azure VM whose private address is not pinned.**
   ARM allocates it dynamically and releases it on deallocate, so the VM can return on a
   different one. Azure is schedulable **once its address is pinned** and refused until
   then; ``azure_service.pin_private_address`` does the pinning, new deploys pin themselves,
   and ``api/suspend.py`` pins an older VM the first time somebody schedules it. Pinning
   ratifies the address ARM already chose, so it changes nothing that has been told it.

3. **A VM under Password Safe auto-management is excluded.**
   AWS onboards via the ``ssm`` plugin, GCP via ``gcpvm`` and Azure via ``azurevm`` — all
   three reach the instance through the cloud's own agent, and none can reach a stopped
   one. (OCI uses plain ``ssh``, which has the same problem for the same reason: a stopped
   instance answers nothing.) Password Safe rotates on its own clock, which this dashboard
   does not know and cannot pause, so a nightly suspend produces a nightly rotation failure
   in somebody's Password Safe. The operator who wants this VM scheduled can detach
   auto-management; that is their call to make deliberately, not ours to make for them by
   staying quiet.

Every refusal returns a reason, because a greyed-out control with no explanation is the
thing an operator files a bug about.
"""

# All four. None of them is schedulable unconditionally — the refusals below decide, per
# VM, on facts the deploy recorded. A cloud listed here is one whose VMs *can* qualify.
SCHEDULABLE_CLOUDS = ("aws", "gcp", "azure", "oci")

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


# Each runner's fixed preference when it chose the address to hand PRA, Entitle and
# Password Safe. Used only to reconstruct rows written before `wired_address` was
# recorded — every runner sets it now.
_LEGACY_PREFERENCE = {
    "aws":   ("private_ip", "public_ip"),
    "gcp":   ("private_ip", "public_ip"),
    "azure": ("private_ip", "public_ip"),
    "oci":   ("public_ip", "private_ip"),      # the odd one out, and the reason this exists
}


def wired_address(job_type: str, meta: dict) -> str:
    """The address this VM's wire-up actually wrote into PRA, Entitle and Password Safe.

    Recorded by every runner as ``wired_address``. For rows written before that — the
    fleet as it stands — it is **reconstructed rather than guessed**: each runner had one
    fixed preference and the recorded addresses are still there, so replaying the
    preference gives the same answer the runner gave.

    Returns ``""`` when neither address is recorded. Note the runners fall back to an
    instance id or name when neither address came back; such a VM is not reachable at a
    public address either, so ``""`` is treated as "not public" by the caller.
    """
    meta = meta or {}
    recorded = meta.get("wired_address")
    if recorded:
        return recorded
    first, second = _LEGACY_PREFERENCE.get(cloud_of(job_type), ("private_ip", "public_ip"))
    return meta.get(first) or meta.get(second) or ""


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

    if cloud not in SCHEDULABLE_CLOUDS:      # pragma: no cover — defensive
        return (False, f"{cloud} VMs cannot be scheduled.")

    # Wired into PRA / Entitle / Password Safe at a PUBLIC address? Then the address the
    # jump item holds does not survive the stop, on any of the four. Asked of the address
    # that was actually wired — inferring it from "does a private address exist?" answers
    # the right question on three clouds and the wrong one on OCI, which wires the public
    # address by preference because it has no dashboard-provisioned gateway in the VCN.
    wired = any(meta.get(k) for k in
                ("bt_tf_state", "ps_registration_tf_state", "entitle_registration_tf_state"))
    public = meta.get("public_ip")
    if wired and public and wired_address(job_type, meta) == public:
        return (False,
                "This VM was wired into BeyondTrust at its public address, which is not "
                "guaranteed to survive a stop. Only VMs reached on a private address can "
                "be scheduled, and the address already written into the jump item, "
                "Password Safe system and Entitle registration cannot be changed in "
                "place — so this one would have to be redeployed without a public "
                "address to carry a schedule.")

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
