"""Personas: which role's story the dashboard leads with. **Curation only.**

This is the second of two axes, and the distinction between them is the whole design.

``services/feature_flags.install_profile`` is a **gate**. It exists because of tenancy: a
demo instance resolves its BeyondTrust tenant from the global singletons, a POV instance
from a registry of named customer tenants, and an instance claiming both roles would have
two answers to "which tenant?" at every call site -- with the wrong answer being silent
rather than loud. So it is mutually exclusive, it subtracts, and it 404s routes.

A persona is **not** a tenancy decision. A DevOps demo and a hypervisor-admin demo have no
conflict; they run on the same estate on different days. So a persona may only ever
reorder, emphasise and surface. It may not hide a page, mask a flag, or 404 a route.
Everything a neutral instance can reach, every persona can still reach.

That single property is what licenses the rest of the design, and three things follow from
it:

  * **This module is not ``feature_flags``.** Every function in that file is a gate, and
    its docstrings are one sustained argument that exactly one reader decides whether a
    thing exists. Putting ``resolve()`` in there would put a curation-only value one indent
    away from ``if profile_masks(flag): return False`` -- where the next author's one-line
    "optimisation" would 404 routes and look completely idiomatic. The import direction is
    one-way and asserted: ``personas`` imports ``feature_flags``, never the reverse.
  * **The active persona may come from a cookie, and that is safe.** A cookie is untrusted
    input; here the worst an attacker achieves by editing ``document.cookie`` is reordering
    their own nav. **If a persona ever gains the power to hide anything, the cookie becomes
    wrong the same day** -- that is not a hypothetical, it is the review checklist item.
  * **A card navigates. It never starts work.** A card that POSTed a deploy would make
    "curation only" false -- the persona layer could spend money -- and would need the CSRF
    and permission thinking that a link does not. The card takes you to the form; the
    operator presses the button.

There is a THIRD axis, added for POV work, and it obeys the same rule as the persona.
On a POV instance the question "can I run this demo?" is not "is the flag on?" but "does
THIS POV have that product wired?" -- a POV carries its own PRA / Password Safe / Entitle
tenants, independently, so a Password-Safe-only POV and an all-three POV run different
lists. ``pov_use_cases`` and :func:`pov_catalog` serve that, and they still never subtract:
a card whose product this POV does not have is rendered and *explained*, exactly as a
masked card is.

That axis needs a database row to resolve, and this module may not have one. So the POV
resolvers take a plain **products dict** of booleans; ``services/pov_use_cases`` is the
module that knows about ``PovEnvironment``. Keeping the row out of here is what preserves
the dependency rule below.

Dependencies are ``config_service``, ``settings`` and ``feature_flags`` only -- nothing
from the api layer, and nothing from ``database`` -- so ``api/docs_pages`` can import this
without acquiring a hard dependency it is deliberately built to survive without.
"""
from dataclasses import dataclass

from . import config_service, feature_flags


# The neutral persona: no focus. Renders today's dashboard, today's nav, today's order,
# byte for byte. It is a real value rather than None so callers never branch on a type.
NEUTRAL = ""

VALID_PERSONAS = (
    "cloudops",
    "devops",
    "hypervisor",
    # `itops`, not `it`: two letters is ungreppable, and `persona == 'it'` reads like a
    # typo in every conditional it ever appears in.
    "itops",
    "ot",
    "dba",
    "security",
    "sre",
)

# Human names for the flags a card can require, for the "Needs: ..." copy. A card names a
# flag; the operator needs to be told which Settings panel to go to, and `pra_enabled` is
# not that. Every flag referenced by any card must have an entry here (pinned by test).
_FLAG_LABELS = {
    "pra_enabled": "Privileged Remote Access",
    "password_safe_enabled": "Password Safe",
    "entitle_enabled": "Entitle",
    "entitle_registration_enabled": "Entitle resource registration",
    "entitle_user_jit_enabled": "Entitle user JIT",
    "ansible_enabled": "Configuration Management",
    "epml_enabled": "Endpoint Privilege Management for Linux",
    "vmware_enabled": "Workstation",
    "vsphere_enabled": "vSphere",
    "proxmox_enabled": "Proxmox",
    "hyperv_enabled": "Hyper-V",
    "nutanix_enabled": "Nutanix",
    "xcpng_enabled": "XCP-ng",
    "vdesktops_enabled": "Virtual desktops",
    "cloud_database_enabled": "Cloud databases",
    "cloud_functions_enabled": "Cloud functions",
    "k8s_management_enabled": "Kubernetes",
    "portainer_enabled": "Portainer",
    "cost_explorer_enabled": "Cost reporting",
    "remote_agents_enabled": "Remote agents",
    "admission_control_enabled": "Admission control",
    "resource_expiry_enabled": "Auto-delete timer",
    "notifications_enabled": "Notifications",
}

_CLOUD_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI"}

# ── the POV axis ─────────────────────────────────────────────────────────────
#
# The three BeyondTrust products a POV can be wired into, named as PRODUCTS rather than as
# flags. That is the whole distinction: `pra_enabled` is an instance-wide toggle, while
# `pra` here means "this POV has a PRA tenant on its row". Both are true on a POV instance
# running a Password-Safe-only POV, and only the second one answers the question a card
# asks. Sharing the flag vocabulary is how the two would silently become one.
POV_PRODUCTS = ("pra", "password_safe", "entitle")

_PRODUCT_LABELS = {
    "pra": "Privileged Remote Access",
    "password_safe": "Password Safe",
    "entitle": "Entitle",
}

# The artifact each product leaves on a POV once the wire-up has actually run. A POV can
# name a tenant and have nothing wired into it yet, and those are different answers: the
# first is "this POV does not include that product" (nothing to do), the second is "it does,
# and there is a button to press". `services/pov_use_cases.products_for` fills both halves.
_PRODUCT_ARTIFACT = {
    "pra": "wired",
    "password_safe": "onboarded",
    "entitle": "entitle_wired",
}

# What to run when the tenant is there and the artifact is not. Named per product because
# the Entitle half needs something the other two do not -- an SSH key on the POV row, which
# no other step can derive -- and "run the wire-up" alone would send an operator round a
# loop that keeps skipping the one thing they came for.
_PRODUCT_REMEDY = {
    "pra": "PRA jump items — run the wire-up",
    "password_safe": "Password Safe onboarding — needs a Resource Broker, then the wire-up",
    "entitle": "the Entitle integration — needs an SSH key on this POV, then the wire-up",
}

# Where a card sends an operator when it is unready. One destination, not a per-card field:
# every remedy above ends at the same tab, and a card that could name its own would
# eventually name one that does not exist.
_POV_ACTION_TAB = "#wired"


