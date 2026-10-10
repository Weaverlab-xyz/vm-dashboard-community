# Identity providers

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want to browse users and groups in Entra ID, Okta or PingOne from the dashboard, or change group membership there.

Part of [Directories](../directories.md).

This page is about **managing** an identity provider's users and groups. To have people
**sign in** to the dashboard through one, see [OIDC and single sign-on](../oidc.md).

**Register identity provider** adds a Microsoft Entra ID tenant, an Okta org or a
PingOne environment. These are not domains a server joins. They are where users and
groups live, and where Entitle and Password Safe grant access. Once one is registered you
can:

- **browse** its users and groups, search them by prefix, and see who is in a group and
  which groups a user is in;
- **add or remove** a user in a group, once **writes** are turned on for that directory.

The dashboard calls the provider's API itself; no agent is involved.

## Registering a provider

Registration signs in and reads one user and one group **before** anything is saved. A
wrong secret, a missing permission or an unconsented app fails the dialog, so a
registered row is one that worked at least once.

The secret is never stored. The credential field takes either:

- a **vault reference**, such as `bt_safe://Dashboard/okta-api-token`, `aws_sm://…`,
  `azure_kv://…` or `gcp_sm://…`; or
- a **Password Safe managed account**. It is checked out on first use, and the resulting
  token is held in memory only until it expires (15 minutes at most for an Okta API
  token, and never longer than the Password Safe request).

Pasting the secret itself is refused.

| Provider | What you enter | Sign-in |
|---|---|---|
| Entra ID | Tenant id, client id | Client secret, or the **dashboard's Azure identity** (the tenant is then read from its token) |
| Okta | Org URL, `https://<org>.okta.com` | API token, or an API Services app with a private key (client id and key id) |
| PingOne | Region (`com`, `eu`, `ca`, `asia`, `com.au`, `sg`), environment id, client id | Client secret of a worker app |

The Okta URL must be the org's own `okta.com`, `oktapreview.com`, `okta-emea.com` or
`okta-gov.com` address. The management API is served there even when the org has a
custom sign-in domain, and pinning it stops the dashboard being pointed at an internal
host.

## The permissions each provider needs

| Provider | To browse | To change membership |
|---|---|---|
| Entra ID | Graph **application** permissions `User.Read.All` and `Group.Read.All`, admin-consented | add `GroupMember.ReadWrite.All` |
| Okta, API token | the token acts as the admin who made it: give it to a read-only admin | that admin also needs Group Membership Admin (or higher) |
| Okta, API Services app | grant `okta.users.read` and `okta.groups.read` | also grant `okta.groups.manage`, which is requested only while writes are on. Turn off **Require DPoP**; the dashboard does not send DPoP proofs |
| PingOne | worker app with the **Identity Data Read Only** role | **Identity Data Admin** |

**Test** on the directory's row signs in afresh. For Entra ID it lists any Graph
permission the app is missing, for browsing and for writes separately.

## Changing group membership

Writes are **off** when a directory is registered unless you tick the box, and **Allow
writes** / **Make read-only** on the row changes it later. With writes on, the browse
dialog offers **Add** and **Remove** on a group's members. Each one asks for
confirmation and names the directory, and each one is written to the audit log
(`directory_member_add` / `directory_member_remove`) with the tenant, group and user ids.

Some groups are refused whatever the setting, because their membership is not the
provider API's to change:

- **Entra ID:** dynamic groups, groups synced from on-premises AD, role-assignable
  groups, and distribution lists or mail-enabled security groups (Exchange owns those).
- **Okta:** app and directory groups (`APP_GROUP`), and the built-in Everyone group.
- **PingOne:** dynamic groups.

The dashboard does **not** create, disable or delete users, reset passwords, create
groups, or create anything in Entitle. Unregistering a provider only forgets it.
