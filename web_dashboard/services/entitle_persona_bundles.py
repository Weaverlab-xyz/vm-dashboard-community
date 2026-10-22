"""Persona × cloud access bundles in Entitle. **This module grants access**; it is not
``personas``.

``services/personas`` is curation only by contract: a persona may reorder the dashboard
and never hide or grant anything, which is what lets it be chosen from a cookie. A bundle
is the opposite — requesting one puts a person in groups and cloud roles — so the
catalog lives here, imports nothing from ``personas``, and only shares its persona KEYS
(``tests/test_entitle_persona_bundles`` pins that every key here is a real persona).

Each bundle is one Entitle request for one job function on one cloud, built from three
components, each resolved against the live tenant:

  ``group``      the virtual-app role over the directory group ``ent-<cloud>-<key>`` —
                 the group the cloud's SSO maps to a permission set / role / binding.
                 Found among active virtual apps (``entitle_virtual_app_service``).
  ``cloud``      roles in the cloud's own Entitle integration (AWS, Azure, GCP), pinned
                 per cloud in Settings (``entitle_cloud_integration_<cloud>``), matched by
                 role name or external id. OCI has none: Entitle grants OCI through IAM
                 group membership, which is the ``group`` component — the catalog's
                 ``oci_policy`` lines say what that group's policy should allow.
  ``dashboard``  the dashboard's own ``dashboard-<scope>-<level>`` groups that
                 ``bootstrap_entitle_groups`` created, matched tenant-wide by group name.

A component is ``resolved`` (every part found), ``absent`` (nothing configured or nothing
found — left out), or ``partial`` (some parts found, some not). **A partial component
blocks its bundle**: a bundle that silently granted three of the four things its name
promises is worse than no bundle. A bundle also needs cloud access — the ``group`` or
``cloud`` component resolved — or it would grant only dashboard permissions under a
cloud's name. Nothing is ever guessed: an ambiguous match (one role name on several
subscriptions, one group name in two directories) is reported, not picked.

Durations are the tier's own (:mod:`entitle_tiers`), capped per bundle, because the tier
workflows' single rule stops at the tier's longest duration — a longer bundle request
would match no rule.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import entitle_tiers
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)

JOB_TYPE = "entitle_persona_bundles"
CLOUDS = ("aws", "azure", "gcp", "oci")
CLOUD_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI"}
# On-premises Active Directory is a target alongside the clouds: the IT, OT and
# hypervisor personas work there, through AD groups that are delegated rights on OUs,
# jump hosts and hypervisor identity sources.
AD = "ad"
TARGETS = CLOUDS + (AD,)
TARGET_LABELS = {**CLOUD_LABELS, AD: "On-prem AD"}
CATEGORY = "Cloud access"
CATEGORIES = {**{c: "Cloud access" for c in CLOUDS}, AD: "On-prem access"}
GROUP_PREFIX = "ent"

# Directory kinds that are Active Directory (or LDAP with AD-style groups) for the AD
# target. Their pinned Entitle integration is where the AD component is resolved.
AD_DIRECTORY_PROVIDERS = ("onprem_ad", "aws_managed_ad", "aws_ad_connector",
                          "aws_simple_ad", "gcp_managed_ad", "azure_managed_ad", "ldap")

# Built-in AD groups that are tier 0, or that reach domain controllers. A requestable
# bundle must never grant one: membership is control of the domain, and Entitle would
# hand it out on a single request. The catalog is pinned against this list by a test,
# and resolution refuses a match on it at run time as well, so a group renamed to one of
# these in a customer's AD is still not picked up.
PRIVILEGED_AD_GROUPS = frozenset(g.casefold() for g in (
    "Administrators", "Domain Admins", "Enterprise Admins", "Schema Admins",
    "Account Operators", "Server Operators", "Backup Operators", "Print Operators",
    "DnsAdmins", "Group Policy Creator Owners", "Cert Publishers", "Key Admins",
    "Enterprise Key Admins", "Domain Controllers", "Read-only Domain Controllers",
    "Remote Desktop Users", "Remote Management Users", "Hyper-V Administrators",
    "Incoming Forest Trust Builders", "Replicator", "Storage Replica Administrators",
))


@dataclass(frozen=True)
class PersonaAccess:
    """One job function's access, on every cloud.

    ``key`` is a persona key, or ``<persona>-<variant>`` for a second level of the same
    role (``sre-breakglass``); :attr:`persona` is the part before the dash. Role names per
    cloud are what that cloud's Entitle integration calls them — AWS managed policy or
    permission-set names, Azure built-in role names, GCP role ids (matched against the
    role's external id as well as its name).
    """
    key: str
    label: str
    purpose: str
    tier: str
    max_duration: int
    aws: tuple = ()
    azure: tuple = ()
    gcp: tuple = ()
    oci_policy: tuple = ()          # documentation for the ent-oci-<key> group's policy
    dashboard: tuple = ()           # "{cloud}" is replaced by the cloud key
    targets: tuple = CLOUDS         # where this job function has a bundle
    # Further delegated AD groups for the AD target, beyond ent-ad-<key> itself. Your
    # own groups only — never a built-in (PRIVILEGED_AD_GROUPS).
    ad_extra: tuple = ()

    @property
    def persona(self) -> str:
        return self.key.split("-", 1)[0]

    def cloud_roles(self, cloud: str) -> tuple:
        """Role names for the target's own integration. For AD these are GROUP names —
        the persona's ent-ad-<key> group first — matched as resources in the AD
        directory's Entitle integration."""
        if cloud == AD:
            return (group_name(self.key, AD),) + tuple(self.ad_extra)
        return getattr(self, cloud) if cloud in ("aws", "azure", "gcp") else ()

    def dashboard_groups(self, cloud: str) -> tuple:
        return tuple(g.format(cloud=cloud) for g in self.dashboard)

    def durations(self) -> tuple:
        return tuple(d for d in entitle_tiers.TIER_DURATIONS[self.tier]
                     if d <= self.max_duration)


CATALOG = (
    PersonaAccess(
        key="cloudops", label="Cloud Ops",
        purpose="Run and fix compute: start, stop, resize and rebuild instances, read "
                "everything else.",
        tier="single_approver", max_duration=43200,
        aws=("AmazonEC2FullAccess", "ReadOnlyAccess"),
        azure=("Virtual Machine Contributor", "Reader"),
        gcp=("roles/compute.instanceAdmin.v1", "roles/viewer"),
        oci_policy=("manage instance-family in compartment <c>",
                    "read all-resources in compartment <c>"),
        dashboard=("dashboard-{cloud}-write", "dashboard-images-write"),
    ),
    PersonaAccess(
        key="devops", label="DevOps",
        purpose="Ship to non-production: clusters, registries and pipelines, without "
                "identity administration.",
        tier="single_approver", max_duration=43200,
        aws=("PowerUserAccess",),
        azure=("Contributor", "Azure Kubernetes Service Cluster User Role"),
        gcp=("roles/container.developer", "roles/artifactregistry.writer"),
        oci_policy=("manage cluster-family in compartment <c>",
                    "manage repos in compartment <c>"),
        dashboard=("dashboard-{cloud}-read", "dashboard-k8s-write",
                   "dashboard-cloud_function-write", "dashboard-config_mgmt-write"),
    ),
    PersonaAccess(
        key="dba", label="DBA",
        purpose="Administer managed databases. Pairs with, not instead of, the "
                "per-database ephemeral accounts the dashboard registers.",
        tier="single_approver", max_duration=43200,
        aws=("AmazonRDSFullAccess",),
        azure=("SQL DB Contributor", "SQL Server Contributor"),
        gcp=("roles/cloudsql.admin",),
        oci_policy=("manage database-family in compartment <c>",
                    "manage autonomous-database-family in compartment <c>"),
        dashboard=("dashboard-{cloud}-read", "dashboard-cloud_database-write"),
    ),
    PersonaAccess(
        key="security", label="Security / IAM",
        purpose="Audit configuration and identity across the account, read-only.",
        tier="auto_approve", max_duration=3600,
        aws=("SecurityAudit",),
        azure=("Security Reader",),
        gcp=("roles/iam.securityReviewer",),
        oci_policy=("inspect all-resources in tenancy", "read audit-events in tenancy"),
        dashboard=("dashboard-{cloud}-read", "dashboard-audit-read",
                   "dashboard-inventory-read"),
    ),
    PersonaAccess(
        key="sre", label="SRE",
        purpose="Operate: read everything, own monitoring and alerting.",
        tier="auto_approve", max_duration=3600,
        aws=("ReadOnlyAccess", "CloudWatchFullAccess"),
        azure=("Monitoring Contributor", "Reader"),
        gcp=("roles/monitoring.editor", "roles/logging.viewer"),
        oci_policy=("read all-resources in compartment <c>",
                    "manage alarms in compartment <c>"),
        dashboard=("dashboard-{cloud}-read", "dashboard-k8s-read",
                   "dashboard-inventory-read"),
    ),
    PersonaAccess(
        key="sre-breakglass", label="SRE break-glass",
        purpose="Incident-only administrator access, one hour at most, two approvers.",
        tier="two_approver", max_duration=3600,
        aws=("AdministratorAccess",),
        azure=("Contributor",),
        gcp=("roles/editor",),
        oci_policy=("manage all-resources in compartment <c>",),
        dashboard=("dashboard-{cloud}-delete",),
    ),
    PersonaAccess(
        key="netadmin", label="Network admin",
        purpose="VPCs / VNets, routing, firewalls and load balancers.",
        tier="single_approver", max_duration=43200,
        aws=("NetworkAdministrator",),
        azure=("Network Contributor",),
        gcp=("roles/compute.networkAdmin", "roles/compute.securityAdmin"),
        oci_policy=("manage virtual-network-family in compartment <c>",),
        dashboard=("dashboard-{cloud}-read", "dashboard-gateways-write"),
    ),
    PersonaAccess(
        key="finops", label="FinOps",
        purpose="Cost, budgets and recommendations; no resource changes.",
        tier="auto_approve", max_duration=3600,
        aws=("Billing", "AWSBudgetsReadOnlyAccess"),
        azure=("Cost Management Contributor",),
        gcp=("roles/billing.viewer", "roles/recommender.viewer"),
        oci_policy=("read usage-reports in tenancy", "manage usage-budgets in tenancy"),
        dashboard=("dashboard-{cloud}-read", "dashboard-costs-write"),
    ),
    PersonaAccess(
        key="aiops", label="AI platform",
        purpose="Build on the cloud's managed AI services.",
        tier="single_approver", max_duration=43200,
        aws=("AmazonBedrockFullAccess",),
        azure=("Azure AI Developer",),
        gcp=("roles/aiplatform.user",),
        oci_policy=("manage generative-ai-family in compartment <c>",),
        dashboard=("dashboard-{cloud}-read", "dashboard-cloud_function-write"),
    ),
    # ── On-premises, through Active Directory ────────────────────────────────
    # Each fronts the persona's own delegated group, ent-ad-<key>, which carries the
    # rights where they are needed: delegated control of an OU, local admin on a set of
    # jump hosts, the admin role in vCenter's AD identity source. None is a built-in.
    PersonaAccess(
        key="itops", label="IT engineer",
        purpose="Endpoints and servers: the delegated rights on their OUs (join, reset, "
                "LAPS read) and the dashboard's VM and configuration pages.",
        tier="single_approver", max_duration=43200, targets=(AD,),
        dashboard=("dashboard-vms-write", "dashboard-config_mgmt-write",
                   "dashboard-directories-read"),
    ),
    PersonaAccess(
        key="ot", label="OT / ICS engineer",
        purpose="Plant access: the group the OT jump hosts and engineering workstations "
                "admit, for a few hours at a time.",
        tier="single_approver", max_duration=10800, targets=(AD,),
        dashboard=("dashboard-ot-write", "dashboard-connections-read"),
    ),
    PersonaAccess(
        key="ot-breakglass", label="OT break-glass",
        purpose="Incident-only control of the OT cell, one hour at most, two approvers.",
        tier="two_approver", max_duration=3600, targets=(AD,),
        dashboard=("dashboard-ot-delete", "dashboard-connections-read"),
    ),
    PersonaAccess(
        key="hypervisor", label="Hypervisor admin",
        purpose="The virtualisation layer: the group vCenter, Hyper-V and the other "
                "hypervisors map to their admin role through AD.",
        tier="single_approver", max_duration=43200, targets=(AD,),
        dashboard=("dashboard-vsphere-write", "dashboard-hyperv-write",
                   "dashboard-proxmox-write", "dashboard-nutanix-write",
                   "dashboard-xcpng-write"),
    ),
)

_BY_KEY = {p.key: p for p in CATALOG}


# ── Delegation: what gives each ent-* group its rights ───────────────────────
#
# Creating a group grants nothing. These are the steps that make ent-<target>-<persona>
# mean something, shown per bundle on the settings card and printed in full in
# docs/integrations/beyondtrust/entitle-persona-bundles.md#delegating-rights. One source
# for both, so they cannot disagree. "{group}" is the bundle's group name.

AD_DELEGATION = {
    "itops": (
        "Delegate on the user OUs: Reset password, and Read/Write pwdLastSet and "
        "lockoutTime — `dsacls \"OU=Users,DC=corp,DC=example,DC=com\" /I:S /G "
        "\"CORP\\{group}:CA;Reset Password;user\"`.",
        "Delegate on the server and workstation OUs: Create/Delete Computer objects, and "
        "Validated write to DNS host name and service principal name.",
        "Let the group read Windows LAPS passwords on those OUs: "
        "`Set-LapsADReadPasswordPermission -Identity \"OU=Servers,DC=corp,DC=example,DC=com\" "
        "-AllowedPrincipals \"CORP\\{group}\"`.",
        "Make the group local Administrators on the managed servers with a GPO (Computer "
        "Configuration → Preferences → Local Users and Groups) linked only to those OUs.",
        "Never nest it in Domain Admins, Account Operators or Server Operators.",
    ),
    "ot": (
        "Add the group to the local Remote Desktop Users group on the OT jump hosts and "
        "engineering workstations, with a GPO linked only to the OT OU.",
        "In that GPO grant \"Allow log on through Remote Desktop Services\" to the group; on "
        "the rest of the domain deny it (\"Deny log on through Remote Desktop Services\").",
        "In Privileged Remote Access, map the group to the OT asset group's group policy, so "
        "PRA sessions follow the same membership.",
    ),
    "ot-breakglass": (
        "Add the group to the local Administrators group on the OT jump hosts and "
        "engineering workstations, with a GPO linked only to the OT OU.",
        "Allow RDP for it there as for ent-ad-ot; deny interactive logon everywhere else.",
        "Alert on any membership change of this group (event 4728/4729 on the DCs).",
    ),
    "hypervisor": (
        "vCenter: add AD as an identity source (Administration → Single Sign On → "
        "Configuration), then Administration → Global Permissions → add CORP\\{group} with "
        "the Administrator role and \"Propagate to children\".",
        "Hyper-V: add the group to each host's LOCAL Hyper-V Administrators group with a "
        "GPO linked to the Hyper-V hosts' OU — not to the domain built-in group.",
        "Proxmox: Datacenter → Permissions → Realms → add an Active Directory realm, sync "
        "groups, then give {group} the Administrator role on path /.",
        "Nutanix Prism Central: Authentication → add the AD directory, then Role Mapping → "
        "{group} → Cluster Admin.",
        "XCP-ng (Xen Orchestra): Settings → Plugins → auth-ldap pointed at AD, then give "
        "{group} the admin permission.",
    ),
}

CLOUD_DELEGATION = {
    "aws": (
        "If AD is your IAM Identity Center source (AD Connector / Managed AD) or syncs to "
        "your IdP, wait for {group} to appear in Identity Center.",
        "IAM Identity Center → AWS accounts → the account(s) → Assign users or groups → "
        "{group} → a permission set holding: {roles}.",
    ),
    "azure": (
        "Let Entra Connect / Cloud Sync carry {group} to Entra ID (or create it there).",
        "Subscription or resource group → Access control (IAM) → Add role assignment → "
        "{roles} → assign to the group {group}.",
    ),
    "gcp": (
        "Sync {group} to your Workforce Identity pool's IdP (Entra, Okta) or to Cloud "
        "Identity with Google Cloud Directory Sync.",
        "Grant the roles to the group: `gcloud projects add-iam-policy-binding PROJECT "
        "--member=\"principalSet://iam.googleapis.com/locations/global/workforcePools/POOL/"
        "group/{group}\" --role=ROLE` for each of: {roles}.",
    ),
    "oci": (
        "Federate the OCI identity domain with your IdP (or AD through it) and map "
        "{group} to an identity-domain group of the same name.",
        "Identity → Policies → a policy allowing the group: {roles}.",
    ),
}


def delegation_steps(p: "PersonaAccess", target: str) -> tuple:
    """The steps that give this bundle's group its rights, with names filled in."""
    group = group_name(p.key, target)
    if target == AD:
        steps = AD_DELEGATION.get(p.key, ())
        roles = ""
    else:
        steps = CLOUD_DELEGATION.get(target, ())
        roles = "; ".join(p.oci_policy if target == "oci" else p.cloud_roles(target))
    return tuple(s.replace("{group}", group).replace("{roles}", roles) for s in steps)


# ── Creating the groups in on-prem AD ────────────────────────────────────────
#
# Only names this catalog produces, only on an on-prem AD reached by its remote agent,
# and only ever created — never given members, never deleted. The playbook
# (builtin_playbooks/ad-ensure-groups.yml) re-checks the name pattern on the agent side.

GROUP_JOB_ASSET = "builtin:ad-ensure-groups"
GROUP_NAME_RE = r"^ent-(ad|aws|azure|gcp|oci)-[a-z0-9-]+$"
_OU_RE = r"^(OU|CN)=[^,\"'\r\n\;<>]+(,(OU|CN|DC)=[^,\"'\r\n\;<>]+)*$"


class GroupCreationError(Exception):
    """A request the operator can fix. The message is shown as-is."""


def plan_groups(keys: list, *, include_cloud: bool) -> list:
    """``[{name, description, target, persona}]`` to ensure, from the selected bundle keys.
    The AD target's groups always; a cloud target's only with ``include_cloud`` (for AD
    that syncs to the IdP). Unknown keys are ignored; duplicates collapse."""
    import re
    out, seen = [], set()
    for k in dict.fromkeys(keys or ()):
        access, _, target = str(k).rpartition("-")
        p = _BY_KEY.get(access)
        if not p or target not in p.targets:
            continue
        if target != AD and not include_cloud:
            continue
        names = [group_name(p.key, target)]
        if target == AD:
            names += list(p.ad_extra)
        for name in names:
            name = name.lower()
            if name in seen:
                continue
            if not re.match(GROUP_NAME_RE, name) or name.casefold() in PRIVILEGED_AD_GROUPS:
                raise GroupCreationError(f"{name} is not a group this dashboard may create")
            seen.add(name)
            out.append({"name": name, "target": target, "persona": p.key,
                        "description": (f"{p.label} — {TARGET_LABELS[target]}: {p.purpose} "
                                        "Created by VM Dashboard for an Entitle bundle; "
                                        "rights are delegated separately.")[:1024]})
    return out


def onprem_ad_directories(db) -> list:
    """On-prem AD rows a group-creation run can target: registered, with an agent."""
    from ..database import ManagedDirectory
    rows = (db.query(ManagedDirectory)
            .filter(ManagedDirectory.cloud == "local",
                    ManagedDirectory.provider == "onprem_ad",
                    ManagedDirectory.status == "available",
                    ManagedDirectory.agent_id.isnot(None)).all())
    return [{"id": r.id, "name": r.name, "host": r.host or ""} for r in rows]


def _domain_dn(domain: str) -> str:
    return ",".join(f"DC={part}" for part in (domain or "").split(".") if part)


def check_ou(ou: str, domain: str) -> str:
    """The OU DN, cleaned, or "" for the default container. Refuses anything that is not
    a plain DN inside this domain."""
    import re
    ou = (ou or "").strip()
    if not ou:
        return ""
    if len(ou) > 512 or not re.match(_OU_RE, ou, re.I):
        raise GroupCreationError("The OU must be a distinguished name such as "
                                 "OU=Entitle,OU=Groups,DC=corp,DC=example,DC=com.")
    if not ou.lower().endswith(_domain_dn(domain).lower()):
        raise GroupCreationError(f"The OU must be inside {domain} "
                                 f"(end with {_domain_dn(domain)}).")
    return ou


def queue_group_creation(db, *, directory_id: str, keys: list, include_cloud: bool,
                         ou: str, created_by: str) -> dict:
    """Queue one ``agent_ansible`` run of ``ad-ensure-groups`` on the directory's agent.
    Returns ``{job_id, groups, directory}``."""
    from . import agent_ansible_meta, agent_service, job_service
    from ..database import ManagedDirectory, RemoteAgent
    row = db.query(ManagedDirectory).filter(ManagedDirectory.id == directory_id).first()
    if not row or row.status == "deleted":
        raise GroupCreationError("That directory is not registered.")
    if row.cloud != "local" or row.provider != "onprem_ad":
        raise GroupCreationError(
            f"{row.name} is not an on-premises Active Directory reached through a remote "
            "agent. Create the groups there with its own tools (AWS Managed AD: the "
            "delegated admin account; GCP / Entra Domain Services: their admin consoles).")
    if not row.host:
        raise GroupCreationError(f"{row.name} has no domain controller host recorded.")
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == (row.agent_id or ""),
                                         RemoteAgent.is_active.is_(True)).first()
    if not agent:
        raise GroupCreationError(f"{row.name} has no active remote agent.")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise GroupCreationError(f"Agent '{agent.name}' is not granted Config Management, "
                                 "which runs the group creation.")
    if not agent_service.supports_directory(agent):
        raise GroupCreationError(agent_service.directory_upgrade_hint(agent))
    groups = plan_groups(keys, include_cloud=include_cloud)
    if not groups:
        raise GroupCreationError("None of the selected bundles has a group to create here "
                                 "(tick 'also cloud groups' for the cloud bundles).")
    path = check_ou(ou, row.name)
    meta = agent_ansible_meta.normalize({
        "description": f"Create {len(groups)} Entitle group(s) in {row.name} "
                       f"(agent: {agent.name})",
        "run_kind": "directory", "transport": "winrm",
        "target_host": row.host, "target_port": 5986,
        "target_id": row.id, "target_label": f"onprem_ad/{row.name}",
        "asset": GROUP_JOB_ASSET, "asset_backend": "",
        "extra_vars": {"ad_groups": [{"name": g["name"], "description": g["description"]}
                                     for g in groups],
                       "ad_groups_path": path},
    })
    problem = agent_ansible_meta.check(meta)
    if problem:
        raise GroupCreationError(problem)
    job = job_service.create_job(db, job_type="agent_ansible", created_by=created_by,
                                 workgroup="ansible", metadata=meta, agent_id=agent.id)
    return {"job_id": job.id, "groups": groups, "directory": row.name, "ou": path}


