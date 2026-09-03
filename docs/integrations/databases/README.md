# Databases · Password Safe

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are handing rotation of a database credential to Password Safe, and want the per-cloud channel.

Handing a **database** credential to Password Safe, so the vault owns the password and
rotates it rather than this dashboard storing one. This is Layer 2 of the
[Databases](../../databases.md) stack, split out per cloud because the three clouds reach a
managed database three different ways — and the way in is what decides the setup.

| Page | Read this when |
|---|---|
| [Password Safe rotation (AWS + Azure)](password-safe.md) | your database is RDS or Azure Flexible Server, and you want the shared model plus the `dbssm` and `dbazure` channels. |
| [Password Safe rotation for Cloud SQL (GCP)](password-safe-gcp.md) | your database is Cloud SQL, which rotates over the Data API with no jump host. |

The Password Safe integration itself — the connection, the API registration and the
non-database managed systems — is [one level up](../password-safe.md).
