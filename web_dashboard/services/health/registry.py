"""Every health area, in the order the Health page lists them by default.

Adding an area is adding a :class:`~.core.Collector` here and a module under
``collectors/``. ``tests/test_health_framework.py`` holds every entry to the contract:
a unique area id, real feature flags, real persona keys, and an interval.
"""
from __future__ import annotations

from typing import Optional

from .collectors import (agents, bt_tenants, cloud_ops, directories, entitle, hypervisors,
                         pra_gateways, ps_accounts, resources, workloads)
from .core import Collector

COLLECTORS = (
    Collector(
        area="resources", label="Resources", collect=resources.collect,
        interval_minutes=30,
        personas=("cloudops", "devops", "dba", "sre", "finops", "hypervisor", "itops",
                  "aiops"),
        description="Everything the dashboard built or registered: whether the cloud still "
                    "has it and it is running, and its Password Safe, PRA and Entitle "
                    "wiring."),
    Collector(
        area="cloud_ops", label="Cloud APIs and spend", collect=cloud_ops.collect,
        interval_minutes=15, personas=("cloudops", "finops"),
        description="Whether the dashboard's own cloud credentials are answering, cost data "
                    "is refreshing, and spend caps and the auto-delete timer are working."),
    Collector(
        area="hypervisors", label="Hypervisors", collect=hypervisors.collect,
        interval_minutes=15,
        flags=("vsphere_enabled", "proxmox_enabled", "hyperv_enabled", "nutanix_enabled",
               "xcpng_enabled"),
        personas=("hypervisor", "itops"),
        description="Whether each hypervisor connection answers, its hosts and nodes are up, "
                    "Proxmox storage has room, and agent-run syncs are keeping up."),
    Collector(
        area="beyondtrust", label="BeyondTrust tenants", collect=bt_tenants.collect,
        interval_minutes=30, flags=("pra_enabled", "password_safe_enabled"),
        personas=("cloudops", "devops", "hypervisor", "itops", "ot", "dba", "sre",
                  "netadmin", "security", "idarch"),
        description="Whether each PRA and Password Safe credential still gets a token."),
    Collector(
        area="pra_gateways", label="PRA Gateways", collect=pra_gateways.collect,
        interval_minutes=15, flags=("pra_enabled",),
        personas=("cloudops", "hypervisor", "itops", "ot", "dba", "sre", "netadmin"),
        description="Whether each Gateway has a connected node, and each Gateway host the "
                    "dashboard deployed is running."),
    Collector(
        area="password_safe", label="Password Safe accounts", collect=ps_accounts.collect,
        interval_minutes=60, flags=("password_safe_enabled",),
        personas=("dba", "devops", "sre", "hypervisor", "iamops", "security"),
        description="Whether every account the dashboard onboarded still exists and is "
                    "rotating on schedule."),
    Collector(
        area="entitle", label="Entitle", collect=entitle.collect,
        interval_minutes=15, flags=("entitle_enabled",),
        personas=("cloudops", "finops", "dba", "security", "idarch"),
        description="Whether just-in-time access is reconciling, and the virtual "
                    "applications and bundles the dashboard applied."),
    Collector(
        area="workloads", label="Workload credentials", collect=workloads.collect,
        interval_minutes=30,
        flags=("workload_credentials_enabled", "password_safe_enabled", "spire_lab_enabled",
               "agentcell_enabled", "mcp_server_enabled"),
        personas=("sre", "devops", "aiops", "security"),
        description="Whether the credentials the dashboard hands to workloads are being "
                    "issued and rotated: Workload Credentials leases, Kubernetes "
                    "ServiceAccount tokens in Password Safe, SPIRE labs, agent cells and "
                    "integration tokens."),
    Collector(
        area="remote_agents", label="Remote agents", collect=agents.collect,
        interval_minutes=5, flags=("remote_agents_enabled",),
        personas=("hypervisor", "itops", "ot", "netadmin", "iamops"),
        description="Whether each agent is polling, and new enough for what is bound to it."),
    Collector(
        area="directories", label="Directories", collect=directories.collect,
        interval_minutes=5, flags=("directories_enabled",),
        personas=("iamops", "idarch"),
        description="The Directory health page's results."),
)


def get(area: str) -> Optional[Collector]:
    return next((c for c in COLLECTORS if c.area == area), None)
