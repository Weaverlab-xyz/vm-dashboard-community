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

**This module is the DEMO profile's, and only the demo profile's.** A persona is a
*presenting* decision -- which role's story an SE leads an estate demo with, on a day they
choose. A POV instance is not presenting: it is one customer's evaluation of the products
on their own row, its dashboard leads with that POV, and its ``/use-cases`` page shows that
POV's checklist. There is no role whose material could lead, so :func:`applies` answers
false there and all five resolvers return :data:`NEUTRAL`. The role pages live under
``docs/profiles/demo/personas`` and always did.

Note which axis does the subtracting in that sentence. The PROFILE removes the pickers,
exactly as it removes the four cloud consoles; the persona layer has gained no power. Every
page an estate instance reaches, a POV instance still reaches -- in shipped order, which is
what neutral has always meant.

The POV checklist is a separate registry for that reason: ``services/pov_cards`` groups its
cards by the PRODUCT each one proves, because a POV is bought and scoped by product. Those
cards used to live here in eight ``Persona.pov_use_cases`` tuples and rendered under role
headings, which sorted a customer's remaining work by an axis belonging to the other
install profile. Nothing in this module knows about them now, and the only thing the two
share is :class:`UseCase` -- a card is a card.

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
    "workload_credentials_enabled": "Workload Credentials",
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
    "cert_lab_enabled": "Certificate Lab",
    "spire_lab_enabled": "SPIRE Lab",
    "k8s_management_enabled": "Kubernetes",
    "portainer_enabled": "Portainer",
    "cost_explorer_enabled": "Cost reporting",
    "remote_agents_enabled": "Remote agents",
    "admission_control_enabled": "Admission control",
    "resource_expiry_enabled": "Auto-delete timer",
    "notifications_enabled": "Notifications",
}

_CLOUD_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI"}


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
    # ``pov_cards.POV_PRODUCTS`` -- which are NOT flags: see the note there. A card with
    # none is always in scope, which is how the oversight cards stay useful on a POV wired
    # into a single product, and it also decides the card's GROUP (``pov_cards.group_of``).
    #
    # Declared here rather than on a POV-only subclass because a card is a card: this is the
    # one thing the two registries share, and `pov_cards` imports the class rather than the
    # module's resolvers. No card in THIS file ever sets it -- a demo card never sets this
    # and a POV card never sets the three fields above, because the two lists resolve
    # through different readers and a card carrying both would be answering a question
    # nobody asked it.
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
    # Estate cards, and the only kind. There WAS a second `pov_use_cases` tuple here,
    # holding the same role's story told against a customer POV; those 32 cards now live in
    # `services/pov_cards`, grouped by the product each one proves. Do not add a POV tuple
    # back: the cards here target demo pages -- /aws#instances, /vsphere, /k8s -- which a
    # POV instance masks or 404s, and this whole module answers neutral there anyway.
    use_cases: tuple = ()