@dataclass(frozen=True)
class UseCase:
    """One guided demo an SE can run end to end.

    ``target`` is an in-app path and MAY carry a ``#hash``. That is not a shortcut -- it is
    the only way some surfaces are reachable at all. ``api/ot.py`` has no page of its own
    (``main.py`` mounts it under ``_feature_gate("pra_enabled")``) and exists solely as an
    Alpine ``activeTab`` deep-link on ``/aws``, ``/azure`` and ``/gcp``. A test parses both
    halves: the path must be a real ``@app.get`` HTML route, and the fragment must be a
    real tab value in that route's template.

    ``requires_flags`` names **flag names** (``pra_enabled``), not ``/api/features`` keys
    (``pra``), because readiness is resolved server-side through
    ``feature_flags.enabled()`` -- the one reader -- and a second naming convention here
    would be a mapping layer with nothing to gain.

    ``requires_clouds`` is any-of, and it is separate from ``requires_flags`` because a
    cloud is not gated by a flag: it is "configured" iff credentials are present
    (``feature_flags.cloud_configured``). It also carries the profile interaction -- the
    cloud consoles are a profile-owned PAGE GROUP, so a card needing a cloud is *masked*
    on a POV instance rather than merely unready.
    """
    # `id`, deliberately not `key`. tests/test_dashboard_collect.py finds the dashboard's
    # tiles by regexing `{ key: '...'` out of dashboard.html, and a card rendered into that
    # page carrying a `key:` would be scanned as a tile -- failing
    # test_every_cloud_backed_tile_is_collected with a message about a missing collector
    # that sends the next reader somewhere else entirely.
    id: str
    title: str
    summary: str
    target: str
    minutes: int
    docs: str = ""                   # path under docs/, no .md suffix
    requires_flags: tuple = ()
    # Any-of, for a page whose own guard is any-of. `/connections` is the case that
    # motivated it: it 404s unless ANY of six hypervisor flags is on, and that guard is
    # written INLINE in the route body rather than as a `_feature_gate` in the decorator --
    # so a card requiring only `pra_enabled` read `ready` and offered a live link to a page
    # that 404s on every POV instance. All six of those flags are _DEMO_ONLY, which is why
    # an any-of guard has to be modelled as any-of here: requiring one specific hypervisor
    # would overstate what the page needs.
    requires_any_flag: tuple = ()
    requires_clouds: tuple = ()
    # POV cards only, and all-of like ``requires_flags``. Names members of
    # :data:`POV_PRODUCTS` -- which are NOT flags: see the note there. A card with none is
    # always in scope, which is how the oversight cards stay useful on a POV wired into a
    # single product.
    #
    # A demo card never sets this and a POV card never sets the three fields above: the two
    # lists resolve through different readers, and a card carrying both would be answering
    # a question nobody asked it.
    requires_products: tuple = ()


@dataclass(frozen=True)
class Persona:
    """One role, and what the dashboard should lead with for it.

    Every ordering field is a **hint, never a filter**. ``section_order`` names dashboard
    tile sections best-first; any section id absent from the tuple keeps its shipped
    relative order and follows the named ones. Dropping a section id from this tuple must
    never remove it from the page -- that is the difference between this module and
    ``feature_flags``.
    """
    key: str
    label: str
    blurb: str
    section_order: tuple = ()
    tile_emphasis: tuple = ()
    nav_pins: tuple = ()
    quick_deploy: tuple = ()
    # Narrative docs for this role: what they own, why the PAM story lands, and which card
    # to run. Kept separate from a UseCase.docs (which is the runbook for one demo) because
    # the role story and the feature reference are different documents with different
    # lifespans. A test asserts every path here resolves to a real file under docs/.
    docs: tuple = ()
    # The wizard PRE-TICKS these on its Features step. Only ever to true -- the profile
    # mask rule inverted: a persona may suggest a feature, never remove one.
    #
    # Every entry must be a toggle that step actually RENDERS, or the preset is a silent
    # no-op; tests/test_persona_wizard pins that. Flags a focus needs but the wizard does
    # not offer (the auto-delete timer, notifications, virtual desktops) are deliberately
    # absent rather than listed and ignored -- the cards already report them as
    # `needs_flag` and point at Settings, which is where they are configured.
    #
    # They are NOT added to the wizard to make this list longer: `_apply_config` writes
    # every feature flag unconditionally, so a reconfigure that left the auto-delete timer
    # unticked would write 0 and silently disarm a live one.
    preset_flags: tuple = ()
    use_cases: tuple = ()
    # The same role's list, told against a customer POV instead of the demo estate.
    #
    # A SECOND tuple rather than a reuse of the one above, because the cards above target
    # demo pages -- /aws#instances, /vsphere, /k8s -- which a POV instance masks or 404s.
    # Every one of them resolves `masked` there, so a POV instance's catalog is complete,
    # correct and unusable. These target the POV detail page's own tabs instead.
    #
    # Their ``target`` is a FRAGMENT ONLY (``"#wired"``). The route is ``/pov/<env_id>``,
    # and the env id is not something this registry can know -- :func:`describe_pov_card`
    # joins them. A card here spelling a full path would be a route shape encoded in the
    # curation layer, which is how the two drift.
    pov_use_cases: tuple = ()


_CLOUDOPS = Persona(
    key="cloudops",
    label="Cloud Ops engineer",
    blurb="Privileged access to cloud infrastructure that is created and destroyed daily "
          "— onboarded the moment it exists, and gone when it does not.",
    section_order=("cloud", "managed", "overview", "containers", "hypervisors"),
    tile_emphasis=("aws_instances", "azure_vms", "gcp_instances", "registered_images"),
    nav_pins=("dashboard", "aws", "azure", "gcp", "images", "inventory", "jobs"),
    quick_deploy=("ec2", "azure_vm", "gce", "oci"),
    docs=("personas/cloudops",),
    preset_flags=("pra_enabled", "password_safe_enabled"),
    use_cases=(
        UseCase(
            id="cloudops-three-layers",
            title="One deploy, three PAM layers",
            summary="Deploy a cloud VM and watch Shell Jump, Password Safe onboarding and "
                    "an Entitle grant wire themselves in the same job — the machine is "
                    "under management before anyone could have logged into it.",
            target="/aws#instances",
            minutes=10,
            docs="cloud-vms",
            requires_flags=("pra_enabled", "password_safe_enabled"),
            requires_clouds=("aws", "azure", "gcp", "oci"),
        ),
        UseCase(
            id="cloudops-agentless-onboard",
            title="Onboard a cloud VM with nothing installed on it",
            summary="Rotate the admin credential on a VM that has no agent and no inbound "
                    "port open, over the cloud's own control plane — SSM on AWS, Run "
                    "Command on Azure, ssh-keys metadata on GCP.",
            target="/aws#instances",
            minutes=8,
            docs="integrations/password-safe",
            requires_flags=("password_safe_enabled",),
            requires_clouds=("aws", "azure", "gcp"),
        ),
        UseCase(
            id="cloudops-console-jit",
            title="Time-boxed access to the cloud console itself",
            summary="Grant an engineer the cloud console for two hours through Entitle, "
                    "then show the role binding disappear on its own — no standing "
                    "administrator anywhere.",
            target="/settings",
            minutes=12,
            docs="design/cloud-identity-jit",
            requires_flags=("entitle_enabled",),
        ),
        UseCase(
            id="cloudops-golden-image",
            title="Bake a golden image once, promote it everywhere",
            summary="Build a hardened image with Packer in one cloud and promote it to the "
                    "others, so every VM in every region starts from the same audited "
                    "baseline.",
            target="/images",
            minutes=15,
            docs="image-management",
            requires_clouds=("aws", "azure", "gcp", "oci"),
        ),
        UseCase(
            id="cloudops-guardrails",
            title="Guardrails: refuse the deploy, then reap it",
            summary="Admission policy turns down a non-compliant request up front, and the "
                    "auto-delete timer removes what did get built — the two halves of "
                    "not accumulating privileged infrastructure by accident.",
            target="/inventory",
            minutes=8,
            docs="policy-guardrails",
            requires_flags=("admission_control_enabled", "resource_expiry_enabled"),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-cloudops-three-layers",
            title="Three PAM layers on one lab VM",
            summary="Take one guest in this environment from “a machine with a password” "
                    "to brokered access, a vaulted account and a grant that expires — the "
                    "same three layers the demo estate shows, on the customer’s own lab.",
            target="#wired",
            minutes=12,
            docs="pov-instance",
            requires_products=("pra", "password_safe"),
        ),
        UseCase(
            id="pov-cloudops-agentless-onboard",
            title="Onboard a guest with nothing installed on it",
            summary="Bring a lab VM’s admin credential under management and rotate it "
                    "without installing an agent on the guest — the Resource Broker "
                    "inside the environment reaches it, so nothing on the machine changes.",
            target="#vms",
            minutes=10,
            docs="integrations/password-safe",
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pov-cloudops-jit-vm",
            title="Two hours on one machine, then nothing",
            summary="Grant access to a single guest for the length of a task and watch the "
                    "account disappear on its own — no standing administrator anywhere in "
                    "the environment.",
            target="#wired",
            minutes=12,
            docs="design/entitle-user-jit",
            requires_products=("entitle",),
        ),
        UseCase(
            id="pov-cloudops-reap",
            title="The whole POV disappears on the date you set",
            summary="Give this environment an expiry and show what teardown removes — the "
                    "integrations, the accounts, the jump items and the lab itself. The "
                    "answer to “what happens to our data when the evaluation ends?”",
            target="#overview",
            minutes=8,
            docs="auto-delete-timer",
        ),
    ),
)

