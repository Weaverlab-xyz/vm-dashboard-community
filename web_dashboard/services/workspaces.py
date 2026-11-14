"""Workspaces: one landing page per estate persona, for the people who DO the role.

``/demo`` is the presenter's page and ``/pov`` the POV presenter's. A Cloud Ops or DevOps
engineer given a login on an estate instance had neither -- the dashboard reorders itself
for them (``services/personas``) and ``/use-cases`` lists demo scripts, but nothing said
"this is your area, start here". A workspace is that page: a getting-started checklist
that ticks itself off from the user's own completed jobs, the resources they can see
that belong to the role, and the pages the role works from.

**Curation only, like the persona it belongs to.** Every rule in the ``personas``
docstring applies here unchanged, and three follow from it:

  * **Any workspace is open to anyone signed in.** Which one a user lands on is decided
    by their persona; which ones they may open is not decided by anything. A workspace
    only ever SURFACES -- the data under it is still filtered by the caller's own
    workgroups and scopes, by the same readers the pages it links to use.
  * **Readiness has one reader.** Every step and toolkit link is a
    :class:`personas.UseCase` resolved by :func:`personas.describe_card` -- the reader
    the Use cases page and the Demo launchpad share -- so a masked link carries no href.
  * **A step navigates; it never starts work.** It is ticked by a job the user ran on the
    page it links to, never by this module running one.

The profile gate (``feature_flags._PROFILE_PAGES["workspace_pages"]``) is what makes this
estate-only, as ``personas.applies`` is: a POV instance has no role to give a workspace.

Dependencies: ``personas``, ``feature_flags`` and the database models only -- nothing from
the api layer, and ``personas`` never imports this module (tests/test_workspace pins both).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import feature_flags, personas
from .personas import UseCase, describe_card

# Where a cloud's VMs are managed. The same paths inventory_service._VM_JOBS links to, so a
# step that says "deploy a VM" opens the console the inventory row would.
CLOUD_HOME = {
    "aws": "/aws#instances",
    "azure": "/azure#vms",
    "gcp": "/gcp",
    "oci": "/oci",
}
_ALL_CLOUDS = ("aws", "azure", "gcp", "oci")

# Where an inventory item of each kind is managed, for the "Your resources" table.
_KIND_HOME = {
    "database": "/databases",
    "k8s": "/k8s",
    "function": "/functions",
    "directory": "/directories",
    "desktop": "/desktops",
    "certlab": "/workload-lab#certificates",
    "spirelab": "/workload-lab#spire",
    "workloadk8s": "/workload-lab#kubernetes",
    "workloadcloud": "/workload-lab#cloud",
}
_HYPERVISOR_HOME = {
    "proxmox": "/proxmox", "nutanix": "/nutanix", "vsphere": "/vsphere",
    "hyperv": "/hyperv", "xcpng": "/xcpng", "workstation": "/vms",
}

RESOURCE_CAP = 25
RECENT_JOBS_CAP = 10

# ── job types a step can be ticked by ─────────────────────────────────────────
# Every name here is checked against the job types the code actually enqueues
# (tests/test_workspace), so a renamed job type fails the suite instead of leaving a
# step that can never tick.
_VM_DEPLOYS = ("ec2_deploy", "azure_deploy", "gce_deploy", "oci_deploy",
               "ec2_bulk_deploy", "azure_bulk_deploy", "gce_bulk_deploy", "oci_bulk_deploy")
_VM_POWER = ("ec2_power", "azure_power", "gce_power", "oci_power")
_IMAGE_BUILDS = ("packer_aws_build", "packer_azure_build", "packer_gcp_build",
                 "packer_oci_build", "ec2_create_image", "azure_create_image",
                 "gce_capture_image")
_IMAGE_PROMOTES = ("image_promote_aws", "image_promote_azure", "image_promote_gcp",
                   "image_promote_oci", "ami_copy")
_ANSIBLE = ("ansible_local", "agent_ansible", "ansible_cloud_run")
_GATEWAY = ("gateway_deploy", "agent_gateway")


@dataclass(frozen=True)
class Step:
    """One getting-started step.

    ``card`` carries the link and its readiness requirements, so it resolves through the
    same reader as every other card. ``done_job_types`` ticks it: the step is done once
    the user has a COMPLETED job of one of those types that they created. With
    ``done_schedule`` it is a "make it recurring" step instead, ticked ONLY by a schedule
    they own of one of those types (any type, when the tuple is empty) -- never by the
    one-off job, which is what the step before it asked for.

    A step with neither is a "look" step -- somewhere the role should know about, with
    nothing to run. It is shown without a checkbox and does not count toward progress,
    because a box that can never tick reads as a task the user has failed to do.

    ``cloud_home``: the target is "this role's cloud console", rewritten at describe time
    to the first cloud this instance has credentials for -- so a GCP-only instance's
    "Deploy your first VM" opens /gcp rather than an AWS page with nothing on it.
    """
    card: UseCase
    done_job_types: tuple = ()
    done_schedule: bool = False
    cloud_home: bool = False

    @property
    def checkable(self) -> bool:
        return bool(self.done_job_types) or self.done_schedule


@dataclass(frozen=True)
class Workspace:
    """One persona's workspace. ``key`` is a member of ``personas.VALID_PERSONAS``.

    ``resource_kinds`` / ``resource_clouds`` choose which of the caller's inventory items
    the "Your resources" tab lists (an empty tuple means any). ``job_types`` chooses the
    recent jobs shown beside them; when empty, the union of the steps' job types is used.
    """
    key: str
    headline: str
    steps: tuple
    toolkit: tuple
    resource_kinds: tuple = ()
    resource_clouds: tuple = ()
    job_types: tuple = ()


def _step(id, title, summary, target="", *, docs="", done=(), schedule=False,
          cloud_home=False, **requires) -> Step:
    return Step(card=UseCase(id=id, title=title, summary=summary, target=target,
                             minutes=0, docs=docs, **requires),
                done_job_types=tuple(done), done_schedule=schedule, cloud_home=cloud_home)


def _link(id, title, summary, target, **requires) -> UseCase:
    return UseCase(id=id, title=title, summary=summary, target=target, minutes=0,
                   **requires)


# ── the shared toolkit links ──────────────────────────────────────────────────
# One definition per page, so the flag a page is gated on is written once. Each workspace
# picks the ones its role uses.

_HYPERVISOR_FLAGS = ("vmware_enabled", "proxmox_enabled", "vsphere_enabled",
                     "hyperv_enabled", "nutanix_enabled", "xcpng_enabled")

_L = {
    "aws": _link("aws", "AWS", "EC2 instances: deploy, power, onboard.", "/aws",
                 requires_clouds=("aws",)),
    "azure": _link("azure", "Azure", "Azure VMs: deploy, power, onboard.", "/azure",
                   requires_clouds=("azure",)),
    "gcp": _link("gcp", "GCP", "Compute Engine: deploy, power, onboard.", "/gcp",
                 requires_clouds=("gcp",)),
    "oci": _link("oci", "OCI", "OCI instances: deploy, power, onboard.", "/oci",
                 requires_clouds=("oci",)),
    "images": _link("images", "Images", "Build, capture and promote golden images.",
                    "/images", requires_clouds=_ALL_CLOUDS),
    "inventory": _link("inventory", "Inventory",
                       "Everything you can see that the dashboard deployed, with its "
                       "end date.", "/inventory"),
    "jobs": _link("jobs", "Jobs", "What ran, what is running, and what failed.", "/jobs"),
    "schedules": _link("schedules", "Schedules",
                       "Recurring runs and power schedules you own.", "/schedules"),
    "costs": _link("costs", "Costs", "Spend by workstream and by resource.", "/costs",
                   requires_flags=("cost_explorer_enabled",)),
    "storage": _link("storage", "Storage", "Buckets and the files a deploy reads.",
                     "/storage"),
    "config_mgmt": _link("config_mgmt", "Configuration",
                         "Ansible playbooks against VMs, clusters and databases.",
                         "/config-mgmt", requires_flags=("ansible_enabled",)),
    "secrets": _link("secrets", "Secrets", "The secrets a run is allowed to use.",
                     "/secrets"),
    "k8s": _link("k8s", "Kubernetes", "Clusters, bindings and tunnels.", "/k8s",
                 requires_flags=("k8s_management_enabled",)),
    "functions": _link("functions", "Functions", "Cloud functions and their secrets.",
                       "/functions", requires_flags=("cloud_functions_enabled",)),
    "databases": _link("databases", "Databases",
                       "Managed databases, onboarded and registered for JIT.",
                       "/databases", requires_flags=("cloud_database_enabled",)),
    "containers": _link("containers", "Containers",
                        "Container hosts, Gateways, Portainer and Rancher.", "/containers"),
    "gateways": _link("gateways", "Gateways",
                      "The outbound-only Gateways every brokered session goes through.",
                      "/containers#gateways", requires_flags=("pra_enabled",)),
    "workload_lab": _link("workload_lab", "Workload Lab",
                          "Machine identity: certificates, SPIRE, cluster tokens, cloud "
                          "credentials.", "/workload-lab",
                          requires_any_flag=("cert_lab_enabled", "spire_lab_enabled",
                                             "agentcell_enabled")),
    "directories": _link("directories", "Directories",
                         "Domains, identity providers and their groups.", "/directories",
                         requires_flags=("directories_enabled",)),
    "directory_health": _link("directory_health", "Directory health",
                              "Is every domain controller and identity provider "
                              "answering?", "/directories/health",
                              requires_flags=("directories_enabled",)),
    "desktops": _link("desktops", "Desktops", "Virtual desktop pools.", "/desktops",
                      requires_flags=("vdesktops_enabled",)),
    "agents": _link("agents", "Agents",
                    "Remote agents inside networks the dashboard cannot reach.", "/agents"),
    "connections": _link("connections", "Connections",
                         "Where each hypervisor lives and how to reach it.",
                         "/connections", requires_any_flag=_HYPERVISOR_FLAGS),
    "proxmox": _link("proxmox", "Proxmox", "Proxmox VE.", "/proxmox",
                     requires_flags=("proxmox_enabled",)),
    "vsphere": _link("vsphere", "vSphere", "VMware vSphere.", "/vsphere",
                     requires_flags=("vsphere_enabled",)),
    "nutanix": _link("nutanix", "Nutanix", "Nutanix AHV.", "/nutanix",
                     requires_flags=("nutanix_enabled",)),
    "hyperv": _link("hyperv", "Hyper-V", "Microsoft Hyper-V.", "/hyperv",
                    requires_flags=("hyperv_enabled",)),
    "xcpng": _link("xcpng", "XCP-ng", "XCP-ng.", "/xcpng",
                   requires_flags=("xcpng_enabled",)),
    "vms": _link("vms", "Workstation", "Local VMware Workstation VMs.", "/vms",
                 requires_flags=("vmware_enabled",)),
    "audit": _link("audit", "Audit", "Who did what, and when.", "/audit"),
    "notifications": _link("notifications", "Notifications",
                           "Where privileged-access events are sent.", "/notifications"),
    "rbac": _link("rbac", "RBAC", "Users, groups, roles and workgroups.", "/rbac"),
    "ot_cells": _link("ot_cells", "OT cells",
                      "Simulated plant cells reached only through PRA.", "/gcp#ot",
                      requires_flags=("pra_enabled",),
                      requires_clouds=("aws", "azure", "gcp")),
    "net_cells": _link("net_cells", "Network cells",
                       "A VyOS router with no inbound rule.", "/gcp#net",
                       requires_flags=("netcell_enabled",), requires_clouds=("gcp",)),
    "use_cases": _link("use_cases", "Use cases",
                       "Guided walk-throughs of what this role can show.", "/use-cases"),
}


def _tools(*keys) -> tuple:
    return tuple(_L[k] for k in keys)


# ── the workspaces ────────────────────────────────────────────────────────────

_CLOUDOPS = Workspace(
    key="cloudops",
    headline="Your clouds, the VMs in them and the images they start from. Everything you "
             "deploy here is under privileged-access management before anyone logs in.",
    steps=(
        _step("cloudops-first-vm", "Deploy your first VM",
              "Pick a cloud, a size and an image. The deploy job onboards the VM into "
              "PRA and Password Safe in the same run, so there is no window where it "
              "exists unmanaged.",
              cloud_home=True, docs="clouds/vms", done=_VM_DEPLOYS,
              requires_clouds=_ALL_CLOUDS),
        _step("cloudops-power", "Stop a VM you are not using",
              "Power it off from its console row. A stopped VM keeps its disk and its "
              "onboarding, and stops costing compute.",
              cloud_home=True, docs="clouds/vms", done=_VM_POWER,
              requires_clouds=_ALL_CLOUDS),
        _step("cloudops-bake-image", "Bake a golden image",
              "Build a hardened image with Packer, or capture one from a VM you have "
              "configured, so the next deploy starts from an audited baseline.",
              "/images", docs="operations/image-management/packer-build",
              done=_IMAGE_BUILDS, requires_clouds=_ALL_CLOUDS),
        _step("cloudops-promote-image", "Promote it to another cloud or region",
              "Copy the image where your other workloads run, so every region starts "
              "from the same thing.",
              "/images", docs="operations/image-management/promotion",
              done=_IMAGE_PROMOTES, requires_clouds=_ALL_CLOUDS),
        _step("cloudops-schedule", "Put a VM on a schedule",
              "Make a deploy or a power change recurring — business hours for a lab, "
              "or a nightly rebuild — from the job that did it once.",
              "/schedules", docs="operations/scheduling", schedule=True,
              done=_VM_DEPLOYS + _VM_POWER),
        _step("cloudops-guardrails", "Know the guardrails",
              "Admission policy can refuse a deploy up front, and the auto-delete timer "
              "removes what outlives its end date. Check what yours carry in Inventory.",
              "/inventory", docs="operations/scheduling/policy-guardrails"),
    ),
    toolkit=(
        ("Clouds", _tools("aws", "azure", "gcp", "oci")),
        ("Images and storage", _tools("images", "storage")),
        ("Running it", _tools("inventory", "jobs", "schedules", "costs")),
    ),
    resource_kinds=("vm",),
    resource_clouds=_ALL_CLOUDS,
    job_types=_VM_DEPLOYS + _VM_POWER + _IMAGE_BUILDS + _IMAGE_PROMOTES
    + ("ec2_destroy", "azure_destroy", "gce_destroy", "oci_destroy"),
)

_DEVOPS = Workspace(
    key="devops",
    headline="Your pipelines and playbooks, and the short-lived credentials they fetch at "
             "run time instead of holding.",
    steps=(
        _step("devops-first-playbook", "Run your first playbook",
              "Point a playbook at a VM, cluster or database. Its credential is checked "
              "out for the run and nothing is left on disk afterwards.",
              "/config-mgmt", docs="operations/config-management", done=_ANSIBLE,
              requires_flags=("ansible_enabled",)),
        _step("devops-schedule-playbook", "Make it recurring",
              "Turn a playbook run into a schedule — the drift check that runs every "
              "night rather than when somebody remembers.",
              "/schedules", docs="operations/scheduling", schedule=True, done=_ANSIBLE),
        _step("devops-cloud-credential", "Give a build a cloud credential, not a key",
              "Issue a workload a short-lived cloud credential per run in place of an "
              "access key in a CI secret store.",
              "/workload-lab#cloud", docs="preview/workload-lab/cloud",
              done=("workload_cloud_credential",),
              requires_flags=("workload_credentials_enabled", "spire_lab_enabled")),
        _step("devops-cluster-token", "Reach a cluster without a kubeconfig",
              "Have a build fetch a namespace-scoped ServiceAccount token from Password "
              "Safe per run.",
              "/workload-lab#kubernetes", docs="preview/workload-lab/kubernetes",
              done=("workload_k8s_token", "k8s_ps_token"),
              requires_flags=("k8s_management_enabled", "password_safe_enabled",
                              "spire_lab_enabled")),
        _step("devops-function", "Deploy a function that fetches its secret",
              "A cloud function with no secret in its environment, reading what it needs "
              "from the vault on first invocation.",
              "/functions", docs="cloud-services/functions", done=("cloudfn_deploy",),
              requires_flags=("cloud_functions_enabled",)),
        _step("devops-cluster", "Stand up a cluster to deploy into",
              "Provision a managed Kubernetes cluster with its access already brokered.",
              "/k8s", docs="cloud-services/kubernetes", done=("k8s_provision",),
              requires_flags=("k8s_management_enabled",)),
    ),
    toolkit=(
        ("Pipelines", _tools("config_mgmt", "schedules", "jobs")),
        ("Where it runs", _tools("k8s", "functions", "containers", "inventory")),
        ("Credentials", _tools("workload_lab", "secrets")),
    ),
    resource_kinds=("vm", "k8s", "function", "workloadk8s", "workloadcloud"),
    job_types=_ANSIBLE + ("workload_cloud_credential", "workload_k8s_token",
                          "k8s_ps_token", "cloudfn_deploy", "cloudfn_update",
                          "k8s_provision", "k8s_secret_delivery", "container_deploy",
                          "compose_deploy", "stack_deploy"),
)

_HYPERVISOR = Workspace(
    key="hypervisor",
    headline="The virtualisation layer: hosts, the VMs on them, and the Gateways and "
             "agents that reach them without an inbound rule.",
    steps=(
        _step("hypervisor-first-vm", "Deploy a VM on Proxmox",
              "Create a VM from a template on a node you can reach.",
              "/proxmox", docs="hypervisors", done=("proxmox_deploy", "nutanix_deploy"),
              requires_flags=("proxmox_enabled",)),
        _step("hypervisor-import-image", "Import an image",
              "Bring a disk image onto the hypervisor so VMs can be built from it.",
              "/proxmox", docs="hypervisors",
              done=("proxmox_import_image", "nutanix_import_image"),
              requires_flags=("proxmox_enabled",)),
        _step("hypervisor-gateway", "Stand up a Gateway",
              "The outbound-only broker every console and Shell Jump session goes "
              "through.",
              "/containers#gateways", docs="beyondtrust/gateways", done=_GATEWAY,
              requires_flags=("pra_enabled",)),
        _step("hypervisor-discover", "Discover a site through an agent",
              "An agent inside the datacentre reports what is there — no inbound "
              "access to the management network.",
              "/agents", docs="platform/spiffe/remote-agents",
              done=("agent_discover", "agent_hypervisor"),
              requires_flags=("remote_agents_enabled",)),
    ),
    toolkit=(
        ("Hypervisors", _tools("proxmox", "vsphere", "nutanix", "hyperv", "xcpng", "vms")),
        ("Reaching them", _tools("connections", "agents", "gateways")),
        ("Running it", _tools("inventory", "jobs")),
    ),
    resource_kinds=("vm",),
    resource_clouds=tuple(_HYPERVISOR_HOME),
    job_types=("proxmox_deploy", "proxmox_delete", "proxmox_import_image",
               "nutanix_deploy", "nutanix_delete_vm", "nutanix_import_image",
               "agent_discover", "agent_hypervisor") + _GATEWAY,
)

_ITOPS = Workspace(
    key="itops",
    headline="Endpoints and desktops where people get work done without anybody being a "
             "local administrator.",
    steps=(
        _step("itops-desktops", "Stand up a desktop pool",
              "Virtual desktops your users sign in to, with support sessions recorded.",
              "/desktops", docs="beyondtrust/privileged-remote-access",
              done=("vdesktop_pool_provision",), requires_flags=("vdesktops_enabled",)),
        _step("itops-epm", "Sync least-privilege policy to Linux endpoints",
              "Let users run the one command that needs elevation and nothing else.",
              "/settings", docs="beyondtrust/epml", done=("epml_sync",),
              requires_flags=("epml_enabled",)),
        _step("itops-playbook", "Fix many machines with one playbook",
              "Run a configuration change across a group of machines instead of "
              "logging into each.",
              "/config-mgmt", docs="operations/config-management", done=_ANSIBLE,
              requires_flags=("ansible_enabled",)),
        _step("itops-agents", "See the machines your agents can reach",
              "Remote agents manage endpoints that are not on the corporate network.",
              "/agents", docs="platform/spiffe/remote-agents",
              requires_flags=("remote_agents_enabled",)),
    ),
    toolkit=(
        ("Endpoints", _tools("vms", "desktops", "agents")),
        ("Running it", _tools("config_mgmt", "inventory", "jobs")),
    ),
    resource_kinds=("vm", "desktop"),
    job_types=("vdesktop_pool_provision", "vdesktop_pool_teardown", "epml_sync",
               "windows_arc_join") + _ANSIBLE,
)

_OT = Workspace(
    key="ot",
    headline="Plant networks reached with no VPN and no inbound firewall hole — "
             "protocol-aware tunnels, every session recorded.",
    steps=(
        _step("ot-first-cell", "Build an OT cell",
              "A simulated plant in a subnet with no public IP, reachable only through "
              "brokered tunnels.",
              "/gcp#ot", docs="profiles/demo/ot-demo-cell", done=("ot_cell_deploy",),
              requires_flags=("pra_enabled",), requires_clouds=("aws", "azure", "gcp")),
        _step("ot-gateway", "Stand up a Gateway",
              "The egress-only path from the cell out to the appliance.",
              "/containers#gateways", docs="beyondtrust/gateways", done=_GATEWAY,
              requires_flags=("pra_enabled",)),
        _step("ot-discover", "Discover a site through an agent",
              "An agent on the plant side reports what is there and polls outward.",
              "/agents", docs="platform/spiffe/remote-agents",
              done=("agent_discover",), requires_flags=("remote_agents_enabled",)),
    ),
    toolkit=(
        ("Plant", _tools("ot_cells", "gateways", "agents")),
        ("Running it", _tools("inventory", "jobs", "audit")),
    ),
    resource_kinds=("vm",),
    resource_clouds=("aws", "azure", "gcp"),
    job_types=("ot_cell_deploy", "agent_discover") + _GATEWAY,
)

_DBA = Workspace(
    key="dba",
    headline="Databases whose admin accounts are managed from birth, and whose access is "
             "granted per request and expires.",
    steps=(
        _step("dba-provision", "Provision a database",
              "Postgres, MySQL or SQL Server, in your workstream.",
              "/databases", docs="cloud-services/databases", done=("clouddb_provision",),
              requires_flags=("cloud_database_enabled",)),
        _step("dba-onboard", "Onboard its admin account",
              "Put the admin credential under Password Safe and rotate it, so nobody is "
              "holding the password the database was created with.",
              "/databases", docs="beyondtrust/password-safe", done=("clouddb_ps_register",),
              requires_flags=("cloud_database_enabled", "password_safe_enabled")),
        _step("dba-jit", "Register it for just-in-time grants",
              "Analysts request access to a schema, an approver says yes, and the grant "
              "removes itself.",
              "/databases", docs="design/entitle-resource-registration",
              done=("clouddb_entitle_register",),
              requires_flags=("cloud_database_enabled", "entitle_enabled")),
        _step("dba-power", "Stop a database out of hours",
              "Power it down when nobody is using it, and back up when they are.",
              "/databases", docs="cloud-services/databases", done=("clouddb_power",),
              requires_flags=("cloud_database_enabled",)),
    ),
    toolkit=(
        ("Databases", _tools("databases", "secrets")),
        ("Running it", _tools("config_mgmt", "inventory", "jobs", "schedules")),
    ),
    resource_kinds=("database",),
    job_types=("clouddb_provision", "clouddb_ps_register", "clouddb_entitle_register",
               "clouddb_power", "clouddb_decommission", "clouddb_dbops_deploy"),
)

_SECURITY = Workspace(
    key="security",
    headline="Who has access to what right now, what they did with it, and what should "
             "have expired already.",
    steps=(
        _step("security-inventory", "See every privileged resource",
              "Everything the dashboard deployed that you can see, with who made it and "
              "when it ends.", "/inventory", docs="operations/inventory"),
        _step("security-audit", "Read the audit trail",
              "Every change, grant and run, with the user behind it.",
              "/audit", docs="admin/audit-log"),
        _step("security-notifications", "Send events where they get read",
              "Stream privileged-access events to Slack, Teams or a signed webhook.",
              "/notifications", docs="admin/notifications",
              requires_flags=("notifications_enabled",)),
        _step("security-expiry", "Check nothing outlives its purpose",
              "Resources past their end date are removed; the ones with none are worth "
              "a question.", "/inventory", docs="operations/auto-delete-timer",
              requires_flags=("resource_expiry_enabled",)),
    ),
    toolkit=(
        ("Oversight", _tools("inventory", "audit", "jobs")),
        ("Controls", _tools("secrets", "notifications", "rbac")),
    ),
)

_SRE = Workspace(
    key="sre",
    headline="Kubernetes and the platform layer, with access bound to the person who asked "
             "and lasting as long as the incident.",
    steps=(
        _step("sre-cluster", "Provision a cluster",
              "A managed cluster with its API access already brokered.",
              "/k8s", docs="cloud-services/kubernetes", done=("k8s_provision",),
              requires_flags=("k8s_management_enabled",)),
        _step("sre-binding", "Bind your team's group to it",
              "Federate cluster access to the directory, so joining a team grants it and "
              "leaving revokes it.",
              "/k8s", docs="platform/oidc/k8s-federation/entra",
              done=("k8s_group_binding", "k8s_entra_federation"),
              requires_flags=("k8s_management_enabled",)),
        _step("sre-tunnel", "Reach a private API server",
              "kubectl through a brokered tunnel rather than a bastion.",
              "/k8s", docs="cloud-services/kubernetes",
              done=("k8s_api_tunnel", "k8s_tunnel"),
              requires_flags=("k8s_management_enabled", "pra_enabled")),
        _step("sre-platform-node", "Stand up a Rancher or Portainer node",
              "A container platform whose admin login is vaulted and injected.",
              "/containers#rancher", docs="cloud-services/rancher",
              done=("rancher_node_deploy", "portainer_node_deploy"),
              requires_flags=("k8s_management_enabled", "pra_enabled")),
    ),
    toolkit=(
        ("Platform", _tools("k8s", "containers", "functions")),
        ("Running it", _tools("config_mgmt", "inventory", "jobs", "schedules")),
    ),
    resource_kinds=("k8s", "workloadk8s"),
    job_types=("k8s_provision", "k8s_group_binding", "k8s_entra_federation",
               "k8s_api_tunnel", "k8s_tunnel", "k8s_power", "k8s_decommission",
               "rancher_node_deploy", "portainer_node_deploy"),
)

_NETADMIN = Workspace(
    key="netadmin",
    headline="Routers and firewalls reached with no inbound rule, changed in recorded "
             "sessions with nobody handed the password.",
    steps=(
        _step("netadmin-cell", "Open the network cell",
              "A VyOS router in a subnet with no public address, managed through PRA "
              "and Password Safe.",
              "/gcp#net", docs="profiles/demo/net-demo-cell",
              requires_flags=("netcell_enabled",), requires_clouds=("gcp",)),
        _step("netadmin-gateway", "Stand up a Gateway",
              "The broker that dials out, so there is nothing inbound to attack.",
              "/containers#gateways", docs="beyondtrust/gateways", done=_GATEWAY,
              requires_flags=("pra_enabled",)),
        _step("netadmin-audit", "Find what changed, and who changed it",
              "Answer from the record rather than a config diff and a guess.",
              "/audit", docs="admin/audit-log"),
    ),
    toolkit=(
        ("Network", _tools("net_cells", "gateways", "agents")),
        ("Running it", _tools("inventory", "jobs", "audit", "secrets")),
    ),
    resource_kinds=("vm",),
    resource_clouds=("gcp",),
    job_types=("gce_deploy",) + _GATEWAY,
)

_FINOPS = Workspace(
    key="finops",
    headline="What is running, what it costs, and what nobody decided to keep.",
    steps=(
        _step("finops-costs", "See what it costs",
              "Spend by workstream and by resource, from the dashboard's own record.",
              "/costs", docs="operations/costs", requires_flags=("cost_explorer_enabled",)),
        _step("finops-power", "Stop something nobody is using",
              "Power a VM down from its console row; it keeps its disk and onboarding.",
              cloud_home=True, docs="clouds/vms", done=_VM_POWER,
              requires_clouds=_ALL_CLOUDS),
        _step("finops-schedule", "Put a lab on business hours",
              "Make a power change recurring so a lab sleeps when its team does.",
              "/schedules", docs="operations/scheduling", schedule=True, done=_VM_POWER),
        _step("finops-end-dates", "Find what has no end date",
              "Every resource can carry an expiry; the ones without one are the "
              "conversation.", "/inventory", docs="operations/auto-delete-timer"),
    ),
    toolkit=(
        ("Spend", _tools("costs", "inventory", "schedules")),
        ("Clouds", _tools("aws", "azure", "gcp", "oci")),
        ("Oversight", _tools("audit", "jobs")),
    ),
    resource_kinds=("vm", "database", "k8s"),
    job_types=_VM_POWER + ("clouddb_power", "k8s_power"),
)

_AIOPS = Workspace(
    key="aiops",
    headline="The things that act without a person at the keyboard: what they may do, "
             "what they did, and how to stop one.",
    steps=(
        _step("aiops-spire", "Stand up a SPIRE trust domain",
              "Workloads attest to it on every call instead of holding an identity "
              "document.",
              "/workload-lab#spire", docs="preview/workload-lab/spiffe",
              done=("spirelab_provision",), requires_flags=("spire_lab_enabled",)),
        _step("aiops-cloud-credential", "Issue a leased cloud credential",
              "A credential minted per run, with its own issuance audit.",
              "/workload-lab#cloud", docs="preview/workload-lab/cloud",
              done=("workload_cloud_credential",),
              requires_flags=("spire_lab_enabled", "workload_credentials_enabled")),
        _step("aiops-agent", "Mint an agent, and revoke it",
              "A worker with a scoped token; revoke it and watch the next call refused.",
              "/workload-lab#agent", docs="profiles/demo/agent-demo-cell",
              requires_flags=("agentcell_enabled", "mcp_server_enabled")),
        _step("aiops-audit", "Read what it did",
              "Every call it made is in the job trail and the audit log.",
              "/audit", docs="admin/audit-log"),
    ),
    toolkit=(
        ("Machine identity", _tools("workload_lab", "secrets")),
        ("Oversight", _tools("audit", "jobs", "rbac")),
    ),
    resource_kinds=("spirelab", "workloadcloud", "workloadk8s", "certlab"),
    job_types=("spirelab_provision", "spirelab_federate", "spirelab_jwt_bundle",
               "workload_cloud_credential", "workload_k8s_token"),
)

_IAMOPS = Workspace(
    key="iamops",
    headline="The directories: domains, identity providers and the groups that grant "
             "access, with no standing domain admin.",
    steps=(
        _step("iamops-directory", "Build a managed directory",
              "A managed AD domain whose delegated admin goes straight into Password "
              "Safe and is rotated.",
              "/directories", docs="preview/directories/active-directory",
              done=("directory_provision",), requires_flags=("directories_enabled",)),
        _step("iamops-health", "Check every directory is answering",
              "Replication, FSMO, clock and sign-in health in one place.",
              "/directories/health", docs="preview/directories/health",
              requires_flags=("directories_enabled",)),
        _step("iamops-audit-run", "Audit privileged groups from inside the network",
              "Run the read-only AD audits through an agent on the domain's own network.",
              "/config-mgmt", docs="preview/directories/on-premises", done=_ANSIBLE,
              requires_flags=("ansible_enabled", "directories_enabled",
                              "remote_agents_enabled")),
    ),
    toolkit=(
        ("Directories", _tools("directories", "directory_health")),
        ("Running it", _tools("config_mgmt", "agents", "jobs", "audit")),
    ),
    resource_kinds=("directory",),
    job_types=("directory_provision", "directory_decommission", "windows_arc_join",
               "windows_hybrid_check") + _ANSIBLE,
)

_IDARCH = Workspace(
    key="idarch",
    headline="The identity plane: how the directories connect, where privilege is granted, "
             "and the evidence none of it is standing.",
    steps=(
        _step("idarch-map", "Map every directory",
              "Cloud and on-prem domains beside the identity providers, each with the "
              "integration that grants access to it.",
              "/directories", docs="preview/directories",
              requires_flags=("directories_enabled",)),
        _step("idarch-hybrid", "Confirm hybrid join",
              "Check cloud servers are joined to the forest and to Entra.",
              "/directories", docs="preview/directories/on-premises",
              done=("windows_hybrid_check", "windows_arc_join"),
              requires_flags=("directories_enabled", "remote_agents_enabled")),
        _step("idarch-rbac", "Review who can grant what",
              "The roles and group mappings behind every scope in this console.",
              "/rbac", docs="admin/permissions"),
        _step("idarch-audit", "Read the evidence",
              "Every membership change and grant, on the record.",
              "/audit", docs="admin/audit-log"),
    ),
    toolkit=(
        ("Identity", _tools("directories", "directory_health", "rbac")),
        ("Oversight", _tools("audit", "jobs")),
    ),
    resource_kinds=("directory",),
    job_types=("directory_provision", "windows_hybrid_check", "windows_arc_join"),
)


_WORKSPACES = {w.key: w for w in (
    _CLOUDOPS, _DEVOPS, _HYPERVISOR, _ITOPS, _OT, _DBA, _SECURITY, _SRE, _NETADMIN,
    _FINOPS, _AIOPS, _IAMOPS, _IDARCH,
)}


def get(key: str):
    """The :class:`Workspace` for ``key``, or ``None``."""
    return _WORKSPACES.get((key or "").strip().lower())


def all_workspaces() -> tuple:
    """Every workspace, in persona declaration order."""
    return tuple(_WORKSPACES[k] for k in personas.VALID_PERSONAS if k in _WORKSPACES)


# ── describing one ────────────────────────────────────────────────────────────

def _cloud_home() -> str:
    """The first cloud console with credentials, or AWS's when none has any.

    With none configured the step resolves `needs_flag` (it requires every cloud any-of),
    so the AWS fallback is never rendered as a live link pointing at nothing."""
    for cloud in _ALL_CLOUDS:
        if feature_flags.cloud_configured(cloud):
            return CLOUD_HOME[cloud]
    return CLOUD_HOME["aws"]


def step_job_types(ws: Workspace) -> tuple:
    """Every job type that can tick one of ``ws``'s steps."""
    out = []
    for s in ws.steps:
        for t in s.done_job_types:
            if t not in out:
                out.append(t)
    return tuple(out)


