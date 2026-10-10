# Identity and access

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding who and what may use the dashboard, how it keeps credentials, or how you would prove what happened.

Who may do what in the dashboard, how machines get in, where its secrets live, and the
record of all of it.

| Page | Read it when |
|---|---|
| [Permissions](access/permissions.md) | you are deciding what a user may see or do, and especially before ticking "Full access", or handing a POV to a customer stakeholder |
| [Service Accounts](access/service-accounts.md) | something that is not a person (a CI job, an MCP agent, a script) needs to call the API, and you would otherwise hand it a PAT |
| [Secrets Management](access/secrets-management.md) | you are deciding where to store cloud credentials, and how to evolve that over time |
| [Audit Log](access/audit-log.md) | you need to show who did what, or to satisfy yourself that the record has not been edited |

Related sections:
- [OIDC and single sign-on](oidc.md): how people sign in, and how the dashboard proves
  its own identity to the clouds;
- [Directories](directories.md): Active Directory domains servers join, and the identity
  providers (Entra ID, Okta, PingOne) whose users and groups you manage;
- [Workload Lab](workload-lab.md): credentials for workloads the dashboard provisions,
  rather than for the dashboard itself;
- [Entitle dashboard permissions](integrations/beyondtrust/entitle-dashboard-permissions.md):
  dashboard access granted just in time instead of standing.
