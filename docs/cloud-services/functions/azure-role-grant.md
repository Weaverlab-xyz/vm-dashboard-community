# The azure_role_grant workload

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want Entitle to grant and revoke Azure role assignments through a function.

Part of [Cloud Functions](../functions.md).

Time-boxed Azure RBAC for a **machine identity**. AWS machine identity works because
Entitle attaches an IAM policy to the dashboard's IAM *user*; Azure has no
equivalent, because a machine identity there is an *application* and an application
cannot be the account privileges are requested for. So Entitle's native Azure
integration cannot grant to one, and this adapter is the only route.

| Setting | Notes |
|---|---|
| `FN_AZURE_TENANT_ID` / `FN_AZURE_CLIENT_ID` | the adapter's own service principal; needs **User Access Administrator** on the scopes below |
| `FN_AZURE_CLIENT_SECRET` | that principal's secret — pass it as `secret_environment` (see [Credentials](../functions.md#credentials)). It is the highest-value credential in the feature, so it is never a plaintext setting |
| `FN_AZURE_SUBSCRIPTION_ID` | where role definitions are resolved from |
| `FN_AZURE_SCOPES` | comma-separated scopes it may grant at — subscription or resource-group paths |
| `FN_AZURE_ROLES` | comma-separated role names or GUIDs it may grant |
| `FN_AZURE_PRINCIPALS` | comma-separated service-principal **object** ids, optionally `objectid=Label` |
| `FN_AZURE_DRY_RUN` | unset or truthy = dry run. **Default is dry run.** |

> **An unbounded adapter here is a privilege-escalation primitive**, so it is bounded
> three ways. Scopes, roles and principals are all allowlists — an empty one grants
> nothing rather than everything. Management-group and tenant-root scopes are refused
> outright. And **Owner, User Access Administrator and RBAC Administrator are refused
> even if you allowlist them**: a time-boxed grant of a role that can grant further
> access is not time-boxed, because the grantee can make it permanent before it
> expires. If you genuinely need to hand one of those out, that belongs with a human
> and a change ticket, not an automated integration behind a shared secret.

Assignments are named by a GUID derived from (scope, principal, role), so a grant is
an idempotent upsert and a revoke deletes by name without listing anything — and
`get_all_permissions` reports only assignments this adapter made, never ones a human
created at the same scope.

`FN_AZURE_PRINCIPALS` accepts a label (`<object-id>=deploy-sp`) so an Entitle request
reads as a name rather than a GUID. Note it must be the service principal's **object**
id: Azure accepts an application id and silently creates an assignment that grants
nothing.
