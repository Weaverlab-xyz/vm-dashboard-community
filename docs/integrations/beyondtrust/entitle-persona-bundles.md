# Entitle persona bundles

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want people to request everything one job function needs on one cloud, or on-premises through Active Directory, in a single Entitle request instead of a handful of separate roles.

An Entitle **bundle** grants several roles, from any number of integrations, in one
request. The dashboard ships a catalog with one bundle per job function per **target**.
A target is a cloud (AWS, Azure, GCP or OCI) or **on-prem AD**, which is where the IT,
OT and hypervisor roles work. Each bundle combines three components:

| Component | What it is | Where it is found |
|---|---|---|
| **group** | the [virtual-application](entitle-virtual-apps.md) role over the directory group `ent-<target>-<persona>`. For a cloud, this is the group your cloud's SSO maps to a permission set, role or binding. For AD, it is the group that holds the delegated rights | active virtual applications, by the persona and target the role is tagged with |
| **cloud** | for a cloud: roles in that cloud's own Entitle integration. For AD: membership of the persona's own delegated AD groups | the AWS, Azure or GCP integration you pin for that cloud, or the Entitle integration pinned on your AD directory |
| **dashboard** | the dashboard's own `dashboard-<scope>-<level>` groups | the directory integration those groups were synced from |

**Settings → Integrations → Entitle → Persona access bundles** (a collapsed section; click its header to open it) shows the catalog as a grid
of personas by clouds:

1. Tick the bundles you want.
2. Click **Preview**. Each cell shows **ready** and its role count, or what blocks it.
3. Click **Create**. The ready bundles are applied together as one set, with one
   Terraform state.

**Remove** destroys that set.

## The catalog

| Persona | AWS | Azure | GCP | OCI group policy | Tier · durations |
|---|---|---|---|---|---|
| Cloud Ops | `AmazonEC2FullAccess`, `ReadOnlyAccess` | Virtual Machine Contributor, Reader | `roles/compute.instanceAdmin.v1`, `roles/viewer` | `manage instance-family`, `read all-resources` | one approver · 1–12 h |
| DevOps | `PowerUserAccess` | Contributor, Azure Kubernetes Service Cluster User Role | `roles/container.developer`, `roles/artifactregistry.writer` | `manage cluster-family`, `manage repos` | one approver · 1–12 h |
| DBA | `AmazonRDSFullAccess` | SQL DB Contributor, SQL Server Contributor | `roles/cloudsql.admin` | `manage database-family`, `manage autonomous-database-family` | one approver · 1–12 h |
| Security / IAM | `SecurityAudit` | Security Reader | `roles/iam.securityReviewer` | `inspect all-resources`, `read audit-events` | auto · ≤ 1 h |
| SRE | `ReadOnlyAccess`, `CloudWatchFullAccess` | Monitoring Contributor, Reader | `roles/monitoring.editor`, `roles/logging.viewer` | `read all-resources`, `manage alarms` | auto · ≤ 1 h |
| SRE break-glass | `AdministratorAccess` | Contributor | `roles/editor` | `manage all-resources` | two approvers · 1 h |
| Network admin | `NetworkAdministrator` | Network Contributor | `roles/compute.networkAdmin`, `roles/compute.securityAdmin` | `manage virtual-network-family` | one approver · 1–12 h |
| FinOps | `Billing`, `AWSBudgetsReadOnlyAccess` | Cost Management Contributor | `roles/billing.viewer`, `roles/recommender.viewer` | `read usage-reports`, `manage usage-budgets` | auto · ≤ 1 h |
| AI platform | `AmazonBedrockFullAccess` | Azure AI Developer | `roles/aiplatform.user` | `manage generative-ai-family` | one approver · 1–12 h |

Every bundle also includes the matching dashboard groups. For example, Cloud Ops — AWS
includes `dashboard-aws-write` and `dashboard-images-write`, and DBA includes
`dashboard-cloud_database-write`.

### On-prem, through Active Directory

The IT, OT and hypervisor [personas](../../profiles/demo/personas/README.md) work
on-premises, so their bundles target **on-prem AD** instead of a cloud:

