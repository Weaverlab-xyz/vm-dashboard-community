# Managed Active Directory

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want Windows servers on AWS or GCP joined to an Active Directory domain, built here or one you already run.

Part of [Cloud](../cloud.md).

Off by default. Turn it on with the **Managed Active Directory** toggle in Settings
(`directories_enabled`). The page is `/directories`, under **Directories** in the nav.

Entra ID join exists for Windows only on Azure VMs, and the dashboard does that directly
(see [Windows servers](vms.md#windows-servers)). On AWS and GCP a Windows server gets its
domain identity from Active Directory instead. Both clouds sell a managed one, and this
feature can:

- **build** AWS Managed Microsoft AD or GCP Managed Service for Microsoft Active Directory;
- **discover** a directory that already exists and **register** it. On AWS that includes
  AD Connector (a proxy to your on-premises AD) and Simple AD;
- **join** a Windows server to one of them when it is deployed from the AWS or GCP page.

## What a directory costs, and why nothing deletes one

A managed directory runs two domain controllers around the clock:

| Option | Approximate list price |
|---|---|
| AWS Managed Microsoft AD, Standard | about $150–200/month |
| AWS Managed Microsoft AD, Enterprise | about $600/month |
| GCP Managed Microsoft AD | about $300/month per region |

Check the cloud's pricing page for current figures. The build form makes you confirm the
cost.

Directories get **no auto-delete timer** by default, because deleting one breaks every
server joined to it. You can still set one through the [auto-delete timer](../operations/auto-delete-timer.md)
controls on the Inventory page. A timer is honoured for a directory built here, and is
never allowed on a registered one.

**Destroy is refused while any dashboard VM is still joined.** The error names the VMs.
Destroy them, or take them out of the domain, first. A registered directory is never
destroyed: deleting it on this page only forgets it.

## Building one

**Directories → Build directory**, then choose:

- **AWS:** domain name (for example `corp.example.com`), optional NetBIOS name, edition,
  region, VPC, and **two subnets in different Availability Zones**. Each holds one domain
  controller. Building takes 20–45 minutes.
- **GCP:** domain name, project, region(s), an unused **/24** for the domain controllers
  (`directory_gcp_reserved_ip_range` is the default), and the **authorized VPC networks**
  your Windows servers are on. Building can take up to an hour.

The build runs Terraform from `terraform/directory/aws_managed_ad` or
`terraform/directory/gcp_managed_ad`, and records its state like every other dashboard
resource (see [Infrastructure as Code](infrastructure-as-code.md)).

## The administrator credential

The directory's delegated administrator (`Admin` on AWS, `setupadmin` on GCP) is a
domain-admin credential, so it follows the same rules as Windows server passwords:

- It is **never stored in the dashboard database**. It is written to Password Safe
  (Secrets Safe), your external secrets backend, or the cloud's own vault, in the same
  order as [Windows server passwords](vms.md#where-the-administrator-password-goes).
  If none is configured, the build is refused.
- On **AWS**, Terraform must pass a password at create, and that value ends up in
  Terraform state. So the build immediately resets `Admin` to a fresh password through
  the Directory Service API and stores that one. The value in state no longer works.
- On **GCP**, the build calls `resetAdminPassword` after the domain exists and stores the
  password GCP returns. Nothing secret is in state.
- With **Onboard the administrator into Password Safe** ticked, the account is onboarded
  as a password-managed account on an **Active Directory** platform
  (`passwordsafe_directory_functional_account`) and rotated straight away
  (`passwordsafe_directory_change_password_on_register`). The stored copy is then
  deleted, and the page points you to Password Safe check-out instead.

If storing the password fails after the directory exists, the build still completes
with a warning. Use **Reset admin password** to set and store a new one. A registered
directory has no stored credential, because joining a server does not need one.

## Discovering and registering existing directories

**Directories → Discover existing** lists what the account already has:

- **AWS:** every directory in the chosen region (`ds:DescribeDirectories`), including AD
  Connector and Simple AD.
- **GCP:** every Managed AD domain in the project.

Only an **Active** (AWS) or **READY** (GCP) directory can be registered. Registering
re-reads the directory from the cloud rather than trusting the page, and writes nothing
to the cloud.

## Joining a Windows server

On the AWS or GCP deploy form, pick a directory under **Join Active Directory**, and
optionally an OU. The default OU is `directory_join_default_ou`; blank means the domain's
default Computers container. The picker lists only available directories on that cloud,
and on AWS only those in the deploy's region. It is ignored for Linux images.

Neither cloud needs a domain credential from the dashboard.

**AWS:** after the Administrator password is captured, the deploy waits for the instance
to come online in Systems Manager, then runs `AWS-JoinDirectoryServiceDomain` (AWS's
seamless domain join). The instance reboots. Requirements:

- The SSM instance profile has `AmazonSSMDirectoryServiceAccess` as well as
  `AmazonSSMManagedInstanceCore`.
- The instance can reach the directory's DNS addresses: same VPC, or a peered one.

**GCP:** the instance is created with `managed-ad-domain` metadata, and the guest agent
joins during first boot. Requirements:

- `gcp_domain_join_service_account` is set to a service account holding
  `roles/managedidentities.domainJoin`. The Windows server runs as it.
- The server is on one of the domain's authorized networks.

`managed-ad-domain-join-failure-stop` is set to `false`, so a failed join still leaves a
reachable server.

A join that cannot be attempted, or that fails, is a **warning on the deploy job**
(`ad_join_error`), not a failed deploy. The local administrator, stored or managed by
Password Safe, still reaches the server. A successful join is recorded as `ad_joined` on
the job; GCP's is recorded when the join is requested, because the guest performs it.
That record is what the destroy guard counts.

**Destroying a joined server does not remove its computer object from AD.** Neither cloud
does that, and removing it needs domain credentials on a host that can reach a domain
controller. Clean up stale computer objects with your normal AD tooling.

## Settings

All on the **Managed Active Directory** panel:

| Key | Default | Meaning |
|---|---|---|
| `directories_enabled` | off | The feature toggle: page, nav and API. Demo profile only. |
| `directory_aws_default_edition` | `Standard` | Edition pre-selected on the AWS build form |
| `directory_gcp_reserved_ip_range` | — | Default /24 for GCP domain controllers |
| `directory_join_default_ou` | — | OU for joined servers when the deploy names none |
| `gcp_domain_join_service_account` | — | Service account GCE Windows servers run as when joining |
| `passwordsafe_directory_functional_account` | — | Password Safe functional account on an Active Directory platform |
| `passwordsafe_directory_change_password_on_register` | on | Rotate the administrator right after onboarding |

## Permissions

A `directories` scope (read / write / delete):

- **read:** list directories.
- **write:** build, register, and read or reset the administrator password.
- **delete:** destroy or unregister.

It is checked explicitly, so a user with a custom permission map must be granted it. The
built-in read-only role reads it.

The **Join Active Directory** picker uses the deploying cloud's `write` permission
instead, because the person choosing a directory is the one deploying the server.