_DEVOPS = Persona(
    key="devops",
    label="DevOps engineer",
    blurb="Pipelines and playbooks that hold no secrets — the credential is fetched at "
          "run time, scoped to the run, and gone afterwards.",
    section_order=("managed", "containers", "cloud", "overview", "hypervisors"),
    tile_emphasis=("k8s_clusters", "cloud_run_jobs", "ecs_tasks"),
    nav_pins=("dashboard", "config_mgmt", "secrets", "functions", "jobs", "inventory"),
    quick_deploy=("k8s", "gce", "ec2"),
    docs=("personas/devops",),
    preset_flags=("ansible_enabled", "password_safe_enabled", "entitle_enabled"),
    use_cases=(
        UseCase(
            id="devops-secretless-ansible",
            title="A playbook with no credential in it",
            summary="Run an Ansible playbook that looks its own credential up from Password "
                    "Safe as it executes — nothing in the repo, nothing in the "
                    "inventory file, nothing on disk when it finishes.",
            target="/config-mgmt",
            minutes=10,
            docs="integrations/ansible",
            requires_flags=("ansible_enabled", "password_safe_enabled"),
        ),
        UseCase(
            id="devops-workload-credentials",
            title="A workload that mints its own cloud credential",
            summary="Replace a long-lived access key with a short-lived one the workload "
                    "requests when it needs it — the secret nobody can leak because "
                    "nobody is holding it.",
            target="/secrets",
            minutes=12,
            docs="integrations/workload-credentials",
            requires_flags=("password_safe_enabled",),
        ),
        UseCase(
            id="devops-ephemeral-ssh",
            title="SSH accounts that exist only for the run",
            summary="A pipeline requests an account, gets it for the length of the job, and "
                    "the account is destroyed on completion — so there is no build "
                    "user to audit, rotate or forget about.",
            target="/config-mgmt",
            minutes=12,
            docs="design/entitle-user-jit",
            requires_flags=("entitle_enabled", "ansible_enabled"),
        ),
        UseCase(
            id="devops-function-secret",
            title="A serverless function that fetches its secret at cold start",
            summary="Deploy a cloud function with no environment secret, and show it pull "
                    "what it needs from the vault on first invocation.",
            target="/functions",
            minutes=10,
            docs="integrations/cloud-functions",
            requires_flags=("cloud_functions_enabled",),
        ),
        UseCase(
            id="devops-drift",
            title="What changed on this host since the last run",
            summary="Config drift tracking against the last known-good run — the "
                    "question every incident review opens with.",
            target="/config-mgmt",
            minutes=6,
            docs="config-management",
            requires_flags=("ansible_enabled",),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-devops-secretless-run",
            title="A playbook with no credential in it",
            summary="Run a playbook against a lab host that looks its own credential up as "
                    "it executes — nothing in the repo, nothing in the inventory file, "
                    "nothing on disk when it finishes.",
            target="#wired",
            minutes=12,
            docs="integrations/ansible",
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pov-devops-ephemeral-ssh",
            title="SSH accounts that exist only for the run",
            summary="A pipeline asks for an account on a lab host, gets it for the length "
                    "of the job, and the account is destroyed on completion — so there is "
                    "no build user to audit, rotate or forget about.",
            target="#wired",
            minutes=12,
            docs="design/entitle-user-jit",
            requires_products=("entitle",),
        ),
        UseCase(
            id="pov-devops-broker-path",
            title="How the credential reaches a host you cannot route to",
            summary="Walk the path from the vault to a guest on a private lab network "
                    "through the Resource Broker — the architecture question every "
                    "rotation project stalls on, answered against something running.",
            target="#overview",
            minutes=8,
            docs="design/pov-resource-broker",
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pov-devops-no-inbound",
            title="Reach the environment with no inbound firewall rule",
            summary="Every session into this lab arrives through a Gateway that only ever "
                    "dials outward — no VPN, no port opened, nothing published.",
            target="#overview",
            minutes=8,
            docs="integrations/gateways",
            requires_products=("pra",),
        ),
    ),
)

