# Managed Active Directory

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want Windows servers on AWS or GCP joined to an Active Directory domain (built here, one you already run, or your on-premises domain), or you want to manage an on-premises AD or LDAP directory through a remote agent.

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

It also handles **on-premises** directories reached through a [remote agent](../remote-agents/enrolment.md):

- **discover** domain controllers and LDAP servers;
- **register** them, or **import** them from Password Safe;
- **change** them with AD and LDAP playbooks;
- **extend** an on-prem domain to AWS (an AD Connector) or to GCP (a DNS link), so cloud
  servers join the domain you already run.

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

## Connecting on-prem AD to the cloud

Extending your own domain to the cloud is usually cheaper than running domain controllers
there. What you pay for is the network path, and what you give up is resilience to a WAN
outage. Approximate list prices per month; check each cloud's pricing page, and add data
egress:

| Option | What runs in the cloud | Approximate cost | Logons during a WAN outage |
|---|---|---|---|
| VPN + **AWS AD Connector** (built here) | A proxy to your DCs, with no DCs of its own | Small about $36, plus the VPN | No |
| VPN + **GCP DNS link** (built here) | A DNS forwarding zone | about $0.20, plus the VPN | No |
| VPN only; servers join your DCs directly | Nothing | The VPN only | No |
| VPN + one **read-only DC** (RODC) in the cloud | One small Windows VM | about $60, plus the VPN | Yes; changes still need the WAN |
| VPN + your own writable DCs in the cloud | Two Windows VMs | about $150–250 with the VPN | Yes |
| Managed AD (with a trust if you need one) | The cloud's DCs | $150–600 | Yes |

The cloud side of the VPN:

- AWS Site-to-Site VPN: about $36 per connection, with two tunnels.
- GCP HA VPN: about $73 for two tunnels.
- Azure VPN Gateway: VpnGw1 about $140 with BGP; Basic about $27, without BGP.

### Using VyOS

**VyOS on-prem, with the cloud's managed VPN on the other end, is the recommended cheap
and reliable setup.** VyOS is free on hardware or a VM you already have. The cloud side
gives you two tunnels to separate endpoints. BGP over both fails over by itself when a
tunnel or an endpoint goes down.
[`examples/playbooks/network/vyos-ad-site-vpn.yml`](../../examples/playbooks/network/vyos-ad-site-vpn.yml)
configures it:

- IKEv2 with AES-256-GCM, DH group 20 and PFS. The pre-shared keys come from Password Safe.
- The cloud learns only your DC subnets, and VyOS accepts only the cloud's networks.
- A forward firewall lets the cloud networks reach the DCs on the AD ports only, and drops
  everything else.

**VyOS at both ends** (a small cloud VM, about $8–13/month; the
[network demo cell](../profiles/demo/net-demo-cell.md) image can serve) is the cheapest option. It is also a
single point of failure in the cloud, so use it for labs and POVs, not for a domain that
production servers depend on.

The AD ports to allow from the cloud networks to the DCs:

- TCP and UDP 53 (DNS), 88 (Kerberos), 389 (LDAP) and 464 (password change);
- UDP 123 (time);
- TCP 135 (RPC), 445 (SMB), 636 (LDAPS) and 3268–3269 (Global Catalog);
- the RPC dynamic range, TCP 49152–65535, unless your DCs pin RPC to fewer ports.

The VPN encrypts all of it. Several of these protocols are not encrypted on their own.

## On-premises directories (through a remote agent)

An AD domain or an LDAP directory on a network the dashboard cannot reach is managed
through a remote agent there. It needs agent **2.8.0** or later.