def bundle_key(access_key: str, cloud: str) -> str:
    return f"{access_key}-{cloud}"


def group_name(access_key: str, cloud: str) -> str:
    return f"{GROUP_PREFIX}-{cloud}-{access_key}"


def bundle_name(p: PersonaAccess, cloud: str) -> str:
    return f"{p.label} — {TARGET_LABELS[cloud]}"


def catalog() -> list:
    """Every bundle the catalog defines, flat, for the settings card."""
    out = []
    for p in CATALOG:
        for cloud in p.targets:
            out.append({
                "key": bundle_key(p.key, cloud), "access": p.key, "persona": p.persona,
                "cloud": cloud, "name": bundle_name(p, cloud), "label": p.label,
                "purpose": p.purpose, "tier": p.tier,
                "tier_label": entitle_tiers.TIER_LABELS[p.tier],
                "durations": list(p.durations()), "group": group_name(p.key, cloud),
                "cloud_roles": list(p.cloud_roles(cloud)),
                "oci_policy": list(p.oci_policy) if cloud == "oci" else [],
                "dashboard_groups": list(p.dashboard_groups(cloud)),
                "category": CATEGORIES[cloud],
                "delegation": list(delegation_steps(p, cloud)),
            })
    return out


def suggest_for_group(group: str) -> Optional[dict]:
    """Name a virtual-app role for a directory group that follows ``ent-<cloud>-<key>``.
    ``None`` for any other group — no guessing at groups named some other way."""
    g = (group or "").strip().lower()
    for cloud in TARGETS:
        prefix = f"{GROUP_PREFIX}-{cloud}-"
        key = g[len(prefix):]
        if g.startswith(prefix) and key in _BY_KEY and cloud in _BY_KEY[key].targets:
            p = _BY_KEY[key]
            return {"name": bundle_name(p, cloud)[:50], "resource": TARGET_LABELS[cloud],
                    "tier": p.tier, "persona": p.key, "cloud": cloud}
    return None