_CLOUDOPS = Persona(
    key="cloudops",
    label="Cloud Ops engineer",
    blurb="Privileged access to cloud infrastructure that is created and destroyed daily "
          "— onboarded the moment it exists, and gone when it does not.",
    section_order=("cloud", "managed", "overview", "containers", "hypervisors"),
    tile_emphasis=("aws_instances", "azure_vms", "gcp_instances", "registered_images"),
    nav_pins=("dashboard", "aws", "azure", "gcp", "images", "inventory", "jobs"),
    quick_deploy=("ec2", "azure_vm", "gce", "oci"),
    docs=("profiles/demo/personas/cloudops",),
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
    docs=("profiles/demo/personas/devops",),
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
            id="devops-pipeline-certificate",
            title="A pipeline that authenticates with a certificate nobody has to renew",
            summary="Issue a short-lived client certificate under approval, have a build "
                    "retrieve it over the API, and prove it actually completes an mTLS "
                    "handshake — then rotate and show the build never notices.",
            target="/workload-lab#certificates",
            minutes=15,
            docs="integrations/certificates",
            requires_flags=("cert_lab_enabled",),
        ),
        UseCase(
            id="devops-workload-identity-governed",
            title="Every workload identity in a trust domain, inventoried and governed",
            summary="Stand up a SPIRE trust domain, discover each registration entry as "
                    "a managed account, and show which ones carry control-plane rights "
                    "nobody meant to grant — then mint an audience-scoped JWT-SVID for "
                    "the one consumer that cannot run an agent.",
            target="/workload-lab#spire",
            minutes=20,
            docs="integrations/spiffe",
            requires_flags=("spire_lab_enabled",),
        ),
        UseCase(
            id="devops-workload-cluster-token",
            title="A build server that reaches the cluster without a kubeconfig",
            summary="A CI build fetches a short-lived ServiceAccount token from Password "
                    "Safe per run, deploys into one namespace, and is refused in every "
                    "other — instead of a long-lived kubeconfig nobody can revoke or "
                    "prove anyone read. The answer for a MANAGED cluster, where the "
                    "SPIFFE route above cannot be configured at all.",
            target="/workload-lab#kubernetes",
            minutes=10,
            docs="integrations/workload-kubernetes",
            # THREE flags, and `spire_lab_enabled` is the one that looks wrong. The tab
            # needs k8s + Password Safe, but /workload-lab itself is gated on the derived
            # `workload_lab_enabled` — either PREVIEW flag — so a card that named only the
            # first two would render as a link to a page that 404s. That is the drift
            # tests/test_personas.py::test_a_card_declares_the_flag_its_target_page_is_gated_on
            # exists to catch, and it caught this. Paired with the SPIRE flag rather than
            # the certificate one because this story IS the managed-cluster counterpart to
            # the entry above, and a visitor reading one wants the other.
            requires_flags=("k8s_management_enabled", "password_safe_enabled",
                            "spire_lab_enabled"),
        ),
        UseCase(
            id="devops-dynamic-cloud-credential",
            title="A build that reaches AWS with no access key anywhere",
            summary="A pipeline mints a short-lived cloud credential per run instead of "
                    "holding an access key in a CI secret store — then watch the same call "
                    "be refused once the lease expires, with nothing revoked and nobody "
                    "rotating anything.",
            target="/workload-lab#cloud",
            minutes=10,
            docs="integrations/workload-cloud",
            # The Workload Credentials integration's own flag plus a PREVIEW flag, for the
            # reason test_personas.py enforces: /workload-lab is gated on the derived
            # `workload_lab_enabled`, so a card naming only the first would link to a page
            # that 404s. Paired with the SPIRE flag because that is the story an operator
            # reading this one wants next — it is the path that fixes the axis this one
            # cannot (anyone who can mint is the workload).
            requires_flags=("workload_credentials_enabled", "spire_lab_enabled"),
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
)

_HYPERVISOR = Persona(
    key="hypervisor",
    label="Hypervisor admin",
    blurb="Console access to the virtualisation layer without handing out the root "
          "password — injected, brokered and recorded.",
    section_order=("hypervisors", "containers", "overview", "cloud", "managed"),
    tile_emphasis=("vsphere_vms", "proxmox_vms", "nutanix_vms", "gateways"),
    # `connections` and `agents` are the same PAGE now — two tabs of /agents — and both
    # are pinned on purpose rather than by oversight: the nav renders whichever of the two
    # links this viewer gets (Agents is admin-only, Connections is the row that survives
    # for everyone else and for an install with no agent at all), so pinning both is what
    # puts a live link in the shortlist either way. Collapsing them to one would leave the
    # other role's only route to the page unpinned. See _nav_links.html.
    nav_pins=("dashboard", "vsphere", "proxmox", "connections", "agents", "jobs"),
    quick_deploy=("proxmox_vm", "nutanix_vm"),
    docs=("profiles/demo/personas/hypervisor",),
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
    docs=("profiles/demo/personas/itops",),
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
    docs=("profiles/demo/personas/ot",),
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
            docs="profiles/demo/ot-demo-cell",
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
            docs="profiles/demo/ot-demo-cell",
            requires_flags=("pra_enabled",),
            requires_clouds=("gcp", "aws", "azure"),
        ),
        UseCase(
            id="ot-kubectl-brokered",
            title="kubectl into the plant, through PRA",
            summary="The cell runs its simulators on KubeSolo, so the plant IPC really is "
                    "a single-node Kubernetes host — brokered on a tunnel of its own, so "
                    "a vendor can be granted the PLC and not the cluster, and every "
                    "session is recorded.",
            target="/gcp#ot",
            minutes=10,
            docs="kubesolo",
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
            docs="profiles/demo/ot-demo-cell",
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
    docs=("profiles/demo/personas/dba",),
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
)