def describe_step(step: Step, done: bool) -> dict:
    card = step.card
    if step.cloud_home:
        card = UseCase(**{**card.__dict__, "target": _cloud_home()})
    out = describe_card(card)
    out.pop("minutes", None)
    out["checkable"] = step.checkable
    out["done"] = bool(done) and step.checkable
    return out


def step_done(step: Step, completed_types: set, scheduled_types: set) -> bool:
    """Whether the user's history ticks ``step``.

    ``completed_types``: job types of the user's own completed jobs. ``scheduled_types``:
    job types of schedules the user owns. Pure, so the rule is testable without a DB.
    """
    if step.done_schedule:
        # A schedule step is about making something RECURRING, so only a schedule ticks
        # it: the one-off job of the same type is what the step before it asked for.
        if not step.done_job_types:
            return bool(scheduled_types)
        return any(t in scheduled_types for t in step.done_job_types)
    return any(t in completed_types for t in step.done_job_types)


def resource_href(item: dict) -> str:
    """Where one inventory item is managed."""
    kind = item.get("kind") or ""
    cloud = item.get("cloud") or ""
    if kind == "vm":
        return CLOUD_HOME.get(cloud) or _HYPERVISOR_HOME.get(cloud) or "/inventory"
    return _KIND_HOME.get(kind, "/inventory")