_HYPERVISOR = Persona(
    key="hypervisor",
    label="Hypervisor admin",
    blurb="Console access to the virtualisation layer without handing out the root "
          "password — injected, brokered and recorded.",
    section_order=("hypervisors", "containers", "overview", "cloud", "managed"),
    tile_emphasis=("vsphere_vms", "proxmox_vms", "nutanix_vms", "gateways"),
    nav_pins=("dashboard", "vsphere", "proxmox", "connections", "agents", "jobs"),
    quick_deploy=("proxmox_vm", "nutanix_vm"),
    docs=("personas/hypervisor",),
    preset_flags=("pra_enabled", "vsphere_enabled", "proxmox_enabled"),
    use_cases=(
        UseCase(
            id="hypervisor-web-jump-console",
            title="Web Jump into the hypervisor console",
            summary="Open the vSphere or Proxmox web console with the root credential "
                    "injected and the whole session recorded — the administrator does "
                    "the work and never learns the password.",
            target="/vsphere",
            minutes=8,
            docs="integrations/privileged-remote-access",
            requires_flags=("pra_enabled", "vsphere_enabled"),
        ),
        UseCase(
            id="hypervisor-rotate-root",
            title="Onboard and rotate the hypervisor root account",
            summary="Bring an ESXi or Proxmox root credential under management and rotate "
                    "it — the account that has historically been in a password "
                    "manager shared by six people.",
            target="/connections",
            minutes=10,
            docs="integrations/password-safe",
            requires_flags=("password_safe_enabled", "vsphere_enabled"),
        ),
        UseCase(
            id="hypervisor-shell-jump-guest",
            title="Reach a guest VM with no inbound firewall rule",
            summary="Shell Jump to a VM on an isolated management network through a "
                    "Gateway that only ever makes outbound connections.",
            target="/connections",
            minutes=8,
            docs="integrations/privileged-remote-access",
            requires_flags=("pra_enabled",),
            # /connections guards itself on any-of these six, inline in the route body.
            requires_any_flag=("proxmox_enabled", "vsphere_enabled", "hyperv_enabled",
                               "nutanix_enabled", "xcpng_enabled", "vmware_enabled"),
        ),
        UseCase(
            id="hypervisor-gateway-lifecycle",
            title="Stand up a Gateway and watch it register",
            summary="Build the gateway, see it appear in PRA, and use it — the piece "
                    "that makes every other item on this list possible.",
            target="/containers#gateways",
            minutes=12,
            docs="integrations/gateways",
            requires_flags=("pra_enabled",),
        ),
        UseCase(
            id="hypervisor-agent-discovery",
            title="Discover an on-prem estate from the outside",
            summary="An agent inside the datacentre polls outward and reports what is "
                    "there — no inbound access to the management network, no VPN to "
                    "the dashboard.",
            target="/agents",
            minutes=10,
            docs="remote-agents",
            requires_flags=("remote_agents_enabled",),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-hypervisor-web-jump-console",
            title="Open a guest console with the password injected",
            summary="Reach a management console in the lab with the credential injected and "
                    "the session recorded — the administrator does the work and never "
                    "learns the password.",
            target="#wired",
            minutes=10,
            docs="integrations/privileged-remote-access",
            requires_products=("pra", "password_safe"),
        ),
        UseCase(
            id="pov-hypervisor-rotate-root",
            title="Rotate the account six people share",
            summary="Bring a lab guest’s root or Administrator credential under management "
                    "and rotate it — the account that has historically lived in a shared "
                    "password manager.",
            target="#vms",
            minutes=10,
            docs="integrations/password-safe",
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pov-hypervisor-shell-jump-guest",
            title="Reach a guest on an isolated segment",
            summary="Shell Jump to a VM on the lab’s private network through the Gateway "
                    "inside it — the machine has no route in and never needed one.",
            target="#wired",
            minutes=8,
            docs="integrations/privileged-remote-access",
            requires_products=("pra",),
        ),
        UseCase(
            id="pov-hypervisor-gateway",
            title="Where the Gateway sits, and why that is the whole story",
            summary="The egress-only path out of this environment, told against the live "
                    "Gateway this POV installed rather than a diagram.",
            target="#overview",
            minutes=8,
            docs="integrations/gateways",
            requires_products=("pra",),
        ),
    ),
)

_ITOPS = Persona(
    key="itops",
    label="IT engineer",
    blurb="Endpoints and desktops where the user needs to get work done and nobody needs "
          "to be a local administrator to do it.",
    section_order=("hypervisors", "overview", "containers", "cloud", "managed"),
    tile_emphasis=("workstation_vms",),
    nav_pins=("dashboard", "vms", "desktops", "agents", "jobs", "inventory"),
    quick_deploy=("proxmox_vm",),
    docs=("personas/itops",),
    preset_flags=("epml_enabled", "pra_enabled"),
    use_cases=(
        UseCase(
            id="itops-epm-least-privilege",
            title="Least privilege on a Linux endpoint",
            summary="Let a user run the one command that needs elevation and nothing else "
                    "— no sudo entry, no local admin group, and a record of what ran.",
            target="/settings",
            minutes=10,
            docs="integrations/epml",
            requires_flags=("epml_enabled",),
        ),
        UseCase(
            id="itops-vdi-support",
            title="Support a user on a virtual desktop, recorded",
            summary="Stand up a virtual desktop and join the user's session to help them "
                    "— with the session recorded and no credential shared.",
            target="/desktops",
            minutes=12,
            docs="integrations/privileged-remote-access",
            requires_flags=("vdesktops_enabled", "pra_enabled"),
        ),
        UseCase(
            id="itops-rotate-local-admin",
            title="Rotate a workstation local-admin password",
            summary="The shared local administrator password every workstation has had "
                    "since imaging, brought under management and rotated per machine.",
            target="/vms",
            minutes=8,
            docs="integrations/password-safe",
            requires_flags=("password_safe_enabled", "vmware_enabled"),
        ),
        UseCase(
            id="itops-agent-power",
            title="Power a workstation on and off without RDP",
            summary="Start, stop and reboot a machine through the agent — an operator "
                    "who can manage the endpoint without any standing access to it.",
            target="/vms",
            minutes=6,
            docs="remote-agents",
            requires_flags=("remote_agents_enabled", "vmware_enabled"),
        ),
        UseCase(
            id="itops-remote-support-no-vpn",
            title="Remote support with no VPN",
            summary="Reach a workstation that is not on the corporate network at all, "
                    "through an outbound-only broker.",
            target="/vms",
            minutes=8,
            docs="integrations/privileged-remote-access",
            requires_flags=("pra_enabled", "vmware_enabled"),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-itops-recorded-support",
            title="Help someone on a lab desktop, recorded",
            summary="Join a session on a guest desktop to support whoever is using it — "
                    "recorded end to end, with no credential handed over.",
            target="#wired",
            minutes=10,
            docs="integrations/privileged-remote-access",
            requires_products=("pra",),
        ),
        UseCase(
            id="pov-itops-rotate-local-admin",
            title="Rotate a Windows guest’s local-admin password",
            summary="The shared local administrator password every machine has carried "
                    "since imaging, brought under management and rotated per machine.",
            target="#vms",
            minutes=8,
            docs="integrations/password-safe",
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pov-itops-ask-for-elevation",
            title="Let the user ask for the one thing they need",
            summary="A request, an approval, and access to exactly one machine for exactly "
                    "as long as was asked for — instead of a permanent group membership "
                    "nobody reviews.",
            target="#wired",
            minutes=12,
            docs="design/entitle-user-jit",
            requires_products=("entitle",),
        ),
        UseCase(
            id="pov-itops-share-desktop",
            title="Hand the customer their own way in",
            summary="Publish a password-protected, expiring link onto this environment’s "
                    "desktops, so the evaluation continues when nobody from your side is "
                    "on the call.",
            target="#share",
            minutes=6,
            docs="pov-instance",
        ),
    ),
)

