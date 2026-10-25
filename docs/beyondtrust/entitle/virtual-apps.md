# Entitle virtual applications

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you registered Active Directory or an identity provider and want people to request its groups in Entitle by a clear name, under an approval tier.

When Entitle syncs a directory, each group becomes a resource with a membership role. In
the request catalog those show up under their raw group names, such as `ent-aws-cloudops`
or `CN=SQL-Admins,OU=Groups,DC=corp,DC=example,DC=com`. A **virtual application** puts a
friendly role over each group and groups them under one application:

```
Cloud access  (virtual application)
├── AWS
│   └── Cloud Ops — AWS   ─fronts→  ent-aws-cloudops · Member   (Okta integration)
└── Azure
    └── DBA — Azure       ─fronts→  ent-azure-dba · Member      (Okta integration)
```

When someone requests **Cloud Ops — AWS**, Entitle adds them to `ent-aws-cloudops` for
the duration they picked. Whatever that group already grants, such as an IAM Identity
Center assignment, an Azure role or a GCP binding, then takes effect.

The dashboard **creates nothing in the directory.** It only links groups that Entitle
has already synced. Before anything is applied, it checks each one against Entitle to
confirm it belongs to this directory's integration.

## Before you start

1. Register the directory on **Directories**. Every kind works except the two site links
   (`dns_link`, `gcp_vyos_link`): Active Directory (on-premises or managed in AWS, GCP or
   Azure), LDAP, Entra ID, Okta or PingOne. See [Directories](../../preview/directories.md).
2. In Entitle, connect the directory as an integration and let it sync.
3. On the directory's row, click **Entitle** and pin that integration. See
   [identity providers](../../preview/directories/identity-providers.md).
4. In **Settings → Integrations → Entitle**:
   - turn on **resource registration** (`entitle_registration_enabled`);
   - set the **Owner ID** (`entitle_owner_id`);
   - set the tier workflows (below), or at least the default **Workflow ID**.

## Tiers

Every role goes through one of the three tiers that
[`terraform/entitle_user_jit`](../../../terraform/entitle_user_jit/README.md) creates.
This keeps a group requested here consistent with the dashboard's own `dashboard-*` groups.

| Tier | Workflow setting | Durations offered |
|---|---|---|
| Auto-approve | `entitle_workflow_auto_approve_id` | 30 min, 1 h |
| One approver | `entitle_workflow_single_approver_id` | 1 h, 3 h, 12 h, 24 h |
| Two approvers | `entitle_workflow_two_approver_id` | 1 h, 3 h, 6 h |

The durations are the defaults from that module. Each tier workflow has one rule that
stops at the tier's longest duration, so a role cannot offer more than its tier allows.

If a tier's own workflow setting is blank, it falls back to `entitle_workflow_id`. The
dialog labels that tier **(default workflow)**, so a two-approver role is never routed
through a one-step workflow without you seeing it.

## Creating one

1. On **Directories**, click **Virtual app** on the directory's row. The button appears
   only once an integration is pinned.
2. Name the application, for example `Cloud access`.
3. Search the directory's groups, then **Add** each one to include. Membership roles are
   listed first.
4. For each group, set the role name, the grouping (the resource inside the application)
   and the tier. Groups named `ent-<cloud>-<persona>` come pre-filled to match the
   [persona bundles](persona-bundles.md), for example `ent-gcp-sre` →
   **SRE — GCP**, grouping **GCP**, tier auto-approve. On an AD directory,
   `ent-ad-itops` → **IT engineer — On-prem AD**.
5. Click **Create in Entitle**. This queues an `entitle_virtual_app` job. When the job
   finishes, the application is **active** and each role's new Entitle id is stored with it.

The create and remove requests are written to the audit log as
`entitle_virtual_app_create` and `entitle_virtual_app_delete`. Each entry names the
directory, the application and the role each new role fronts.

### Through the API

```
GET    /api/directories/{id}/entitle-roles?q=ent-      # what the pinned integration synced
GET    /api/directories/{id}/virtual-apps              # apps, tiers, and any refusal
POST   /api/directories/{id}/virtual-apps              # {"name": ..., "roles": [...]}
DELETE /api/directories/{id}/virtual-apps/{app_id}
```

Each item in `roles` is `{role_id, name, resource, tier, persona, cloud}`:

- `role_id` is the Entitle role being fronted.
- `persona` and `cloud` are optional. They tag the role so a persona bundle can find it.

Reading needs `directories:read`; creating and removing need `directories:write`.

## Removing one

Click **Remove** next to the application. The job runs `terraform destroy` against the
state stored with the application, which deletes the application, its resources and its
roles from Entitle. The directory and its groups are left alone. Check Entitle for active
grants on the roles first: the dashboard removes the application, but group memberships
are Entitle's to revoke.

A directory cannot be unregistered while it has a virtual application in any state other
than removed.

## What to confirm on a new tenant

The Terraform this runs was validated against the
[provider's own schema](../../../tests/fixtures/entitle_provider_schema.json), and CI
validates it again on every change. Two values come from Entitle's catalog rather than
the schema, so check them the first time you use a tenant:

- **The application name.** The default is `virtual application`, Entitle's catalog name,
  lowercased as the provider requires. A tenant whose catalog differs fails with
  `404 Application not found`. Override it with `entitle_virtual_app_slug`.
- **The connection.** A virtual application has no connection settings, so the dashboard
  sends `{}`. If Entitle rejects that, the job error says so, and nothing is created.

If an apply fails partway, the job records the error on the application. Anything
Entitle had already created by then is not in a stored state, so remove it by hand under
**Integrations** in Entitle.