# ── Resolution against the tenant ────────────────────────────────────────────

def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "")


def cloud_settings(cfg=None) -> dict:
    cfg = cfg or _cfg
    return {c: {"integration_id": (cfg(f"entitle_cloud_integration_{c}") or "").strip(),
                "resource": (cfg(f"entitle_cloud_resource_{c}") or "").strip()}
            for c in TARGETS}


def ad_directories(db) -> list:
    """AD / LDAP directories with an Entitle integration pinned: the candidates for the
    AD target. ``[{integration_id, integration_name, directory_id, name, provider}]``."""
    from ..database import ManagedDirectory
    rows = (db.query(ManagedDirectory)
            .filter(ManagedDirectory.provider.in_(AD_DIRECTORY_PROVIDERS),
                    ManagedDirectory.status != "deleted",
                    ManagedDirectory.entitle_integration_id.isnot(None)).all())
    return [{"integration_id": r.entitle_integration_id,
             "integration_name": r.entitle_integration_name or "",
             "directory_id": r.id, "name": r.name, "provider": r.provider}
            for r in rows if r.entitle_integration_id]


def _ad_integration(db, pinned: str) -> tuple:
    """``(integration_id, "")`` or ``("", reason)`` for the AD target. An explicit pin
    wins; otherwise the one AD directory with an integration pinned, and never a guess
    between several."""
    if pinned:
        return pinned, ""
    found = {d["integration_id"]: d for d in ad_directories(db)}
    if len(found) == 1:
        return next(iter(found)), ""
    if not found:
        return "", ""
    names = ", ".join(sorted(d["name"] for d in found.values())[:4])
    return "", (f"{len(found)} AD directories have an Entitle integration pinned ({names})"
                " — choose one for the AD target")