_OT = Persona(
    key="ot",
    label="OT / ICS engineer",
    blurb="Privileged access into a plant network with no VPN and no inbound firewall hole "
          "— protocol-aware tunnels to the PLCs, and every session recorded.",
    # OT lives in the managed band (the ot_cells tile) and its cells are deployed from the
    # cloud pages, so those two lead. Every persona names all five sections even when it
    # only cares about two: a tuple that named a subset would leave "is this a reordering
    # or a whitelist?" with an observable answer, and the whole premise of this module is
    # that it can only ever be the former.
    section_order=("managed", "cloud", "overview", "containers", "hypervisors"),
    tile_emphasis=("ot_cells", "gateways"),
    # `connections` is deliberately absent: an OT cell is deployed in a cloud, not on a
    # hypervisor. Pinning six links a persona never uses is how "pinned" stops meaning
    # anything.
    nav_pins=("dashboard", "gcp", "aws", "azure", "jobs", "inventory"),
    quick_deploy=("gce", "ec2", "azure_vm"),
    # `pra_enabled` only. api/ot.py is gated on it and nothing else is required to demo a
    # cell -- a preset that ticks the union of everything a persona might touch is
    # indistinguishable from ticking everything.
    docs=("personas/ot",),
    preset_flags=("pra_enabled",),
    use_cases=(
        UseCase(
            id="ot-cell-modbus",
            title="Stand up a Modbus cell and tunnel to it",
            summary="Deploy a simulated plant cell into a subnet with no public IP and no "
                    "egress, then read its live holding registers on TCP 502 through a "
                    "brokered tunnel — no VPN, no inbound rule.",
            target="/gcp#ot",
            minutes=15,
            docs="cloud-ot",
            requires_flags=("pra_enabled",),
            requires_clouds=("gcp", "aws", "azure"),
        ),
        UseCase(
            id="ot-multi-protocol",
            title="One cell, four protocols",
            summary="Serve the same four live process values over Modbus, OPC UA, S7comm "
                    "and EtherNet/IP, each as its own tunnel a policy can grant "
                    "separately — the answer to “we are a Siemens shop” and "
                    "“we are a Rockwell shop” in one demo.",
            target="/gcp#ot",
            minutes=10,
            docs="cloud-ot",
            requires_flags=("pra_enabled",),
            requires_clouds=("gcp", "aws", "azure"),
        ),
        UseCase(
            id="ot-vendor-jit",
            title="Time-bound vendor access to one cell",
            summary="Give a third-party integrator two hours on a single cell, then watch "
                    "the grant expire and the tunnel close by itself — the flagship "
                    "OT story, because vendor access is how plants get compromised.",
            target="/gcp#ot",
            minutes=15,
            docs="design/entitle-user-jit",
            requires_flags=("pra_enabled", "entitle_enabled"),
            requires_clouds=("gcp", "aws", "azure"),
        ),
        UseCase(
            id="ot-credential-checkout",
            title="Check out the cell's admin credential in PRA",
            summary="The cell's account is onboarded in Password Safe, mirrored into the "
                    "PRA vault and rotated once, so a rep injects a real credential "
                    "without ever seeing it.",
            target="/gcp#ot",
            minutes=12,
            docs="cloud-ot",
            requires_flags=("pra_enabled", "password_safe_enabled"),
            requires_clouds=("gcp", "aws", "azure"),
        ),
        UseCase(
            id="ot-jumpoint-egress",
            title="Where the Gateway sits, and why that is the whole story",
            summary="Walk the egress-only path from the cell subnet out to the appliance "
                    "— the architecture slide, told against a live gateway instead of "
                    "a diagram.",
            target="/containers#gateways",
            minutes=8,
            docs="integrations/gateways",
            requires_flags=("pra_enabled",),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-ot-tunnel-to-device",
            title="Tunnel to a device with no VPN and no inbound rule",
            summary="Reach a machine on the lab’s process network over its own protocol "
                    "through a brokered tunnel — the access path that does not require "
                    "opening the plant network.",
            target="#wired",
            minutes=12,
            docs="integrations/privileged-remote-access",
            requires_products=("pra",),
        ),
        UseCase(
            id="pov-ot-vendor-jit",
            title="Two hours for the integrator, then the tunnel closes",
            summary="Give a third party access to one machine for a bounded window and "
                    "watch the grant expire and the session end by itself — the flagship "
                    "story, because vendor access is how plants get compromised.",
            target="#wired",
            minutes=15,
            docs="design/entitle-user-jit",
            requires_products=("pra", "entitle"),
        ),
        UseCase(
            id="pov-ot-credential-injection",
            title="The vendor uses a credential they never see",
            summary="The machine’s account is vaulted and injected at session launch, so "
                    "a third party does real work without the password ever being on "
                    "their screen or in their notes.",
            target="#wired",
            minutes=12,
            docs="integrations/password-safe",
            requires_products=("pra", "password_safe"),
        ),
        UseCase(
            id="pov-ot-egress-only",
            title="Nothing dials in — the architecture slide, live",
            summary="Trace the outbound-only path from the lab’s private segment to the "
                    "appliance, against the Gateway this POV is actually using.",
            target="#overview",
            minutes=8,
            docs="integrations/gateways",
            requires_products=("pra",),
        ),
    ),
)

_DBA = Persona(
    key="dba",
    label="DBA / data platform",
    blurb="Database access that is granted per request and expires — for the accounts "
          "that historically had a shared password and no expiry at all.",
    section_order=("managed", "cloud", "overview", "containers", "hypervisors"),
    tile_emphasis=("cloud_databases",),
    nav_pins=("dashboard", "databases", "secrets", "jobs", "inventory"),
    quick_deploy=("database",),
    docs=("personas/dba",),
    preset_flags=("cloud_database_enabled", "password_safe_enabled", "entitle_enabled"),
    use_cases=(
        UseCase(
            id="dba-provision-and-onboard",
            title="Provision a managed database, under management from birth",
            summary="Stand up Postgres, MySQL or SQL Server and have its admin account "
                    "onboarded and rotated as part of the same job — no window where "
                    "the database exists with an unmanaged password.",
            target="/databases",
            minutes=15,
            docs="databases",
            requires_flags=("cloud_database_enabled", "password_safe_enabled"),
        ),
        UseCase(
            id="dba-jit-grant",
            title="Request, approve, grant, expire",
            summary="An analyst asks for read access to one schema, an approver says yes, "
                    "the grant appears in the database and then removes itself — the "
                    "full loop, in about ten minutes.",
            target="/databases",
            minutes=12,
            docs="design/entitle-resource-registration",
            requires_flags=("cloud_database_enabled", "entitle_enabled"),
        ),
        UseCase(
            id="dba-rotate-no-outage",
            title="Rotate a database admin credential with nothing breaking",
            summary="Rotate the account an application depends on, and show the "
                    "application keep working — the objection that stops most "
                    "rotation projects.",
            target="/databases",
            minutes=10,
            docs="integrations/password-safe",
            requires_flags=("cloud_database_enabled", "password_safe_enabled"),
        ),
        UseCase(
            id="dba-private-db-tunnel",
            title="Reach a private database with no public endpoint",
            summary="Connect a normal SQL client to a database that has no internet-facing "
                    "listener, through a brokered tunnel.",
            target="/databases",
            minutes=10,
            docs="databases",
            requires_flags=("cloud_database_enabled", "pra_enabled"),
        ),
        UseCase(
            id="dba-service-account-token",
            title="Rotate the token a service uses to reach the database",
            summary="The non-human identity nobody rotates because nobody is sure what "
                    "would break — rotated on a schedule, with the consumer picking "
                    "up the new value.",
            target="/k8s",
            minutes=12,
            docs="design/k8s-sa-token-rotation",
            requires_flags=("k8s_management_enabled", "password_safe_enabled"),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-dba-onboard-db-account",
            title="Bring the database’s admin account under management",
            summary="Onboard the account the lab’s database runs on and rotate it — the "
                    "credential that has never been changed because nobody was sure what "
                    "would break.",
            target="#wired",
            minutes=12,
            docs="integrations/password-safe",
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pov-dba-jit-grant",
            title="Request, approve, grant, expire",
            summary="An analyst asks for read access, an approver says yes, the grant "
                    "appears and then removes itself — the full loop, on the customer’s "
                    "own data, in about ten minutes.",
            target="#wired",
            minutes=12,
            docs="design/entitle-resource-registration",
            requires_products=("entitle",),
        ),
        UseCase(
            id="pov-dba-private-tunnel",
            title="A normal SQL client, a database with no way in",
            summary="Connect the tool the DBA already uses to a database on the lab’s "
                    "private network, through a brokered tunnel rather than a bastion "
                    "nobody patches.",
            target="#wired",
            minutes=10,
            docs="integrations/privileged-remote-access",
            requires_products=("pra",),
        ),
        UseCase(
            id="pov-dba-rotate-no-outage",
            title="Rotate it with the application still running",
            summary="Rotate an account something depends on and show the dependent keep "
                    "working — the objection that stops most rotation projects, met on "
                    "the customer’s own stack.",
            target="#vms",
            minutes=12,
            docs="integrations/password-safe",
            requires_products=("password_safe",),
        ),
    ),
)