_SECURITY = Persona(
    key="security",
    label="Security / IAM analyst",
    blurb="The oversight lens: who has access to what right now, what they did with it, "
          "and what should have expired already.",
    section_order=("overview", "managed", "cloud", "containers", "hypervisors"),
    tile_emphasis=("active_jobs", "deployed_resources"),
    nav_pins=("dashboard", "inventory", "jobs", "secrets", "rbac"),
    quick_deploy=(),
    docs=("profiles/demo/personas/security",),
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
    docs=("profiles/demo/personas/sre",),
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
)


_PERSONAS = {p.key: p for p in (
    _CLOUDOPS, _DEVOPS, _HYPERVISOR, _ITOPS, _OT, _DBA, _SECURITY, _SRE,
)}


# ── resolution ───────────────────────────────────────────────────────────────

def applies() -> bool:
    """Whether the focus axis exists on this instance at all.

    A persona is a *presenting* decision: which role's story an SE leads an estate demo
    with. A POV instance is not presenting. It is one customer's evaluation of the products
    on their own row, its dashboard leads with that POV, and its ``/use-cases`` page shows
    that POV's checklist rather than the role catalog -- so there is no role whose material
    could lead, and the eight labels in a picker were offering a choice with no outcome.
    The docs have always said so: the role pages live under ``docs/profiles/demo/personas``.

    Note which direction this reads. It asks ``feature_flags`` for the profile, which is the
    permitted direction (see the module docstring); ``feature_flags`` still knows nothing
    about personas, and two tests hold it to that. And it does NOT make the persona layer a
    gate: the profile is doing the subtracting here, exactly as it does for the cloud
    consoles. Every page an estate instance reaches, a POV instance still reaches -- it
    simply reaches them in shipped order, which is what neutral has always meant.

    The four resolvers below consult this rather than their callers doing it, so a focus
    cannot arrive from a cookie, a URL, a user row, an OIDC group or the instance default on
    an instance that has no focus to give.
    """
    return feature_flags.install_profile() == "demo"


def default_persona() -> str:
    """The instance-wide default the setup wizard writes.

    Separate from :func:`resolve` because the wizard and the catalog need it without a
    Request in hand. Unknown values resolve to :data:`NEUTRAL` rather than raising, for
    the same reason ``feature_flags.install_profile`` falls back to ``demo``: this is read
    on the request path, and a typo in one config row must degrade to today's behaviour.

    A stored row is IGNORED rather than cleared when :func:`applies` is false -- an instance
    reconfigured from estate to POV and back must find its default where it left it.
    """
    if not applies():
        return NEUTRAL
    raw = (config_service.get("default_persona") or "").strip().lower()
    return raw if raw in VALID_PERSONAS else NEUTRAL


def resolve_for_user(user) -> tuple:
    """``(key, source)`` for one user, from their own assignment then their groups'.

    Duck-typed on ``.persona`` / ``.session_persona`` rather than typed against
    ``database.User``, so this module keeps importing nothing but config_service and
    feature_flags -- ``jobs_worker`` imports it, and the whole reason it lives in
    ``services/`` is that it costs a process serving no requests nothing.

    ``persona`` outranks ``session_persona`` because a decision about one person outranks
    a rule about a group they happen to be in. Both are validated here rather than trusted:
    a stale key left behind by a persona being retired from the registry must read as
    "unset", not as a focus that no longer exists. An instance with no focus axis at all
    (see :func:`applies`) is the same case one step further out, and it is checked FIRST:
    a stored assignment on a POV instance must read as unset too, or ``/api/auth/me`` would
    hand the browser an assignment for a lens that is not on the page.
    """
    if not applies():
        return NEUTRAL, "none"
    for attr, src in (("persona", "user"), ("session_persona", "group")):
        raw = (getattr(user, attr, None) or "").strip().lower()
        if raw in VALID_PERSONAS:
            return raw, src
    key = default_persona()
    return (key, "default") if key else (NEUTRAL, "none")


# Bigger than any priority an admin would plausibly type, so an unprioritised mapping
# loses to every prioritised one without needing a second sort key or a sentinel column.
_NO_PRIORITY = 1_000_000