def _group_cn(name: str) -> str:
    """``CN=ent-ad-itops,OU=Groups,…`` and ``ent-ad-itops`` are the same group."""
    n = (name or "").strip().casefold()
    if n.startswith("cn="):
        n = n[3:].split(",", 1)[0]
    return n


def _match_group(roles: list, wanted: str) -> tuple:
    """A directory GROUP's membership role, by the group's name (or CN). ``(role, "")``
    or ``(None, reason)``; refuses a built-in privileged group whatever it matched."""
    w = wanted.casefold()
    if w in PRIVILEGED_AD_GROUPS:
        return None, f"{wanted}: a built-in privileged group is never bundled"
    hits = [r for r in roles if _group_cn(r["resource_name"]) == w]
    member = [r for r in hits if "member" in r["name"].casefold()] or hits
    if not member:
        return None, f"{wanted}: not found"
    if len({r["id"] for r in member}) > 1:
        return None, f"{wanted}: {len(member)} matching roles"
    return member[0], ""


def _match_role(roles: list, wanted: str, resource: str) -> tuple:
    """``(role, "")`` or ``(None, reason)``. Exact, case-insensitive on name or external
    id, narrowed to ``resource`` (a resource name or id) when one is set."""
    w = wanted.casefold()
    hits = [r for r in roles
            if r["name"].casefold() == w or (r.get("external_id") or "").casefold() == w]
    if resource:
        hits = [r for r in hits if resource in (r["resource_id"], r["resource_name"])]
    if not hits:
        return None, f"{wanted}: not found" + (f" on {resource}" if resource else "")
    if len({r["id"] for r in hits}) > 1:
        where = ", ".join(sorted({r["resource_name"] or r["resource_id"] for r in hits})[:4])
        return None, (f"{wanted}: on {len(hits)} resources ({where}) — set the resource "
                      "for this cloud to choose one")
    return hits[0], ""


