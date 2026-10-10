# Active Directory in the cloud

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want Windows servers on AWS, GCP or Azure joined to an Active Directory domain that runs in the cloud: one built here, or one that already exists.

Part of [Directories](../directories.md).

All three clouds sell a managed Active Directory, and the **Directories** page can:

- **build** AWS Managed Microsoft AD, GCP Managed Service for Microsoft Active Directory,
  or Microsoft Entra Domain Services on Azure;
- **discover** a directory that already exists and **register** it. On AWS that includes
  AD Connector (a proxy to your on-premises AD) and Simple AD;
- **join** a Windows server to one of them when it is deployed from the AWS, GCP or Azure
  page.

Azure VMs can also join **Entra ID** directly, without a domain
(see [Windows servers](../cloud/windows-servers.md)). Entra Domain Services is for servers that
need Kerberos, LDAP or Group Policy. A VM joins one or the other, not both.

To join servers to the domain you already run on-premises instead of building one, see
[On-premises directories](on-premises.md).

## What a directory costs, and why nothing deletes one

A managed directory runs two domain controllers around the clock:

| Option | Approximate list price |
|---|---|
| AWS Managed Microsoft AD, Standard | about $150–200/month |
| AWS Managed Microsoft AD, Enterprise | about $600/month |
| GCP Managed Microsoft AD | about $300/month per region |
| Entra Domain Services, Standard | about $110/month |
| Entra Domain Services, Enterprise | about $290/month |
| Entra Domain Services, Premium | about $1,170/month |

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
- **Azure:** see [Entra Domain Services](#entra-domain-services). Building takes 45–60
  minutes.

The build runs Terraform from `terraform/directory/aws_managed_ad` or
`terraform/directory/gcp_managed_ad`, and records its state like every other dashboard
resource (see [Infrastructure as Code](../cloud/infrastructure-as-code.md)).

## The administrator credential

The directory's delegated administrator (`Admin` on AWS, `setupadmin` on GCP) is a
domain-admin credential, so it follows the same rules as Windows server passwords:

- It is **never stored in the dashboard database**. It is written to Password Safe
  (Secrets Safe), your external secrets backend, or the cloud's own vault, in the same
  order as [Windows server passwords](../cloud/windows-servers.md#where-the-administrator-password-goes).
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

## Entra Domain Services

Azure's managed Active Directory. It differs from the AWS and GCP ones in one way that
shapes everything else: **it has no administrator of its own.** Its admins are Entra users
in the tenant's **AAD DC Administrators** group, and their passwords sync from Entra. The
dashboard creates no user and changes no group, so there is no administrator password to
store or reset.

Instead, you pin a **join account**: an existing member of AAD DC Administrators, held in
Password Safe as a managed account. Azure VMs join the domain as that account. Its password
is checked out of Password Safe for each join, passed only to the VM extension's protected
settings, and never stored by the dashboard or written to the job. Pin it on the build form,
or later with **Join account** on the directory's row. Use the account's UPN
(`joiner@contoso.com`) as the account name; a bare name gets the managed domain appended.

**Before you build.** The build is refused, before anything is created, when:

- the **Microsoft.AAD** resource provider is not registered in the subscription. Fix it
  with `az provider register --namespace Microsoft.AAD`;
- the **Domain Services service principal** is missing from the tenant. A Global
  Administrator creates it with `az ad sp create --id 2565bd9d-da50-47d4-8b85-4c97f669dc36`.
  The dashboard can only check this when its Azure app can read Graph; when it cannot, the
  build goes ahead and Terraform reports a missing principal;
- the subscription already has Entra Domain Services. A tenant may have only one, so
  register the existing one with **Discover** instead. One in *another* subscription of the
  same tenant cannot be seen from here, and the build then fails in Terraform.

The dashboard runs none of those commands itself.

**The build form** asks for:

- the domain name, the SKU, the location and the resource group;
- the VNet your Windows servers are on, with its resource group;
- an **unused /24** in that VNet. The module adds a dedicated subnet there, with the
  network security group Microsoft documents (5986 from the service's management plane,
  3389 from Microsoft's support hosts, nothing else);
- whether to **point the VNet's DNS at the domain controllers**. Joining needs the VNet to
  resolve the domain, but this changes name resolution for every VM on that VNet, so it is
  off unless you tick it. With it off, point the VNet's DNS (or a forwarder) at the domain
  controller addresses shown on the row after the build.

It runs Terraform from `terraform/directory/azure_managed_ad`.

**After the build**, a cloud-only Entra user must change their password once before they
can sign in to the managed domain: Entra only syncs the password hashes the domain needs
when a password is set. Synced users need password-hash sync on in Entra Connect. The build
job lists what is still to do.

**Joining a VM.** On the Azure deploy form, a Windows image offers **Join Active
Directory**. A directory with no join account is listed but cannot be picked. After the VM
is created, the `JsonADDomainExtension` VM extension joins it, optionally into an OU, and
the VM reboots. A failed join is a warning on the deploy job and the VM keeps its local
administrator. A VM joins Entra ID or a managed AD domain, not both: picking a directory
turns off **Join to Microsoft Entra ID**.

**Reset admin password** is not offered: reset an admin's password in Entra, or rotate the
join account in Password Safe.
