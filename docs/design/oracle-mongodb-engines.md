# Design: Oracle and MongoDB as first-class database engines

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you are adding Oracle or MongoDB support to a database layer (PRA tunnel, Entitle, Password Safe, Ansible), or swapping their tcp tunnels for PRA 26.3's dedicated ones.

Status: **slice 1** (engine plumbing, the raw-TCP tunnel, Entitle payloads) and **slice 2**
(AWS RDS for Oracle — `terraform/db_aws_oracle`, SE2 license-included, single-tenant CDB so
the one PDB is what Entitle manages) landed. **Slice 3** adds MongoDB Atlas
(`terraform/db_atlas_mongodb`: one Atlas project per cluster, public endpoint locked to the
gateway's egress /32, Flex or dedicated) and Entitle's Atlas MongoDB integration. **Slice 4**
onboards RDS Oracle on Password Safe's native Oracle platform (asset -> database -> managed
system, via `passwordsafe_managed_system_by_database`; the by-workgroup resource has no
instance field) with a self-rotating `psafe_<id>` created by sqlplus over SSM. MongoDB Atlas
stays out of Password Safe: its users change only through the Atlas Admin API. Remaining:
Configuration Management (Ansible).

Re-checked 2026-10-07: `beyondtrust/sra` **v1.4.0** (2026-09-25, the latest) still validates
`tunnel_type` as `OneOf("tcp", "mssql")`, and nothing on its `main` mentions MongoDB or
Oracle. Oracle on Azure / GCP (Oracle Database@Azure,
Oracle Database@Google Cloud, Autonomous tier) is deferred.

## Why

PRA 26.3 ships Oracle and MongoDB tunnels, and Password Safe and Entitle already manage
both engines. Before this work the dashboard's Oracle support was half-wired, and every
broken layer failed silently:

| Layer | What was wrong |
|---|---|
| PRA | the Oracle `tcp` tunnel carried no `tunnel_definitions`, so it had nothing to forward |
| Entitle | Oracle fell into the postgres branch: `user` + `options{}`, no `service_name` |
| Password Safe | `ps_api_service._PLATFORM_BY_ENGINE` had no Oracle, and the legacy staging swallows that error |
| Destroy | the generic tunnel resource mapped back to whichever engine was listed last |

MongoDB was absent everywhere.

## Verified facts (2026-10-07)

### PRA

* PRA **26.2.1** already has a MongoDB tunnel. Its jump item carries an **Auth Source**
  field that defaults to `admin`, and its **Database** field is required.
  (PRA 26.2.1 release notes.)
* The `beyondtrust/sra` provider's `sra_protocol_tunnel_jump.tunnel_type` validator is
  `OneOf("tcp", "mssql")` (see [the k8s tunnel note](../notes/sra-provider-k8s-tunnel-bug.md)).
  A dedicated Oracle or MongoDB tunnel type is therefore blocked **in the provider**, not
  in PRA. The dashboard brokers DB tunnels with the provider only, never `btapi`, so it
  waits for the provider.
* Until then, both engines use `tunnel_type = "tcp"` with
  `tunnel_definitions = "<port>;<port>"` and `tunnel_listen_address = "127.0.0.1"`. That
  is the same shape as the k8s API tunnel, which is proven live. No username or database
  goes on a tcp jump.

### Entitle

* **Oracle Database** connector
  ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-oracle_database)):
  `username`, `password`, `host`, `service_name`, optional `port` (default 1521), `protocol`
  (`tcp`|`tcps`) and `ssl_server_dn_match`. It manages **PDBs only**, so `service_name`
  must not be the CDB. It needs a SYSDBA account, or at least the DBA role. It mints
  ephemeral accounts and needs the Entitle agent.
* **Atlas MongoDB** is the only Mongo integration
  ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-atlas),
  [API key](https://docs.beyondtrust.com/entitle/docs/configuring-mongodb-atlas-api-key)):
  * Connection is `public_key` / `private_key` / optional `project_id`, plus
    `options{connect_to_clusters, read_only, use_privatelink_endpoint}`.
  * It needs an **Organization Owner** key, or Group Membership Admin + Report Admin for
    partial function.
  * Entitle's egress IPs must be on the key's API access list and on each project's
    network access list.
  * It mints ephemeral accounts.
  * There is no self-hosted MongoDB connector, so a non-Atlas Mongo row is refused for
    Entitle (`_entitle_ineligible_reason`).

### Open (verify on a live tenant)

* Does the OCI Autonomous DB `ADMIN` user (PDB_DBA) satisfy the Oracle connector's
  "DBA role" floor? And the RDS for Oracle master user?
* What are the Password Safe built-in platform names? `Oracle` and `MongoDB` are assumed
  in `_PLATFORM_BY_ENGINE`.
* Atlas database users can only be changed through the Atlas Admin API, so the Password
  Safe native MongoDB platform is unlikely to be able to rotate an Atlas user. That makes
  Atlas the first consumer of the custom-plugin hook.

## Password Safe reachability: native now, a custom plugin next

Native onboarding works only where the **Resource Broker** serving the asset's workgroup
can reach the listener. Two network shapes, and the dashboard creates neither path:

* a **public** endpoint: allow-list the broker's public IP on the database (for Atlas, put it
  in `atlas_extra_access_cidrs`);
* a **private** endpoint (RDS Oracle, which stays private by decision, 2026-10-07): the
  broker has to sit in or route into the VPC.

The planned answer for private endpoints is a **custom plugin** that reaches the database
through a cloud control plane — an API, AWS SSM or Azure Run Command — the same pattern as
the `dbssm` / `dbazure` / `dbgcp` plugins. When it exists, onboarding switches from the
native path to the plugin path whenever its platform is configured (a
`clouddb_ps_platform_<cloud>_oracle` key), and the plugin's address grammar gets a validator
in `ps_resource_service` beside the others. Until then `_PS_NATIVE_ENGINES` is the only route.

## Where the swap happens when the provider ships

`terraform_pra_service._DB_TUNNEL_RESOURCE` and `_DB_TUNNEL_TYPE` are the whole change on
the generation side. An existing tcp tunnel lives in its job's stored state as an
`sra_protocol_tunnel_jump`, and the destroy path regenerates it from that state
(`_engine_from_tunnel_state`, `tunnel_definitions`). Old rows therefore keep
decommissioning cleanly; moving them to the new resource is a destroy + re-broker, not a
state move.
