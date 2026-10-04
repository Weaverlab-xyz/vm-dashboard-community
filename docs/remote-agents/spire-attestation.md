# Attesting an agent through SPIRE

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want a remote agent to hold no signing key on disk, are moving an enrolled agent from Ed25519 to SPIRE, or an attested agent stopped authenticating.

Part of [Remote Agents](../remote-agents.md). By default an agent enrols with a one-time
code and keeps an Ed25519 key in its state volume (`identity.json`), and whoever can read
that volume *is* the agent. With SPIRE, the agent proves who it is at every start with a
short-lived JWT-SVID from the dashboard's own SPIRE server, and keeps its signing key only
in memory. Ed25519 stays the default and stays supported; nothing changes for an agent you
do not migrate.

**Preview.** Built and covered by tests, but not yet run against a live agent host. The
design is [SPIRE for agents, Dex for people](../design/agent-and-human-identity.md#part-1-the-agent-attests-through-spire).

## What runs where

| Where | What | From |
|---|---|---|
| The dashboard host | a SPIRE server for the dashboard's trust domain | [`docker-compose.spire.yml`](../../docker-compose.spire.yml), layered over `docker-compose.yml` |
| The agent host | `spire-agent`, which attests the host; `spiffe-helper`, which keeps a JWT-SVID in a memory-only volume as the agent's uid (10001); the agent, which reads it | [`examples/remote-agent/docker-compose.spire.yml`](../../examples/remote-agent/docker-compose.spire.yml), layered over the agent's `docker-compose.yml` |

The agent needs **2.6.0 or later** (`AGENT_SPIFFE_JWT_FILE`; see the
[agent README](../../runners/agent/README.md)). The Agents page banner says "update the
image first" for an older one.

## Ports

| From | To | Port | Notes |
|---|---|---|---|
| the agent host | the dashboard's SPIRE server | tcp/8081 (`SPIRE_BIND_PORT`) | mTLS gRPC. **Not** through Caddy, and not through a TLS-inspecting proxy: ask for a bypass for this host:port, or keep that agent on Ed25519 |
| the agent | the dashboard's agent gateway | as today | one single-use, audience-bound token per start, then ordinary signed requests |

The server's admin API is a unix socket on a volume mounted only into the dashboard's own
containers. Nothing administrative listens on the network. tcp/8082
(`SPIRE_FEDERATION_PORT`) is the bundle endpoint for
[federation with a Workload Lab](../workload-lab/spiffe.md#federating-with-the-dashboard);
attestation does not use it.

## Setting it up

1. **Start the dashboard's SPIRE server.**

   ```bash
   SPIRE_TRUST_DOMAIN=dashboard.example.com \
   docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
   ```

   ```powershell
   $env:SPIRE_TRUST_DOMAIN = 'dashboard.example.com'
   docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
   ```

2. **Pin the agent audience** under **Settings → Integrations → Remote Agents**, if it is
   not already. The agent host reaches the SPIRE server at that URL's host name.
3. **Tick "Let agents attest through SPIRE"** (`spire_attest_enabled`) on the same panel.
   Leave **SPIRE server container** (`spire_server_container`) at `vmdash-spire-server`
   unless you renamed the container.
4. **Migrate the agent.** On the **Agents** page, open the agent and press **Migrate to
   SPIRE**. In one step the dashboard:
   - creates a one-use **join token** for `spiffe://<td>/node/<agent-id>`, valid for 15
     minutes;
   - creates (or reuses) the workload entry `spiffe://<td>/agent/<agent-id>`, selecting
     `unix:uid:10001` under that node;
   - registers its own trust domain, so attestation can be verified;
   - **binds** the agent to that SPIFFE ID. Binding does not cut off the agent's current
     key. That happens when it first attests.

   The modal shows the token once, the `.env` lines and the trust bundle.
5. **On the agent host**, in the agent's directory:
   - save the trust bundle as `spire/bootstrap.crt`;
   - add the `SPIRE_SERVER_ADDRESS`, `SPIRE_TRUST_DOMAIN` and `SPIRE_JOIN_TOKEN` lines to
     `.env`, beside `DASHBOARD_URL`;
   - start it with the overlay:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
   ```

   The join token is spent on first use. If 15 minutes pass first, press **Migrate to
   SPIRE** again: it reuses the entry and mints a new token.
6. **Check it.** The agent's detail on the Agents page reads **SPIRE (key in memory)**
   instead of **Ed25519 (key on disk)**. The attestation is audited as `agent.attest`.

### Cloud VMs with no key at rest

On a cloud VM the agent host can attest with the instance's own identity (`aws_iid`,
`azure_imds`, `gcp_iit`), so it stores no SPIRE key either. **Migrate to SPIRE** does not
create those node entries: the node's SPIFFE ID comes from the instance and is not known
until it attests. Create the node entry by hand on the dashboard's SPIRE server, then
migrate as above.

## Then: keep credentials in the dashboard

An attested agent is the reason to store credentials centrally rather than on the host:
nothing on the host's disk can ask for them any more. See
[Central storage with SPIRE](credentials.md#central-storage-with-spire-the-recommended-model),
and tick **Release dashboard-held credentials only to SPIRE-attested agents**
(`dashboard_secrets_require_spire`) once every agent that needs a credential has migrated.

## Rolling back

Drop the overlay on the agent host, and **re-issue an enrolment code** for the agent. That
also clears its SPIFFE binding and puts it back on Ed25519.

## Settings

All on **Settings → Integrations → Remote Agents**.

| Setting | Key | Default | What it does |
|---|---|---|---|
| Let agents attest through SPIRE | `spire_attest_enabled` | off | Turns on `POST /api/agent/attest`, the **Migrate to SPIRE** button and the banner |
| SPIRE server container | `spire_server_container` | `vmdash-spire-server` | The container the dashboard drives with `docker exec` for entries, join tokens and the daily trust-bundle re-read |
| Release dashboard-held credentials only to SPIRE-attested agents | `dashboard_secrets_require_spire` | off | Hypervisor credentials, Gateway deploy keys, Config-Management bundles and file-share passwords go only to agents whose key came from a SPIRE attestation |
| Give the dashboard its own SPIFFE identity | `dashboard_spiffe_identity_enabled` | off | Not needed for attestation; see [The dashboard's own SPIFFE identity](dashboard-identity.md) |

The overlay's environment variables: `SPIRE_TRUST_DOMAIN` (required), `SPIRE_BIND_PORT`
(8081), `SPIRE_FEDERATION_PORT` (8082) and `SPIRE_JWT_ISSUER` (only for the dashboard's own
identity).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| The SPIRE agent container cannot connect | a TLS-inspecting proxy, or tcp/8081 not published to the agent host | a proxy bypass for the server's host:port, or stay on Ed25519 |
| The join token is refused | spent, or older than 15 minutes | press **Migrate to SPIRE** again for a new one |
| The agent waits at start instead of authenticating | its last token was already presented, and a replay is refused | nothing: it waits for spiffe-helper's next rotation, half the token's lifetime |
| **Migrate to SPIRE** says the server is not running | the overlay is not up, or **SPIRE server container** names another container | start the overlay, or correct the setting |
| `403 … only to agents attested through SPIRE` | the release setting is on and this agent is still Ed25519 | migrate it, or keep its credential on its host for now |