**Discover.** Run a **Directories** scan from the agent's **Discover** dialog
([scan details](../remote-agents/enrolment.md#5-discover)). The scan reads the anonymous
rootDSE on 389 and 636 and never binds. Scope it by domain name to find every DC. Each
finding has a **Register directory** link that opens the form below, pre-filled.

**Register.** Use **Directories → Register on-prem**:

- AD domain or LDAP name;
- host and port, with LDAPS on by default;
- base DN (derived from the domain for AD);
- the agent that reaches the directory;
- a **Password Safe managed account** to bind as.

The dashboard stores only the account's ids and name. The credential is checked out per
run, for that run only, and never written here.

**Import from Password Safe.** **Directories → Import from Password Safe** lists the AD and
LDAP directories Password Safe already manages, with their domain, port, SSL setting and
the accounts the dashboard can request. Pick each row's account and agent. A row that cannot
be imported says why: no requestable account, no host, or a domain that is not a DNS name.
Importing changes nothing in Password Safe. It needs `secrets:use` as well as
`directories:write`.

**Change it.** In Config Management, choose the directory under **On-Prem Directories (via
agent)**, and pick **LDAP** or **WinRM**
([how the run works](../remote-agents/config-runs.md#on-premises-directories)).
[`examples/playbooks/directory/`](../../examples/playbooks/directory/) has playbooks for
these tasks:

- AD users, groups and OUs; password resets; removing stale computer objects;
- LDAP entries, attributes, group membership and passwords;
- read-only audits: stale accounts, privileged group membership and stale computers.

The agent's `policy.yaml` must list the directory's LDAP port, and 5986 for WinRM, under
`ansible.targets`.

**Unregister** only forgets the row. It is refused while an AD Connector or DNS link
extends the directory.

## Extending to AWS: an AD Connector

Use **Extend to AWS** on a registered on-prem AD. Give it:

- the region and VPC, and two subnets in different Availability Zones;
- the IP addresses of on-prem DNS servers, normally the DCs, reachable over the VPN;
- a size: Small, about $36/month, or Large, about $110/month.

The dashboard creates the connector through the Directory Service API, not Terraform, so
the service account's password never lands in Terraform state. The password comes from
the directory's Password Safe account, is used once, and is not stored.

**Turn off automatic rotation for that account in Password Safe.** AWS has no API to update
a connector's copy of the password, so a rotation breaks the connector until someone
updates it in the AWS console. The account needs only read rights and the right to join
computers.

When the connector is Active it appears in the AWS deploy form's **Join Active
Directory** picker. Joining works as for any AWS directory, through seamless domain join.
A connector that fails keeps its AWS id, so **Destroy** can delete it. Destroy is refused
while servers are joined.

## Extending to GCP: a DNS link

GCP has no AD Connector. Use **Extend to GCP** on a registered on-prem AD to build a
**DNS link**: a Cloud DNS private forwarding zone (about $0.20/month) that sends the
domain's queries from the VPC networks you name to your DNS servers over the VPN. Cloud DNS
sends those queries from 35.199.192.0/19, so your firewall and VPN routes must allow that
range to reach the DNS servers on port 53.

Choose the DNS link in the GCP deploy form's **Join Active Directory** picker:

- At first boot, the server opens a WinRM HTTPS listener (5986).
- When the deploy completes, the dashboard queues a join on the directory's own agent. The
  join runs the built-in
  [`ad-join-computer.yml`](../../examples/playbooks/directory/ad-join-computer.yml): it
  logs on as the server's stored local administrator and joins as the directory's Password
  Safe account, checked out for that run.
- No domain credential goes into instance metadata.
- The deploy job links to the join job (`ad_join_job_id`). A join that cannot be queued is
  a warning on the deploy (`ad_join_error`).

For this to work:

- the agent's `ansible.targets` must cover the server's subnet on 5986;
- the VPC firewall must allow 5986 from the agent's side of the VPN.

**For resilience**, add one read-only domain controller in GCP, on a small Windows VM, so
logons keep working when the VPN is down. The dashboard does not build one. Promote it
with your usual AD tooling, and point the DNS link at it first.

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
- **write:** build, register, import, extend to AWS or GCP, and read or reset the
  administrator password. A Config Management run against an on-prem directory needs it
  too, on top of `config_mgmt:write`.
- **delete:** destroy or unregister.

Importing from Password Safe also needs `secrets:use`.

It is checked explicitly, so a user with a custom permission map must be granted it. The
built-in read-only role reads it.

The **Join Active Directory** picker uses the deploying cloud's `write` permission
instead, because the person choosing a directory is the one deploying the server.