| Persona | AD group | Where its rights are delegated | Dashboard groups | Tier · durations |
|---|---|---|---|---|
| IT engineer | `ent-ad-itops` | the workstation and server OUs (join, reset, LAPS read) | `vms-write`, `config_mgmt-write`, `directories-read` | one approver · 1–12 h |
| OT / ICS engineer | `ent-ad-ot` | the local admin or RDP group on the OT jump hosts and engineering workstations | `ot-write`, `connections-read` | one approver · 1–3 h |
| OT break-glass | `ent-ad-ot-breakglass` | full control of the OT cell | `ot-delete`, `connections-read` | two approvers · 1 h |
| Hypervisor admin | `ent-ad-hypervisor` | the admin role in vCenter, Hyper-V, Proxmox, Nutanix and XCP-ng, through their AD identity source | `vsphere-write`, `hyperv-write`, `proxmox-write`, `nutanix-write`, `xcpng-write` | one approver · 1–12 h |

The exact steps for each group are under [Delegating rights](#delegating-rights), and
the bundle card can [create the groups](#creating-the-groups) for you.

**Only your own delegated groups.** The AD bundles never include a built-in group. The
catalog is pinned to `ent-ad-*` names by a test. At run time, resolution also refuses to
match the built-in groups that are tier 0 or reach domain controllers, even if one is
renamed or added to the catalog by mistake. These include Domain Admins, Enterprise
Admins, Administrators, Account Operators, Server Operators, Backup Operators, DnsAdmins,
Group Policy Creator Owners and the built-in Remote Desktop Users. If someone needs one
of those, Entitle's own break-glass process is the place for it, not a requestable bundle.

The AD groups can be granted two ways, and the bundle grants each group only once:

- as the virtual-application role over the group, which gives it a friendly name and a tier;
- or directly, as the group's membership role in the AD integration.

A group can be given as its plain name or its distinguished name
(`CN=ent-ad-itops,OU=Groups,…`). Both refer to the same group.

**Durations follow the tier.** The tier workflows from `terraform/entitle_user_jit` each
have one rule that ends at the tier's longest duration: 1 h for auto-approve, 24 h for
one approver and 6 h for two approvers. A bundle that offered more would let someone
request a duration that matches no rule. Each bundle is then capped further, as shown in
the table.

**OCI has no cloud component.** Entitle grants OCI access through IAM group membership,
which is the group component. The policy column is what the `ent-oci-<persona>` group's
OCI policy should allow, scoped to your compartment.

The DBA bundle is in addition to, not instead of, the per-database ephemeral accounts the
dashboard registers when it builds a database. See [Entitle](entitle.md).

## Setting it up

1. **Create the groups** — `ent-aws-cloudops`, `ent-azure-dba`, `ent-ad-itops` and so on,
   for the bundles you want. On an on-premises AD, the bundle card can do this for you
   (see [Creating the groups](#creating-the-groups)); elsewhere, create them in your IdP
   or directory. Then give each group its rights — see
   [Delegating rights](#delegating-rights). A group with no delegation grants nothing.
2. **Create a virtual application** over those groups (see
   [Entitle virtual applications](entitle-virtual-apps.md)). Groups that follow the
   naming convention are tagged with their persona and cloud automatically, and that tag
   is how a bundle finds its group role.
3. **Pin each cloud's Entitle integration** in the bundle card. The candidates are listed
   with likely matches first. If a role exists on several resources (one role name across
   several Azure subscriptions, for example), also set the **resource**: an AWS account,
   Azure subscription or GCP project name or id. Without it, that role is reported as
   ambiguous and never picked for you.
4. Optionally, set **Dashboard groups integration**
   (`entitle_dashboard_groups_integration_id`) to the directory integration the
   `dashboard-*` groups came from. Without it, each group is searched for by name across
   the tenant.
5. Set the three **tier workflows**, as described in
   [virtual applications](entitle-virtual-apps.md#tiers).
6. **For the on-prem AD bundles:** register the AD on **Directories** and pin its
   Entitle integration. If exactly one AD directory has an integration pinned, the AD
   target uses it automatically. If several do, pick one in the **On-prem AD** box of the
   bundle card. Until you do, the AD bundles report the ambiguity rather than picking one.

## Creating the groups

**Create missing groups in on-prem AD**, on the bundle card, creates the groups for the
bundles you have ticked:

- always the `ent-ad-*` groups of the ticked on-prem AD bundles;
- with **Also create the cloud bundles' groups** ticked, the `ent-aws-*`, `ent-azure-*`,
  `ent-gcp-*` and `ent-oci-*` groups of the ticked cloud bundles too. Tick this when your
  AD is the source your IdP syncs from (Entra Connect or Cloud Sync, the Okta AD agent,
  Google Cloud Directory Sync), so the cloud groups are mastered in AD like the rest.

Each is created as an empty **global security group**, in the OU you name (blank = the
domain's default Users container), with a description naming the bundle. Pick the
directory from the on-premises Active Directory rows that have a remote agent.

How it runs: one Config Management job, `builtin:ad-ensure-groups`, on the directory's
remote agent, over WinRM HTTPS (5986) to the directory's domain controller — the same
path the dashboard uses to join servers to the domain. WinRM and the AD module both use
the directory's Password Safe account, checked out for that run only. **That account
needs *Create group objects* (and *Write description*) on the target OU** — delegate it
with **Active Directory Users and Computers → the OU → Delegate Control**, or:

```powershell
dsacls "OU=Entitle,OU=Groups,DC=corp,DC=example,DC=com" /G "CORP\svc-dashboard:CC;group"
dsacls "OU=Entitle,OU=Groups,DC=corp,DC=example,DC=com" /I:S /G "CORP\svc-dashboard:WP;description;group"
```

What it never does:

- **set members** — membership is Entitle's to grant;
- **delete or rename a group**, or change an existing group other than its description —
  running it twice is a no-op;
- **create any name outside `ent-<target>-<persona>`** — names come from the catalog for
  the bundles you ticked, never from the request, and the playbook re-checks the pattern
  on the agent before it touches AD;
- **run against managed AD** (AWS Managed Microsoft AD, GCP Managed AD, Entra Domain
  Services) — those are not reached through a remote agent. Create the groups there with
  the service's delegated admin account.

The dashboard does not create groups in Entra ID, Okta or PingOne (see
[identity providers](../../directories/identity-providers.md)), nor the `dashboard-*`
groups, which come from the [user-JIT bootstrap](entitle-dashboard-permissions.md).

The run is audited as `entitle_groups_create`, with the directory, OU, job and group
names. After it finishes, wait for Entitle's next sync of the AD integration (or trigger
one), then do the delegation below and **Preview** again.

## Delegating rights

A new group grants nothing until it is given rights where the work happens. These are
the steps for each bundle; the same text is shown under **Delegation** on each cell of
the bundle card. Replace `CORP`, the OU paths, accounts and projects with your own.

### IT engineer — On-prem AD (`ent-ad-itops`)

1. Delegate on the user OUs: Reset password, and Read/Write pwdLastSet and lockoutTime — `dsacls "OU=Users,DC=corp,DC=example,DC=com" /I:S /G "CORP\ent-ad-itops:CA;Reset Password;user"`.
2. Delegate on the server and workstation OUs: Create/Delete Computer objects, and Validated write to DNS host name and service principal name.
3. Let the group read Windows LAPS passwords on those OUs: `Set-LapsADReadPasswordPermission -Identity "OU=Servers,DC=corp,DC=example,DC=com" -AllowedPrincipals "CORP\ent-ad-itops"`.
4. Make the group local Administrators on the managed servers with a GPO (Computer Configuration → Preferences → Local Users and Groups) linked only to those OUs.
5. Never nest it in Domain Admins, Account Operators or Server Operators.

### OT / ICS engineer — On-prem AD (`ent-ad-ot`)

1. Add the group to the local Remote Desktop Users group on the OT jump hosts and engineering workstations, with a GPO linked only to the OT OU.
2. In that GPO grant "Allow log on through Remote Desktop Services" to the group; on the rest of the domain deny it ("Deny log on through Remote Desktop Services").
3. In Privileged Remote Access, map the group to the OT jump group's group policy, so PRA sessions follow the same membership.

### OT break-glass — On-prem AD (`ent-ad-ot-breakglass`)

1. Add the group to the local Administrators group on the OT jump hosts and engineering workstations, with a GPO linked only to the OT OU.
2. Allow RDP for it there as for ent-ad-ot; deny interactive logon everywhere else.
3. Alert on any membership change of this group (event 4728/4729 on the DCs).

### Hypervisor admin — On-prem AD (`ent-ad-hypervisor`)

1. vCenter: add AD as an identity source (Administration → Single Sign On → Configuration), then Administration → Global Permissions → add CORP\ent-ad-hypervisor with the Administrator role and "Propagate to children".
2. Hyper-V: add the group to each host's LOCAL Hyper-V Administrators group with a GPO linked to the Hyper-V hosts' OU — not to the domain built-in group.
3. Proxmox: Datacenter → Permissions → Realms → add an Active Directory realm, sync groups, then give ent-ad-hypervisor the Administrator role on path /.
4. Nutanix Prism Central: Authentication → add the AD directory, then Role Mapping → ent-ad-hypervisor → Cluster Admin.
5. XCP-ng (Xen Orchestra): Settings → Plugins → auth-ldap pointed at AD, then give ent-ad-hypervisor the admin permission.

### Cloud bundles

The cloud groups get their rights on the cloud side. The steps are the same for every
persona; only the group and its roles change (the roles are in [the catalog](#the-catalog)).
Using Cloud Ops as the example:

**AWS** (`ent-aws-cloudops`)

1. If AD is your IAM Identity Center source (AD Connector / Managed AD) or syncs to your IdP, wait for ent-aws-cloudops to appear in Identity Center.
2. IAM Identity Center → AWS accounts → the account(s) → Assign users or groups → ent-aws-cloudops → a permission set holding: AmazonEC2FullAccess; ReadOnlyAccess.

**Azure** (`ent-azure-cloudops`)

1. Let Entra Connect / Cloud Sync carry ent-azure-cloudops to Entra ID (or create it there).
2. Subscription or resource group → Access control (IAM) → Add role assignment → Virtual Machine Contributor; Reader → assign to the group ent-azure-cloudops.

**GCP** (`ent-gcp-cloudops`)

1. Sync ent-gcp-cloudops to your Workforce Identity pool's IdP (Entra, Okta) or to Cloud Identity with Google Cloud Directory Sync.
2. Grant the roles to the group: `gcloud projects add-iam-policy-binding PROJECT --member="principalSet://iam.googleapis.com/locations/global/workforcePools/POOL/group/ent-gcp-cloudops" --role=ROLE` for each of: roles/compute.instanceAdmin.v1; roles/viewer.

**OCI** (`ent-oci-cloudops`)

1. Federate the OCI identity domain with your IdP (or AD through it) and map ent-oci-cloudops to an identity-domain group of the same name.
2. Identity → Policies → a policy allowing the group: manage instance-family in compartment <c>; read all-resources in compartment <c>.

## Why a bundle is not ready

Preview reports each component as **resolved** (every part found), **absent** (nothing
configured or found, so it is left out) or **partial** (some parts found and some not).
A bundle is created only when:

- **no component is partial.** Creating it would grant less than its name promises.
  Fix the missing role or group, or remove that component;
- **it has access on its target.** The group or cloud component must be resolved. A
  bundle that carried only dashboard groups under a cloud's or AD's name would mislead;
- **its tier has a workflow.**

Apply resolves the selection again on the server, so what gets created is what Entitle
holds at that moment. Anything no longer ready is skipped and named in the response.

## API

```
GET    /api/entitle/persona-bundles                    catalog, cloud pins, applied sets
GET    /api/entitle/persona-bundles/cloud-candidates?cloud=aws
PUT    /api/entitle/persona-bundles/clouds             {"cloud", "integration_id", "resource"}
POST   /api/entitle/persona-bundles/preview            {"keys": ["cloudops-aws", ...]}
POST   /api/entitle/persona-bundles/apply              {"keys": [...]}
DELETE /api/entitle/persona-bundles/sets/{id}
POST   /api/entitle/persona-bundles/create-groups      {"directory_id", "keys", "include_cloud", "ou"}
```

All of these are administrator-only. Bundle keys are `<persona>-<target>`, for example
`sre-breakglass-gcp` or `itops-ad`. For `cloud=ad`, `cloud-candidates` lists the AD
directories that have an Entitle integration pinned, and `clouds` accepts only one of
those.

The audit log records `entitle_cloud_pin`, `entitle_groups_create`, `entitle_bundles_apply` (with the skipped
bundles and their reasons) and `entitle_bundles_delete`.

The bundles are created through the `entitleio/entitle` provider's `entitle_bundle`
resource, as an `entitle_persona_bundles` job. Each bundle gets the category
`Cloud access` (or `On-prem access` for AD) and the tags `persona:<p>`, `cloud:<c>` and `tier:<t>`, so they are easy
to find in Entitle's catalog.