_SECURITY = Persona(
    key="security",
    label="Security / IAM analyst",
    blurb="The oversight lens: who has access to what right now, what they did with it, "
          "and what should have expired already.",
    section_order=("overview", "managed", "cloud", "containers", "hypervisors"),
    tile_emphasis=("active_jobs", "deployed_resources"),
    nav_pins=("dashboard", "inventory", "jobs", "secrets", "users", "groups"),
    quick_deploy=(),
    docs=("personas/security",),
    preset_flags=("admission_control_enabled",),
    use_cases=(
        UseCase(
            id="security-who-has-access",
            title="Who has access to what, right now",
            summary="Every privileged path into the estate in one view, with the job trail "
                    "that created each one — the question an auditor opens with and "
                    "most organisations answer with a spreadsheet.",
            target="/inventory",
            minutes=8,
        ),
        UseCase(
            id="security-secret-scanning",
            title="Find the credentials somebody committed",
            summary="Scan the estate for secrets sitting in configuration, and show what "
                    "turns up — it always turns something up.",
            target="/secrets",
            minutes=8,
            docs="secrets-management",
        ),
        UseCase(
            id="security-admission-control",
            title="Refuse the deploy that would have been a finding",
            summary="Policy turns down a non-compliant request before anything is built, "
                    "with a reason the requester can act on — prevention rather than "
                    "a quarterly report.",
            target="/inventory",
            minutes=10,
            docs="policy-guardrails",
            requires_flags=("admission_control_enabled",),
        ),
        UseCase(
            id="security-event-stream",
            title="Push the events somewhere that gets read",
            summary="Stream privileged-access events to Slack, Teams or a signed webhook, "
                    "so the record lives in the tool the team already watches.",
            target="/settings",
            minutes=8,
            docs="notifications",
            requires_flags=("notifications_enabled",),
        ),
        UseCase(
            id="security-no-orphans",
            title="Nothing privileged outlives its purpose",
            summary="Every resource carries an expiry, and the ones that pass it are "
                    "removed — the antidote to an estate nobody can account for.",
            target="/inventory",
            minutes=6,
            docs="auto-delete-timer",
            requires_flags=("resource_expiry_enabled",),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-security-who-has-access",
            title="Who has access to this environment, right now",
            summary="Every privileged path into the POV in one view, with what created "
                    "each one — the question an auditor opens with, answered without a "
                    "spreadsheet.",
            target="#wired",
            minutes=8,
            docs="pov-instance",
        ),
        UseCase(
            id="pov-security-session-record",
            title="Every session recorded, and where the recording lives",
            summary="Play back a session someone ran during this evaluation, and say "
                    "plainly where the recording is held and who can reach it.",
            target="#wired",
            minutes=10,
            docs="integrations/privileged-remote-access",
            requires_products=("pra",),
        ),
        UseCase(
            id="pov-security-no-standing-access",
            title="Prove nobody holds standing access",
            summary="Show the grant list empty between requests — access exists only while "
                    "somebody asked for it, which is a different claim from “access is "
                    "logged”.",
            target="#wired",
            minutes=10,
            docs="design/entitle-user-jit",
            requires_products=("entitle",),
        ),
        UseCase(
            id="pov-security-teardown-proof",
            title="What teardown removes, in the order it removes it",
            summary="Walk the destroy path — accessors and links first, then the customer’s "
                    "own appliance objects, then the lab. The evidence that an evaluation "
                    "leaves nothing behind.",
            target="#overview",
            minutes=8,
            docs="pov-instance",
        ),
    ),
)

