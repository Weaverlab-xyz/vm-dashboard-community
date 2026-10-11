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

## Permissions

Viewing the page and **Check now** need `directories:read`. Every check is read-only
against the directory.

To see Entra Connect sync freshness, the Entra app also needs the Graph application
permission `Organization.Read.All` (or `Directory.Read.All`). Without it, that one check
is unknown and says what to grant.

## What this page does not show

It never reads accounts, groups, roles, sign-in methods, app credentials or privilege.
Stale and privileged accounts, standing access, risky sign-ins and attack paths are
identity posture. That is BeyondTrust Identity Security Insights' data, and the dashboard
does not collect a second copy of it.
