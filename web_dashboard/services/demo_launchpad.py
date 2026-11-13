"""The Demo hub's launchpad: every page a demo on an estate instance runs from.

A list of links, grouped, and nothing else. Each one is a ``personas.UseCase`` so it is
resolved by ``personas.describe_card`` -- the SAME reader the Use cases page uses -- into
``ready`` / ``needs_flag`` / ``masked``. A second resolver here would be a second answer to
"does this page exist on this instance?", which is the question services/feature_flags
insists has exactly one.

The rule that makes this a hub rather than a gate: like /use-cases, it may only ever
SURFACE. An entry whose page this instance cannot serve is shown masked, with no href;
it is never dropped, and nothing here 404s anything.

Adding a page: add an entry, with the flag its route is gated on. tests/test_demo_hub.py
checks every target is a real HTML route.
"""
from __future__ import annotations

from .personas import UseCase, describe_card

_WORKLOAD_LAB = ("cert_lab_enabled", "spire_lab_enabled", "agentcell_enabled")
_HYPERVISORS = ("vmware_enabled", "proxmox_enabled", "vsphere_enabled",
                "hyperv_enabled", "nutanix_enabled", "xcpng_enabled")


def _link(id, title, summary, target, **requires) -> UseCase:
    return UseCase(id=id, title=title, summary=summary, target=target, minutes=0,
                   **requires)


LAUNCHPAD = (
    ("Demo cells", (
        _link("ot_cell", "OT demo cell",
              "A simulated plant -- HMI and PLC protocols -- reached only through PRA.",
              "/gcp#ot", requires_flags=("pra_enabled",),
              requires_clouds=("aws", "azure", "gcp")),
        _link("net_cell", "Network demo cell",
              "A VyOS router with no inbound rule, managed through PRA and Password Safe.",
              "/gcp#net", requires_flags=("netcell_enabled",), requires_clouds=("gcp",)),
        _link("agent_cell", "Agent demo cell",
              "A non-human worker with a SPIFFE identity and a token that expires.",
              "/workload-lab#agent", requires_flags=("agentcell_enabled",)),
    )),
    ("Clouds", (
        _link("aws", "AWS", "Deploy and onboard EC2 instances.", "/aws",
              requires_clouds=("aws",)),
        _link("azure", "Azure", "Deploy and onboard Azure VMs.", "/azure",
              requires_clouds=("azure",)),
        _link("gcp", "GCP", "Deploy and onboard Compute Engine instances.", "/gcp",
              requires_clouds=("gcp",)),
        _link("oci", "OCI", "Deploy and onboard OCI instances.", "/oci",
              requires_clouds=("oci",)),
    )),
    ("Labs and platform", (
        _link("workload_lab", "Workload Lab",
              "Machine identities: certificates, SPIRE, Kubernetes and cloud credentials.",
              "/workload-lab", requires_any_flag=_WORKLOAD_LAB),
        _link("k8s", "Kubernetes", "Clusters and their privileged access.", "/k8s",
              requires_flags=("k8s_management_enabled",)),
        _link("databases", "Databases", "Cloud databases onboarded into Password Safe.",
              "/databases", requires_flags=("cloud_database_enabled",)),
        _link("functions", "Functions", "Cloud functions and the secrets they read.",
              "/functions", requires_flags=("cloud_functions_enabled",)),
        _link("directories", "Directories", "Managed Active Directory on AWS and GCP.",
              "/directories", requires_flags=("directories_enabled",)),
        _link("desktops", "Desktops", "Virtual desktops.", "/desktops",
              requires_flags=("vdesktops_enabled",)),
        _link("containers", "Containers", "Container hosts.", "/containers"),
        _link("config_mgmt", "Configuration", "Ansible runs against the estate.",
              "/config-mgmt", requires_flags=("ansible_enabled",)),
    )),
    ("Hypervisors", (
        _link("vms", "Workstation", "Local VMware Workstation VMs.", "/vms",
              requires_flags=("vmware_enabled",)),
        _link("proxmox", "Proxmox", "Proxmox VE.", "/proxmox",
              requires_flags=("proxmox_enabled",)),
        _link("vsphere", "vSphere", "VMware vSphere.", "/vsphere",
              requires_flags=("vsphere_enabled",)),
        _link("hyperv", "Hyper-V", "Microsoft Hyper-V.", "/hyperv",
              requires_flags=("hyperv_enabled",)),
        _link("nutanix", "Nutanix", "Nutanix AHV.", "/nutanix",
              requires_flags=("nutanix_enabled",)),
        _link("xcpng", "XCP-ng", "XCP-ng.", "/xcpng", requires_flags=("xcpng_enabled",)),
        _link("connections", "Connections", "Where each hypervisor lives and how to reach it.",
              "/connections", requires_any_flag=_HYPERVISORS),
    )),
    ("Scripts and records", (
        _link("use_cases", "Use cases", "The guided demo scripts, grouped by role.",
              "/use-cases"),
        _link("inventory", "Inventory", "Everything the dashboard has deployed.",
              "/inventory"),
        _link("jobs", "Jobs", "What ran, what is running, and what failed.", "/jobs"),
    )),
)


def describe() -> list:
    """``[{"label", "links": [card, ...]}, ...]``, every entry kept, masked or not."""
    return [{"label": label, "links": [describe_card(link) for link in links]}
            for label, links in LAUNCHPAD]