def select_resources(ws: Workspace, items: list) -> list:
    """The items (already RBAC-filtered by the caller) this workspace lists, newest first."""
    picked = [i for i in items
              if (not ws.resource_kinds or i.get("kind") in ws.resource_kinds)
              and (not ws.resource_clouds or i.get("cloud") in ws.resource_clouds)]
    picked.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return picked


def describe(key: str, *, completed_types=(), scheduled_types=(), resources=None,
             recent_jobs=None) -> dict:
    """The payload ``GET /api/workspace/summary`` serves for one persona.

    The caller supplies the user's history and their RBAC-filtered inventory; this module
    reads no database, so the API layer owns every query and the rules here stay pure.
    """
    ws = get(key)
    persona = personas.get(key)
    if ws is None or persona is None:
        return {"persona": "", "chooser": chooser()}
    completed, scheduled = set(completed_types), set(scheduled_types)
    steps = [describe_step(s, step_done(s, completed, scheduled)) for s in ws.steps]
    checkable = [s for s in steps if s["checkable"]]
    resources = resources or []
    return {
        "persona": persona.key,
        "label": persona.label,
        "blurb": persona.blurb,
        "headline": ws.headline,
        "steps": steps,
        "progress": {"done": sum(1 for s in checkable if s["done"]),
                     "total": len(checkable)},
        "toolkit": [{"label": label, "links": [describe_card(link) for link in links]}
                    for label, links in ws.toolkit],
        "resources": [dict(i, href=resource_href(i)) for i in resources[:RESOURCE_CAP]],
        "resources_total": len(resources),
        "resources_link": "/inventory",
        "recent_jobs": list(recent_jobs or []),
        "guides": [describe_card(c) for c in persona.use_cases],
        "docs": [f"/docs/{d}" for d in persona.docs],
    }


def chooser() -> list:
    """``[{key, label, blurb, headline}]`` for every workspace, for the picker shown to a
    user with no persona. Empty on an instance with no persona axis."""
    if not personas.applies():
        return []
    return [{"key": p.key, "label": p.label, "blurb": p.blurb,
             "headline": _WORKSPACES[p.key].headline}
            for p in personas.all_personas() if p.key in _WORKSPACES]