def _component(parts: list) -> dict:
    """``parts`` = ``[(label, role_or_None, reason)]`` → a component summary."""
    found = [{"label": lbl, "role_id": r["id"], "role": r["name"],
              "resource": r["resource_name"]} for lbl, r, _ in parts if r]
    missing = [{"label": lbl, "reason": why} for lbl, r, why in parts if not r]
    state = ("absent" if not found else "resolved" if not missing else "partial")
    return {"state": state, "found": found, "missing": missing}


async def resolve(db, keys: list, *, cfg=None, tp=None) -> list:
    """Resolve each requested bundle key; returns the preview rows. Reads Entitle only."""
    from . import entitle_public_api as api
    from . import entitle_virtual_app_service as vapps
    cfg = cfg or _cfg
    wanted = []
    for k in dict.fromkeys(keys):
        access, _, cloud = k.rpartition("-")
        if access in _BY_KEY and cloud in _BY_KEY[access].targets:
            wanted.append((_BY_KEY[access], cloud))
    if not wanted:
        return []

    vroles = {}
    for r in vapps.active_roles(db):
        if r.get("persona") and r.get("cloud"):
            vroles.setdefault((r["persona"], r["cloud"]), []).append(r)

    clouds = cloud_settings(cfg)
    native: dict = {}
    target_refusal: dict = {}
    if any(c == AD for _, c in wanted):
        iid, why = _ad_integration(db, clouds[AD]["integration_id"])
        clouds[AD]["integration_id"] = iid
        if why:
            target_refusal[AD] = why
    for cloud in {c for _, c in wanted}:
        iid = clouds[cloud]["integration_id"]
        if iid and cloud != "oci":
            native[cloud] = await api.list_roles(integration_id=iid, cfg=cfg, tp=tp,
                                                 max_pages=10)

    # The dashboard-* groups live in whichever directory integration user-JIT adopted
    # them from. With that integration pinned (entitle_dashboard_groups_integration_id)
    # every role in it is read once; without it each group is searched for by name
    # across the tenant, and only an exact group-name match counts either way.
    dash_names = sorted({g for p, c in wanted for g in p.dashboard_groups(c)})
    dash: dict = {}
    dash_iid = (cfg("entitle_dashboard_groups_integration_id") or "").strip()
    pool = (await api.list_roles(integration_id=dash_iid, cfg=cfg, tp=tp, max_pages=10)
            if dash_iid and dash_names else None)
    for g in dash_names:
        rows = pool if pool is not None else await api.list_roles(
            search=g, cfg=cfg, tp=tp, max_pages=1)
        dash[g] = [r for r in rows if r["resource_name"].casefold() == g.casefold()]

    flows = entitle_tiers.resolve_workflows(cfg)
    out = []
    for p, cloud in wanted:
        # group: the virtual-app role an operator tagged with this persona key + cloud
        vr = vroles.get((p.key, cloud), [])
        if len(vr) > 1:
            group = _component([(group_name(p.key, cloud), None,
                                 f"{len(vr)} virtual-app roles are tagged {p.key}/{cloud}")])
        elif vr:
            r = vr[0]
            group = _component([(group_name(p.key, cloud),
                                 {"id": r["role_id"], "name": r["name"],
                                  "resource_name": r.get("app_name") or ""}, "")])
        else:
            group = _component([])

        names = p.cloud_roles(cloud)
        if cloud == AD and group["state"] == "resolved":
            # The virtual-app role already fronts a group; granting the same group's
            # membership a second time, directly, adds nothing.
            fronted = _group_cn(vr[0].get("group_name") or "")
            names = tuple(n for n in names if _group_cn(n) != fronted)
        if cloud in native and names:
            if cloud == AD:
                cloud_c = _component([(n, *_match_group(native[cloud], n)) for n in names])
            else:
                res = clouds[cloud]["resource"]
                cloud_c = _component([(n, *_match_role(native[cloud], n, res))
                                      for n in names])
        else:
            cloud_c = _component([])

        dparts = []
        for g in p.dashboard_groups(cloud):
            hits = dash.get(g) or []
            member = [h for h in hits if "member" in h["name"].casefold()] or hits
            if len({h["id"] for h in member}) == 1:
                dparts.append((g, member[0], ""))
            else:
                dparts.append((g, None, "not found" if not member else
                               f"{len(member)} matching roles"))
        dash_c = _component(dparts)

        comps = {"group": group, "cloud": cloud_c, "dashboard": dash_c}
        blockers = [f"{n} is partial" for n, c in comps.items() if c["state"] == "partial"]
        if cloud in target_refusal:
            blockers.append(target_refusal[cloud])
        if group["state"] != "resolved" and cloud_c["state"] != "resolved":
            where = ("pin an AD directory's Entitle integration" if cloud == AD
                     else f"pin the {TARGET_LABELS[cloud]} integration")
            blockers.append(f"no {TARGET_LABELS[cloud]} access resolved — create the "
                            f"virtual-app role for {group_name(p.key, cloud)}, or {where}")
        if not flows[p.tier]["id"]:
            blockers.append(f"no workflow for the {entitle_tiers.TIER_LABELS[p.tier]} tier")
        role_ids = [f["role_id"] for c in comps.values() for f in c["found"]]
        out.append({
            "key": bundle_key(p.key, cloud), "name": bundle_name(p, cloud),
            "persona": p.persona, "access": p.key, "cloud": cloud, "tier": p.tier,
            "durations": list(p.durations()), "components": comps,
            "role_ids": list(dict.fromkeys(role_ids)),
            "workflow_id": flows[p.tier]["id"],
            "workflow_fallback": flows[p.tier]["fallback"],
            "ready": not blockers, "blockers": blockers,
        })
    return out