def persona_for_groups(mappings) -> str:
    """The focus a set of matched OIDC group mappings confers, or :data:`NEUTRAL`.

    Called from the login path, which is the ONLY place that knows which groups matched --
    ``_complete_oauth_login`` maps group ids to workgroups and then forgets the ids, so the
    answer has to be computed while they are in hand and stored on the user.

    Lowest ``persona_priority`` wins. A mapping with no persona expresses no opinion, which
    is what lets a broad catch-all group grant a workgroup without also dictating a focus.
    Ties -- and rows with no priority at all -- fall back to ``display_name``, so the
    outcome never depends on row order, which is not stable across databases.

    Neutral on an instance with no focus axis (:func:`applies`). This is the write half of
    that decision: the login path stores the answer on ``User.session_persona``, so without
    the guard a POV instance would keep writing a focus nothing reads -- and an instance
    later reconfigured to an estate would inherit assignments nobody made.
    """
    if not applies():
        return NEUTRAL
    claims = []
    for m in mappings or []:
        raw = (getattr(m, "persona", None) or "").strip().lower()
        if raw not in VALID_PERSONAS:
            continue
        prio = getattr(m, "persona_priority", None)
        # None sorts last, not first: an unprioritised row is the weakest opinion, not the
        # strongest. `or ""` because display_name is nullable in practice on old rows.
        claims.append((prio if isinstance(prio, int) else _NO_PRIORITY,
                       (getattr(m, "display_name", None) or ""), raw))
    if not claims:
        return NEUTRAL
    claims.sort(key=lambda c: (c[0], c[1]))
    return claims[0][2]



def resolve(request=None) -> tuple:
    """The active persona and where it came from, as ``(key, source)``.

    Precedence: ``?persona=`` (this render only, persists nothing) > the ``persona``
    cookie (an explicit pick in the nav lens) > the ``persona_assigned`` cookie (the
    focus assigned to this user or their OIDC group, cached from ``/api/auth/me``) > the
    instance default > neutral.

    An explicit pick outranks an assignment deliberately: an assignment is a default, not
    a lock. An SE presenting to a different role must be able to switch without asking an
    admin, and nothing is being protected by pinning it -- a persona cannot grant or
    remove access. Clearing the ``persona`` cookie returns them to their assignment.

    ``source`` is returned rather than discarded because a shared ``?persona=`` link means
    "why is my nav in a strange order" has three possible causes. The lens control shows
    the answer, so the question never becomes a support ticket.

    **The return value is always a member of :data:`VALID_PERSONAS` or :data:`NEUTRAL` --
    never a pass-through of the query string.** ``?persona=`` is reflected into HTML, and
    ``api/docs_pages`` already carries an escaping fix for exactly this shape.

    All three request-borne tiers are skipped when :func:`applies` is false. The cookie tier
    is why that check has to live here rather than only on the default: a ``persona`` cookie
    set while an instance was an estate one would otherwise keep curating after it became a
    POV instance, and a shared ``?persona=`` link would do it to anyone who clicked.
    """
    if not applies():
        return NEUTRAL, "none"
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
        # The assigned focus, cached client-side. This tier exists because an HTML page
        # load carries NO identity: the token lives in localStorage and is only sent as a
        # Bearer header on /api/* XHR, so this function cannot ask who is asking. The
        # client fetches /api/auth/me at login -- a hop it already makes for is_admin --
        # and writes the server's answer here, where the nav's own render can read it.
        #
        # Format `<user|group>:<key>`, so the lens can say WHICH rule produced the focus
        # rather than just naming it. An unparseable or unknown value is ignored rather
        # than raising: it is attacker-editable input on the request path.
        #
        # Editable, and that is acceptable for exactly one reason -- a persona may never
        # gate anything, so the most anyone achieves by forging this is reordering their
        # own nav. The day a persona can hide a page, this whole tier becomes wrong.
        try:
            raw = (request.cookies.get("persona_assigned") or "").strip().lower()
        except Exception:  # noqa: BLE001
            raw = ""
        if ":" in raw:
            src, _, key = raw.partition(":")
            if key in VALID_PERSONAS and src in ("user", "group"):
                return key, src

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

    EMPTY when :func:`applies` is false, and empty rather than neutral-only: a list holding
    just "No focus" is a control that offers one choice you already have. The templates that
    render a picker are gated on the same answer (``persona_focus``), so this is the second
    half of a belt-and-braces pair -- whichever of the two a future edit forgets, no POV
    operator is offered a focus. Note this is NOT the same decision as :func:`catalog`,
    which stays profile-blind because ``/use-cases`` still describes the whole catalog.
    """
    if not applies():
        return []
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