_SRE = Persona(
    key="sre",
    label="Platform / SRE",
    blurb="Kubernetes and the platform layer, where access is bound to the person who "
          "asked for it and lasts as long as the incident.",
    section_order=("managed", "containers", "overview", "cloud", "hypervisors"),
    tile_emphasis=("k8s_clusters", "rancher_nodes", "portainer_endpoints"),
    nav_pins=("dashboard", "k8s", "containers", "jobs", "inventory"),
    quick_deploy=("k8s",),
    docs=("personas/sre",),
    preset_flags=("k8s_management_enabled", "entitle_enabled", "pra_enabled"),
    use_cases=(
        UseCase(
            id="sre-k8s-jit-rbac",
            title="kubectl that expires",
            summary="An engineer requests cluster access for the length of an incident and "
                    "gets an RBAC binding named after them, which removes itself "
                    "afterwards — no permanent cluster-admin group.",
            target="/k8s",
            minutes=12,
            docs="integrations/entra-k8s-federation",
            requires_flags=("k8s_management_enabled", "entitle_enabled"),
        ),
        UseCase(
            id="sre-private-cluster-api",
            title="Reach a private cluster API server",
            summary="Run kubectl against a cluster with no public endpoint, through a "
                    "brokered tunnel rather than a bastion nobody patches.",
            target="/k8s",
            minutes=10,
            docs="kubernetes",
            requires_flags=("k8s_management_enabled", "pra_enabled"),
        ),
        UseCase(
            id="sre-sa-token-rotation",
            title="Rotate a ServiceAccount token",
            summary="The long-lived token in a CI system or a sidecar, rotated on a "
                    "schedule with the consumer picking up the new value.",
            target="/k8s",
            minutes=12,
            docs="design/k8s-sa-token-rotation",
            requires_flags=("k8s_management_enabled", "password_safe_enabled"),
        ),
        UseCase(
            id="sre-federated-identity",
            title="One group, every cluster",
            summary="Federate cluster access to the corporate directory, so joining a team "
                    "grants the right access everywhere and leaving revokes it — "
                    "instead of per-cluster identity nobody deprovisions.",
            target="/k8s",
            minutes=12,
            docs="integrations/entra-k8s-federation",
            requires_flags=("k8s_management_enabled",),
        ),
        UseCase(
            id="sre-container-platform-node",
            title="A container platform with a vaulted admin",
            summary="Stand up a Portainer or Rancher node and reach its UI by Web Jump "
                    "with the admin credential injected — the platform console that "
                    "usually has a shared login.",
            target="/containers#rancher",
            minutes=12,
            docs="integrations/rancher",
            requires_flags=("k8s_management_enabled", "pra_enabled"),
        ),
    ),
    pov_use_cases=(
        UseCase(
            id="pov-sre-private-api",
            title="Reach a private management API",
            summary="Point a normal client at a service in the lab that has no public "
                    "endpoint, through a brokered tunnel instead of a jump box.",
            target="#wired",
            minutes=10,
            docs="integrations/privileged-remote-access",
            requires_products=("pra",),
        ),
        UseCase(
            id="pov-sre-token-rotation",
            title="Rotate the token a service is using",
            summary="The long-lived token in a CI system or a sidecar, rotated with the "
                    "consumer picking up the new value — the non-human identity nobody "
                    "rotates because nobody is sure what would break.",
            target="#wired",
            minutes=12,
            docs="integrations/password-safe",
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pov-sre-incident-access",
            title="Access that lasts as long as the incident",
            summary="An engineer requests elevated access for the length of an incident "
                    "and it removes itself afterwards — no permanent break-glass group "
                    "that quietly becomes everyone’s baseline.",
            target="#wired",
            minutes=12,
            docs="design/entitle-user-jit",
            requires_products=("entitle",),
        ),
        UseCase(
            id="pov-sre-inside-components",
            title="The two things that run inside the customer’s network",
            summary="The Gateway and the Resource Broker, what each one talks to, and why "
                    "neither needs anything dialling in — the review a platform team will "
                    "ask for before any of this ships.",
            target="#overview",
            minutes=10,
            docs="design/pov-resource-broker",
        ),
    ),
)


_PERSONAS = {p.key: p for p in (
    _CLOUDOPS, _DEVOPS, _HYPERVISOR, _ITOPS, _OT, _DBA, _SECURITY, _SRE,
)}


# ── resolution ───────────────────────────────────────────────────────────────

def default_persona() -> str:
    """The instance-wide default the setup wizard writes.

    Separate from :func:`resolve` because the wizard and the catalog need it without a
    Request in hand. Unknown values resolve to :data:`NEUTRAL` rather than raising, for
    the same reason ``feature_flags.install_profile`` falls back to ``demo``: this is read
    on the request path, and a typo in one config row must degrade to today's behaviour.
    """
    raw = (config_service.get("default_persona") or "").strip().lower()
    return raw if raw in VALID_PERSONAS else NEUTRAL


def resolve(request=None) -> tuple:
    """The active persona and where it came from, as ``(key, source)``.

    Precedence: ``?persona=`` (this render only, persists nothing) > the ``persona``
    cookie (what the nav lens writes) > the instance default > neutral.

    ``source`` is returned rather than discarded because a shared ``?persona=`` link means
    "why is my nav in a strange order" has three possible causes. The lens control shows
    the answer, so the question never becomes a support ticket.

    **The return value is always a member of :data:`VALID_PERSONAS` or :data:`NEUTRAL` --
    never a pass-through of the query string.** ``?persona=`` is reflected into HTML, and
    ``api/docs_pages`` already carries an escaping fix for exactly this shape.
    """
    if request is not None:
        try:
            raw = (request.query_params.get("persona") or "").strip().lower()
        except Exception:  # noqa: BLE001 - a Request-like object without query_params
            raw = ""
        if raw in VALID_PERSONAS:
            return raw, "url"
        try:
            raw = (request.cookies.get("persona") or "").strip().lower()
        except Exception:  # noqa: BLE001
            raw = ""
        if raw in VALID_PERSONAS:
            return raw, "cookie"

    key = default_persona()
    return (key, "default") if key else (NEUTRAL, "none")


def get(key: str):
    """The :class:`Persona` for ``key``, or ``None`` for neutral/unknown."""
    return _PERSONAS.get((key or "").strip().lower())


def all_personas() -> tuple:
    """Every persona, in declaration order. The source of truth for the wizard picker."""
    return tuple(_PERSONAS[k] for k in VALID_PERSONAS)


def options() -> list:
    """``[{key, label}, ...]`` for the lens selector, neutral first.

    Rides along in :func:`describe` so the control that switches persona needs no second
    request, and — the reason that matters — so no template ever hard-codes a persona key
    just to render its own picker. Keys and labels come from here or they drift.

    Neutral is first and it is a real option: "no focus" has to be as easy to get back to
    as it was to leave, or the persona has effectively subtracted the plain dashboard.
    """
    return ([{"key": NEUTRAL, "label": "No focus — show everything"}]
            + [{"key": p.key, "label": p.label} for p in all_personas()])


# ── card readiness ───────────────────────────────────────────────────────────

def _any_label(flags: tuple) -> str:
    """"any hypervisor integration" reads better than six names joined by "or"."""
    names = [_FLAG_LABELS.get(f, f) for f in flags]
    if len(names) > 3:
        return f"any of {len(names)} integrations ({names[0]}, {names[1]}, …)"
    return " or ".join(names)


def _card_state(card: UseCase) -> tuple:
    """``(state, needs)`` for one card. State is ``ready``/``needs_flag``/``masked``.

    Three states rather than two, because the two unready reasons are not the same fact.
    ``needs_flag`` is *actionable* -- the operator turns the integration on -- so the card
    keeps a link to Settings. ``masked`` is not: ``api/setup.patch_feature_config`` would
    refuse the enable with a 409 naming the profile, so offering a Settings link there
    sends the operator to a switch that cannot move.

    Everything resolves through ``feature_flags`` -- the one reader -- never by
    re-deriving from ``config_service``, which is how a card would end up linking to a
    page that 404s.
    """
    masked = []
    missing = []

    for flag in card.requires_flags:
        if feature_flags.profile_masks(flag):
            masked.append(_FLAG_LABELS.get(flag, flag))
        elif not feature_flags.enabled(flag):
            missing.append(_FLAG_LABELS.get(flag, flag))

    if card.requires_any_flag:
        # Masked only when the profile refuses EVERY option -- one survivor means the page
        # is still reachable, so the card is unready at worst rather than unavailable.
        if all(feature_flags.profile_masks(f) for f in card.requires_any_flag):
            masked.append(_any_label(card.requires_any_flag))
        elif not any(feature_flags.enabled(f) for f in card.requires_any_flag):
            missing.append(_any_label(card.requires_any_flag))

    if card.requires_clouds:
        # The cloud consoles are a profile-owned PAGE GROUP, not a flag: on a POV instance
        # they 404 and their nav links are gone, so a card targeting one is masked rather
        # than merely unready. This is the check that stops a `cloudops` card from being a
        # link to a 404 on the instance that does customer work.
        if not feature_flags.profile_page_allowed("cloud_pages"):
            masked.append("the cloud consoles")
        elif not any(feature_flags.cloud_configured(c) for c in card.requires_clouds):
            names = " or ".join(_CLOUD_LABELS.get(c, c) for c in card.requires_clouds)
            missing.append(f"credentials for {names}")

    if masked:
        return "masked", tuple(masked)
    if missing:
        return "needs_flag", tuple(missing)
    return "ready", ()