def _hcl_bundle(row: dict) -> dict:
    p = _BY_KEY[row["access"]]
    return {
        "key": row["key"], "name": row["name"],
        "description": f"{p.purpose} ({entitle_tiers.TIER_LABELS[p.tier]}; created by "
                       "VM Dashboard from its persona catalog.)",
        "category": CATEGORIES[row["cloud"]],
        "tags": ["vm-dashboard", f"persona:{p.persona}", f"cloud:{row['cloud']}",
                 f"tier:{p.tier}"],
        "workflow_id": row["workflow_id"],
        "allowed_durations": row["durations"],
        "role_ids": row["role_ids"],
    }


# ── Lifecycle ────────────────────────────────────────────────────────────────

def _loads(raw):
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def set_to_dict(row) -> dict:
    return {"id": row.id, "status": row.status, "bundles": _loads(row.bundles_json),
            "error": row.error or "", "created_by": row.created_by or "",
            "created_at": row.created_at.isoformat() if row.created_at else None}


def list_sets(db) -> list:
    from ..database import EntitleBundleSet
    rows = (db.query(EntitleBundleSet).filter(EntitleBundleSet.status != "deleted")
            .order_by(EntitleBundleSet.created_at.desc()).all())
    return [set_to_dict(r) for r in rows]


