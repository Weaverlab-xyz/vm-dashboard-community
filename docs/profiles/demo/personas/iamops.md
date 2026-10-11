# IAM Engineer

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to whoever runs the directories day to day: the domains, the identity providers, and the groups that grant access.

Owns the **directories**. That means Active Directory domains, LDAP, and the cloud identity
providers (Entra ID, Okta, PingOne), along with the groups in them that decide what everyone else
can reach. Requests from the rest of this list end with this person changing a group membership.

Their problem is that they are the most privileged people in the building and everyone knows it.
A domain admin credential is the first thing an attacker wants, and in most estates several
people hold one permanently.

## Why they care

The directory is tier 0. Whoever can change it can grant themselves anything else, so it is the
place a standing credential costs the most. The usual workarounds make it worse:

- a shared domain admin password in a spreadsheet;
- a service account whose password was set once and never rotated;
- audit scripts run from someone's laptop with their own admin rights.

What changes that is to treat the directory like any other privileged target. Its administrator
credentials are vaulted and rotated. The work is done from inside the network by something that
checks out a credential per run. And the people who change it are granted that right for the
hour they need it.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Domains are built or registered on one page, and servers join them at deploy time. |
| **PRA** | Windows servers on the domain are reached through a jump item rather than open RDP. |
| **Password Safe** | Domain administrators and bind accounts are managed accounts: checked out per run and rotated. |
| **Entitle** | The right to change the directory is requested and expires, so no engineer is a standing directory admin. |

## Use cases

### A domain admin password nobody knows

Build a managed AD domain on AWS or GCP and tick **Onboard the administrator into Password Safe**.
The delegated administrator is onboarded on an Active Directory platform and rotated straight
away. On AWS, the password Terraform had to pass at create is reset before the build finishes, so
the value in state no longer works.

**Guide:** [Active Directory in the cloud](../../../directories/active-directory.md)

### Start from the directories Password Safe already manages

**Directories → Import from Password Safe** lists the AD and LDAP directories Password Safe
already holds, with the accounts the dashboard can request. Each imported directory binds as a
managed account that is checked out per run and never written to the dashboard.

**Guide:** [On-premises directories](../../../directories/on-premises.md)

### Privileged groups and stale accounts, audited from inside the network

Run the read-only audits in `examples/playbooks/directory/` against an on-prem domain from
Config Management, through the remote agent on that domain's network. They cover privileged
group membership, stale accounts and stale computers. The bind credential is checked out for
that run only, and nobody's laptop holds domain rights.

**Guide:** [On-premises directories](../../../directories/on-premises.md)

### Change a group membership, with the change on the record

Register an Entra ID tenant, Okta org or PingOne environment, then browse its users and groups.
Writes are off until someone turns them on for that directory. Each add or remove asks for
confirmation and is written to the audit log with the tenant, group and user ids. Groups the
provider API should not change, such as on-prem synced, dynamic or role-assignable groups, are
refused whatever the setting.

**Guide:** [Identity providers](../../../directories/identity-providers.md)

### Nobody holds directory write by default

Map the dashboard's `directories` write scope to an Entitle resource, request it for an hour,
and show the grant end on its own. The people who change the directory follow the same rule
they enforce on everyone else.

**Guide:** [Entitle dashboard permissions](../../../integrations/beyondtrust/entitle-dashboard-permissions.md)

## What to enable

Turn on **Directories** under Preview features. Then add:

- **Password Safe** for the first two cards;
- **Configuration Management** and **Remote agents** for the audit card, with agent 2.8.0 or
  later on the domain's network;
- **Entitle user JIT** for the last card.

**This focus needs an estate instance:** Directories is estate-owned, so most of these cards are masked on a
[POV instance](../../pov/README.md). Directories builds billing domain controllers and
onboards their administrator through the instance-wide Password Safe settings, and on a POV
instance that would write into the wrong customer's tenant.

## Talking to this buyer

They have heard "zero standing privilege" before, from people who have never had to fix a broken
replication link at 2 a.m. Lead with the domain admin card. It shows a password that is no longer
anyone's to know, and nothing they rely on stops working. Then show the audit run from the agent,
because it answers the question they get asked every quarter. Leave the Entitle card for last:
it only lands once they believe the break-glass path still works.

Identity risk across the estate, such as attack paths and over-privilege, is the job of
BeyondTrust Identity Security Insights, not of this dashboard. Say so if they ask.
