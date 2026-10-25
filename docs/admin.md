# Admin

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding who may use the dashboard, where its secrets live, how you would prove what happened, or you are running its health checks and notifications.

Running the dashboard as its administrator: the pages behind the app's **Admin** menu. Who
may do what, the machines that call it, its secrets and audit record, and what it tells you when something needs attention.

| Page | Read it when |
|---|---|
| [Permissions](admin/permissions.md) | you are deciding what a user may see or do, and especially before ticking "Full access", or handing a POV to a customer stakeholder |
| [Service Accounts](admin/service-accounts.md) | something that is not a person (a CI job, an MCP agent, a script) needs to call the API, and you would otherwise hand it a PAT |
| [Secrets Management](admin/secrets-management.md) | you are deciding where to store cloud credentials, and how to evolve that over time |
| [Audit Log](admin/audit-log.md) | you need to show who did what, or to satisfy yourself that the record has not been edited |
| [Health](admin/health.md) | you want to know whether what the dashboard depends on is working, or why an area shows as degraded |
| [Notifications](admin/notifications.md) | you want to hear about expiring resources and failed jobs without opening the dashboard |

Related:
- [Platform](platform.md): single sign-on, the dashboard's own identity in place of stored keys, and
  [Remote Agents](platform/spiffe/remote-agents.md), under SPIFFE and SPIRE: the Agents page is in this menu, but an agent is a workload that attests to the dashboard's SPIRE server;
- [BeyondTrust](beyondtrust.md): Entitle's just-in-time dashboard access, the app's Request access link.