def start_apply(db, rows: list, *, created_by: str):
    """Record a set of READY preview rows and enqueue its apply. Returns ``(set, job)``."""
    from ..database import EntitleBundleSet
    from . import job_service
    ready = [r for r in rows if r.get("ready")]
    if not ready:
        raise ValueError("none of the selected bundles is ready to apply")
    bset = EntitleBundleSet(status="applying", bundles_json=json.dumps(ready),
                            created_by=created_by)
    db.add(bset)
    db.commit()
    db.refresh(bset)
    job = job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                 metadata={"action": "apply", "set_id": bset.id})
    return bset, job


def start_delete(db, bset, *, created_by: str):
    from . import job_service
    if bset.status in ("applying", "destroying"):
        raise ValueError(f"this bundle set is {bset.status}")
    bset.status, bset.error = "destroying", None
    db.commit()
    return job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                  metadata={"action": "delete", "set_id": bset.id})


async def run_job(db, *, job_id: str, meta: dict) -> None:
    """Worker entry for an ``entitle_persona_bundles`` job (``action`` apply | delete)."""
    from ..api.websocket import broadcast_progress
    from ..database import EntitleBundleSet
    from . import entitle_registration_service as reg
    from . import job_service
    job_service.set_running(db, job_id)
    bset = db.query(EntitleBundleSet).filter(EntitleBundleSet.id == meta.get("set_id")).first()
    if not bset:
        job_service.set_failed(db, job_id, "bundle set not found")
        return
    action = meta.get("action") or "apply"
    try:
        if action == "delete":
            await broadcast_progress(job_id, 20, "Removing the bundles from Entitle…")
            if bset.tf_state_json:
                await reg.deregister(bset.tf_state_json)
            bset.status, bset.tf_state_json = "deleted", None
            bset.updated_at = utcnow()
            db.commit()
            job_service.set_completed(db, job_id, {"action": "delete", "set_id": bset.id})
            return
        rows = _loads(bset.bundles_json)
        await broadcast_progress(job_id, 20, f"Creating {len(rows)} bundle(s) in Entitle…")
        result = await reg.register_bundles(bundles=[_hcl_bundle(r) for r in rows])
        ids = (result.get("outputs") or {}).get("bundle_ids") or {}
        for r in rows:
            r["bundle_id"] = str(ids.get(reg._safe_name(r["key"])) or "")
        bset.bundles_json = json.dumps(rows)
        bset.tf_state_json = result.get("tf_state_json")
        bset.status, bset.error = "active", None
        bset.updated_at = utcnow()
        db.commit()
        job_service.set_completed(db, job_id, {"action": "apply", "set_id": bset.id,
                                               "bundles": len(rows)})
    except Exception as exc:  # noqa: BLE001 — every failure is recorded on the row
        logger.exception("entitle persona bundles job failed set=%s", bset.id)
        bset.status, bset.error = "failed", str(exc)[:4000]
        bset.updated_at = utcnow()
        db.commit()
        job_service.set_failed(db, job_id, str(exc))