def describe_card(card: UseCase) -> dict:
    """One card as the API serves it. A masked card carries **no href at all.**"""
    state, needs = _card_state(card)
    return {
        "id": card.id,
        "title": card.title,
        "summary": card.summary,
        # Withheld rather than dimmed in CSS: a masked card's target is a page this
        # instance's profile 404s, and a link the client merely styles as inert is one
        # stray middle-click from proving it.
        "target": card.target if state != "masked" else "",
        "minutes": card.minutes,
        "docs": f"/docs/{card.docs}" if card.docs else "",
        "state": state,
        "needs": list(needs),
        # `masked` deliberately gets no Settings link -- see _card_state.
        "settings_link": "/settings" if state == "needs_flag" else "",
    }


# ── POV card readiness ───────────────────────────────────────────────────────

def _pov_card_state(card: UseCase, products: dict) -> tuple:
    """``(state, needs)`` for one POV card, against one POV's product mix.

    Three states, and deliberately **not** the three words :func:`_card_state` uses:

      ``ready``         this POV has every product the card names, and each one's artifact
                        actually exists on it
      ``needs_wiring``  the tenant is set and the artifact is not -- actionable, so the
                        card keeps a link to the tab with the button on it
      ``out_of_scope``  this POV has no tenant for that product at all

    ``out_of_scope`` is NOT ``masked``. Masked means this INSTANCE's profile refuses the
    feature and no operator can change that. Out of scope means this CUSTOMER's POV was
    deliberately not wired into that product -- a Password-Safe-only evaluation is a normal,
    correct shape, and borrowing the word "masked" for it would make the whole page read as
    a misconfiguration.

    Absence beats unwired when a card names two products: a card needing PRA and Password
    Safe on a POV with no PRA tenant cannot be run at all, so reporting "run the wire-up"
    would send an operator to a button that will skip the half they came for.
    """
    absent = []
    unwired = []

    for product in card.requires_products:
        if not products.get(product):
            absent.append(_PRODUCT_LABELS.get(product, product))
        elif not products.get(_PRODUCT_ARTIFACT.get(product, ""), False):
            unwired.append(_PRODUCT_REMEDY.get(product,
                                               _PRODUCT_LABELS.get(product, product)))

    if absent:
        return "out_of_scope", tuple(absent)
    if unwired:
        return "needs_wiring", tuple(unwired)
    return "ready", ()


def describe_pov_card(card: UseCase, env_id: str, products: dict) -> dict:
    """One POV card as the API serves it. An out-of-scope card carries **no href at all.**

    The registry holds a fragment and this joins it to the POV -- so ``/pov/<id>`` is
    spelled in exactly one place in this module, and a card can never carry a path to an
    environment that is not the one being described.
    """
    state, needs = _pov_card_state(card, products)
    return {
        "id": card.id,
        "title": card.title,
        "summary": card.summary,
        # Withheld rather than dimmed, for the same reason describe_card withholds a masked
        # target: a link the client only styles as inert is one stray middle-click from
        # taking somebody to a tab that has nothing on it for this POV.
        "target": f"/pov/{env_id}{card.target}" if state != "out_of_scope" else "",
        "minutes": card.minutes,
        "docs": f"/docs/{card.docs}" if card.docs else "",
        "state": state,
        "needs": list(needs),
        # The products this card is about, so the page can group or filter by product
        # without re-deriving it from the copy.
        "products": list(card.requires_products),
        # The POV equivalent of `settings_link`, and it obeys the same rule: only the
        # ACTIONABLE state gets one. An out-of-scope card has nowhere useful to send an
        # operator -- the fix is a tenant on the POV row, which is a decision about the
        # evaluation rather than a button on a tab.
        "action_link": (f"/pov/{env_id}{_POV_ACTION_TAB}"
                        if state == "needs_wiring" else ""),
    }


def describe_pov(key: str, env_id: str, products: dict) -> dict:
    """One persona's POV cards for one POV. Unknown key yields an empty group, never None."""
    persona = get(key)
    if persona is None:
        return {"persona": NEUTRAL, "label": "", "blurb": "", "docs": [], "use_cases": []}
    return {
        "persona": persona.key,
        "label": persona.label,
        "blurb": persona.blurb,
        "docs": [f"/docs/{d}" for d in persona.docs],
        "use_cases": [describe_pov_card(c, env_id, products)
                      for c in persona.pov_use_cases],
    }


def pov_catalog(env_id: str, products: dict) -> list:
    """Every persona's POV cards for one POV, in declaration order.

    Complete for every product mix. A POV wired into one product still sees all eight
    groups and every card in them -- the mix decides each card's STATE and nothing else,
    which is the same promise the persona axis makes one layer up.
    """
    return [describe_pov(p.key, env_id, products) for p in all_personas()]


def find_pov_card(card_id: str) -> tuple:
    """``(persona_key, UseCase)`` for a POV card id, or ``("", None)``.

    The registry is the allowlist for anything that WRITES a card id. Without this a
    progress table accepts any string a client sends and becomes a free-text store nobody
    can render -- and the rows outlive the mistake, because progress is deliberately never
    deleted on a copy edit.
    """
    target = (card_id or "").strip()
    if not target:
        return "", None
    for persona in all_personas():
        for card in persona.pov_use_cases:
            if card.id == target:
                return persona.key, card
    return "", None


def describe(key: str, source: str = "none") -> dict:
    """The payload ``GET /api/persona`` serves.

    Ordering hints ship as data so the templates never learn a persona key. The moment
    ``dashboard.html`` hard-codes "cloudops leads with cloud", this table and the page
    disagree and the wizard's preview lies about what picking a persona will do.
    """
    persona = get(key)
    if persona is None:
        return {
            "persona": NEUTRAL,
            "label": "",
            "blurb": "",
            "source": source,
            "install_profile": feature_flags.install_profile(),
            "section_order": [],
            "tile_emphasis": [],
            "nav_pins": [],
            "quick_deploy": [],
            "preset_flags": [],
            "docs": [],
            "use_cases": [],
            "options": options(),
        }
    return {
        "persona": persona.key,
        "label": persona.label,
        "blurb": persona.blurb,
        "source": source,
        "install_profile": feature_flags.install_profile(),
        "section_order": list(persona.section_order),
        "tile_emphasis": list(persona.tile_emphasis),
        "nav_pins": list(persona.nav_pins),
        "quick_deploy": list(persona.quick_deploy),
        "preset_flags": list(persona.preset_flags),
        "docs": [f"/docs/{d}" for d in persona.docs],
        "use_cases": [describe_card(c) for c in persona.use_cases],
        "options": options(),
    }


def catalog() -> list:
    """Every persona with its cards, for the wizard picker and ``/use-cases``.

    Neutral shows this whole list, so choosing no persona is not a subtraction either.
    """
    return [describe(p.key) for p in all_personas()]
