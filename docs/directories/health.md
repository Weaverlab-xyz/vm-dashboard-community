# Directory health

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want to know whether each directory is up and answering, or why one shows as degraded.

Part of [Directories](../directories.md).

**Directories → Health** (`/directories/health`) shows whether each directory on the
Directories page is up and answering. It covers the directory **service**, not the
identities in it.

## What is checked

| Directory | Checks | Where the answer comes from |
|---|---|---|
| AWS Managed Microsoft AD | the directory's stage, and each domain controller's status | `DescribeDirectories`, `DescribeDomainControllers` |
| AWS AD Connector, Simple AD | the directory's stage | `DescribeDirectories` |
| GCP Managed AD | the domain's state and status message | the Managed Microsoft AD API |
| Entra Domain Services | provisioning state, each replica set's service status, and every health alert Entra DS raises (for example an expiring secure LDAP certificate or a blocked management port) | the `Microsoft.AAD/domainServices` resource |
| Entra ID, Okta, PingOne | a sign-in and a one-user read, with the time it took | the same call as **Test** on the row |
| Entra ID, synced from on-prem | how long ago Entra Connect last synced: over an hour is degraded, over three is failing | Graph `/organization` |
| On-prem AD and LDAP | whether the remote agent is online, and whether the domain controller answers LDAP for the domain you registered | an anonymous rootDSE read by the agent |
| On-prem AD, with a Password Safe account | replication, FSMO role holders, and for each domain controller its core services, clock, SYSVOL and LDAPS certificate | the [domain controller check](#the-domain-controller-check) |

A GCP DNS link or VyOS site link is listed but not checked on its own. It runs no domain
controllers, so it is as healthy as the on-prem directory it links to.

Each directory gets the status of its worst check:

- **Failing**: at least one check failed.
- **Degraded**: something is not right yet, but nothing has failed.
- **Healthy**: every check passed.
- **Unknown**: nothing could be checked, for example because the directory is still
  being built.

A check that cannot be made, such as sync freshness without the permission to read it, is
shown as unknown. It does not make an otherwise healthy directory unhealthy.

## When checks run

The jobs worker checks every directory each **Health check interval** (Settings →
Preview features → Directories → Configure, `directory_health_interval_minutes`, default
15). Set it to 0 to turn scheduled checks off. **Check now** on a row checks that one
directory straight away. A second press within a minute returns the result already
stored.

The page itself never calls a provider. It reads the last stored result, so it loads in
one query however many directories there are.

**On-prem directories are probed through their agent.** The check queues a discovery
job scoped to the directory's one host and port. It is the same anonymous rootDSE read
as a **Directories** scan in the agent's **Discover** dialog, so it needs no credential
and agent **2.8.0** or later. The result appears when the agent finishes the job. Each
probe is a row on the Jobs page, so on-prem directories are probed at most once an hour,
whatever the interval. The host must be listed under `targets` in the agent's
`policy.yaml`, as it is for a scan; if it is not, the agent refuses the probe and the
check fails, saying so.

## The domain controller check

For an on-prem Active Directory domain registered with a Password Safe account, the
dashboard also runs `builtin:ad-dc-health` through the domain's remote agent. It is a
Config Management run over WinRM HTTPS (5986) on the registered domain controller. It
logs on as the directory's Password Safe account, checked out for that run only, and
passes the same credential to every remote call. The playbook is
[`examples/playbooks/directory/ad-dc-health.yml`](../../examples/playbooks/directory/ad-dc-health.yml),
one read-only PowerShell script.

| Check | Healthy | Degraded | Failing |
|---|---|---|---|
| **AD replication**: every inbound link, from `Get-ADReplicationPartnerMetadata` | every link replicated in the last 3 hours with no failures | 1–2 consecutive failures, a non-zero last result, or no success for over 3 hours | 3 or more consecutive failures, or no success for over 24 hours |
| **FSMO roles**: the five holders, from `Get-ADDomain` and `Get-ADForest` | every holder is a current domain controller | — | a role is held by a server that is no longer a domain controller |
| **Each domain controller**: NTDS, DNS, Netlogon, Kdc, W32Time and DFSR | all running | — | any one stopped |
| Clock offset from the DC the check ran on | within 60 seconds | over 60 seconds | over 240 seconds (Kerberos allows 300) |
| SYSVOL (DFSR) state | normal | initialising, initial sync or auto recovery | in error |
| LDAPS certificate on 636 | 30 days or more left | under 30 days | under 7 days, or expired |

A domain controller with nothing readable shows as unknown, and says what failed. That
happens when the account cannot open a CIM session to it, or when it has no LDAPS. One
unreadable DC never hides the others.

**When it runs.** Every **Domain controller check interval**
(`directory_health_deep_interval_hours`, default 6; 0 turns scheduled checks off), and on
**Check now** by someone with `directories:write`. Each run checks out a Password Safe credential and is a row on the Jobs
page, which is why it runs hours apart rather than with every health check. The results
appear when the agent finishes. Until then the page shows the previous ones.

**What it needs:**

- agent **2.8.0** or later, granted Config Management (`agent_ansible`);
- the domain controller's address and port 5986 listed under `ansible.targets` in the
  agent's `policy.yaml`, and WinRM over HTTPS enabled on it;
- the account's rights, in two parts:
  - replication metadata, the FSMO holders and LDAPS certificates are readable by any
    domain user;
  - services, the clock and SYSVOL are read over remote CIM on each domain controller.
    That needs local administrator rights there, or WMI and **Remote Management Users**
    rights delegated to the account.

  Without the second part those rows show as unknown with "access denied", and the rest
  of the check still works.

If a domain is registered without a Password Safe account, or its agent cannot run Config
Management, the page says so in place of these checks.

The check reads the directory service only. It never lists users, groups or computers.

## Permissions

Viewing the page and **Check now** need `directories:read`. Every check is read-only
against the directory. **Check now** starts a [domain controller
check](#the-domain-controller-check) only for someone with `directories:write`, because
that run checks out the directory's Password Safe account; for anyone else it re-runs the
other checks and the domain controller check keeps its schedule.

To see Entra Connect sync freshness, the Entra app also needs the Graph application
permission `Organization.Read.All` (or `Directory.Read.All`). Without it, that one check
is unknown and says what to grant.

## What this page does not show

It never reads accounts, groups, roles, sign-in methods, app credentials or privilege.
Stale and privileged accounts, standing access, risky sign-ins and attack paths are
identity posture. That is BeyondTrust Identity Security Insights' data, and the dashboard
does not collect a second copy of it.
