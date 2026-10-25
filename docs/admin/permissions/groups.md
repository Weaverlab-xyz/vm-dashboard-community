# Permission groups

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are mapping identity-provider groups to dashboard access.

Part of [Permissions](../permissions.md).

A group mapping turns a group from your identity provider into a workstream, a role
(required), and a default permission set added on top of it. **Not Entra-only** — any OIDC provider works, and the
"Group Object ID" field takes whatever identifier that provider's groups claim emits. The
stored column is still called `entra_group_id`, which is why you will see that name in the
API and in the database.

Things to know:

- **If any mappings exist, signing in requires a match.** An OIDC user in none of the
  mapped groups is refused at sign-in (`not_authorized`). With no mappings configured,
  only users who already have an account can sign in.
- **A login also rewrites the user's workstreams** to the matched mappings' workstreams.
  Workstreams set by hand on an OIDC user do not survive their next sign-in.
- The grid is labelled "Default Permissions for new users", but it is not only for new
  users. Its permissions are re-applied to every member **at each login**, so a mistake in a
  mapping keeps reasserting itself until the mapping is fixed — and a change you make
  reaches existing members at their *next sign-in*, not immediately.
- That includes the role. Editing a role changes it immediately for users who hold it
  directly, and at next sign-in for everyone who gets it through a group.
