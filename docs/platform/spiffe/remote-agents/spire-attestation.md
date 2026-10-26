# Attesting an agent through SPIRE

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want a remote agent to hold no signing key on disk, are moving an enrolled agent from Ed25519 to SPIRE, or an attested agent stopped authenticating.

Part of [Remote Agents](../remote-agents.md). **SPIRE is the recommended way to run an
agent**, because the dashboard does not trust an agent until it proves what it is. With
SPIRE, the agent proves it at every start with a short-lived JWT-SVID from the dashboard's
own SPIRE server, and keeps its signing key only in memory. Without it, an agent enrols
with a one-time code and keeps an Ed25519 key in its state volume (`identity.json`), and
whoever can read that volume *is* the agent. Ed25519 stays the enrolment default, for
compatibility, and stays supported for sites that cannot reach a SPIRE server
(a TLS-inspecting proxy, a dashboard on Cloud Run); nothing changes for an agent you do not
migrate. Why this is the recommendation, and what comes after it, is on
[Workload identity](../../workload-identity.md#remote-agents).

Proven live on 2026-10-13: a Docker Desktop agent attested to a dashboard on Azure
Container Apps, and POV control nodes have attested the same way since 2026-10-14. The
design is [SPIRE for agents, Dex for people](../../../design/agent-and-human-identity.md#part-1-the-agent-attests-through-spire).
The server itself, and every other feature that uses it, is on
[SPIFFE and SPIRE](../../spiffe.md).

## What runs where

| Where | What | From |
|---|---|---|
| The dashboard host | a SPIRE server for the dashboard's trust domain | [`docker-compose.spire.yml`](../../../../docker-compose.spire.yml), layered over `docker-compose.yml`; on a managed runtime, [a sidecar of the app](../../spiffe.md#on-a-managed-container-runtime) |
| The agent host | `spire-agent`, which attests the host; `spiffe-helper`, which keeps a JWT-SVID in a memory-only volume as the agent's uid (10001); the agent, which reads it | [`examples/remote-agent/docker-compose.spire.yml`](../../../../examples/remote-agent/docker-compose.spire.yml), layered over the agent's `docker-compose.yml` |

The agent needs **2.6.0 or later** (`AGENT_SPIFFE_JWT_FILE`; see the
[agent README](../../../../runners/agent/README.md)). The Agents page banner says "update the
image first" for an older one.

## Ports

| From | To | Port | Notes |
|---|---|---|---|
| the agent host | the dashboard's SPIRE server | tcp/8081 (`SPIRE_BIND_PORT`) | mTLS gRPC. **Not** through Caddy, and not through a TLS-inspecting proxy: ask for a bypass for this host:port, or keep that agent on Ed25519 |
| the agent | the dashboard's agent gateway | as today | one single-use, audience-bound token per start, then ordinary signed requests |

The server's admin API is a unix socket on a volume mounted only into the dashboard's own
containers. Nothing administrative listens on the network. tcp/8082
(`SPIRE_FEDERATION_PORT`) is the bundle endpoint for
[federation with a Workload Lab](../../../preview/workload-lab/spiffe/federation.md);
attestation does not use it.

## Setting it up

1. **Start the dashboard's SPIRE server.** On Docker Compose, layer the overlay as below.
   On Azure Container Apps, Cloud Run, ECS or Kubernetes, it is a sidecar of the app
   instead: see [Running it on a managed container runtime](../../spiffe.md#on-a-managed-container-runtime).

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
   On **Settings → Integrations → Platform → SPIFFE and SPIRE**, leave **SPIRE server
   container** (`spire_server_container`) at `vmdash-spire-server` unless you renamed the
   container.
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
   - add the `SPIRE_SERVER_ADDRESS`, `SPIRE_SERVER_PORT`, `SPIRE_TRUST_DOMAIN` and
     `SPIRE_JOIN_TOKEN` lines to `.env`, beside `DASHBOARD_URL`;
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

## Then: upgrade it from the dashboard

An attested agent is also the one the dashboard can upgrade and restart, because its
identity survives a new container. Add the updater and opt in through `policy.yaml`; see
[Upgrading and restarting agents centrally](lifecycle.md). A restart also rotates an
attested agent's key, and from agent 2.11 so does the agent itself while it runs (below).

## Agents the dashboard deploys into a cloud

A host the dashboard deploys can prove itself with the VM's own identity document instead
of a join token: `aws_iid`, `azure_imds` or `gcp_iit`. That way no secret is written into
its user-data. Enable the plugin on the SPIRE server, then tick the cloud under
**Settings → SPIFFE and SPIRE**. See
[Deploying an agent into a cloud → Identity](deploying.md#identity).

## Ongoing checks

Attestation proves who the agent is at each start. After that, the dashboard keeps
checking what it can see:

- **Every attestation:** what it replaced, and the policy hash.
- **Every signed request:** which key signed it.
- **Every lease:** the version, policy hash and re-attest interval the agent reports.

When something moves that a person should look at, the agent's row gets an amber
**attention** badge. The details panel says what happened, and an `agent.attention`
notification goes to whatever endpoints you route it to. Nothing here refuses work by
itself; revoking stays your call.

| Item | What it means | What to do |
|---|---|---|
| **Two copies are running** | A request was signed with the key this agent's last attestation replaced. Another process attested as the agent, and the older one is still polling. The request was refused. | If you did not start a second copy (for example, a cloned VM or a duplicated compose project), revoke the agent and find the other one. The item names the address it last attested from. |
| **policy.yaml changed** | The policy hash the agent reports moved. | Confirm that whoever owns the host meant to change the file. |
| **Not a recorded release** | The agent reports a version, 2.10 or later, that is not a release recorded under **Lifecycle**. Builds from a release are signed. | Record the release, or find out who built the image. |
| **Key not rotated** | An attested 2.11+ agent that re-attests every N hours has not done so in twice that, while online. | Check its log for re-attestation errors, then restart it. |

Each item is raised once until someone presses **Acknowledge**, which is audited along with
what it cleared.

**Re-attesting while running (agent 2.11).**
- **How often.** An attested agent attests again every `AGENT_REATTEST_HOURS`, default 24,
  with a fresh in-memory key.
- **Only between jobs.** When it is due, the agent takes no new jobs until the running ones
  finish, waiting up to 15 minutes. That way no request is ever signed by a key the
  dashboard just replaced.
- **On failure** it keeps its current key and tries again in an hour.
- **Turning it off.** `0` turns it off; a restart still attests.

**Revoking removes the SPIRE registration.**
- **What is removed.** Revoking an agent, unbinding its SPIFFE ID, or re-issuing its
  enrolment code deletes the workload entry the dashboard created and evicts its node. SPIRE
  then stops issuing SVIDs for an identity the dashboard no longer trusts.
- **What is left alone.** A SPIFFE ID you bound by hand lives in a SPIRE server you manage,
  so it is left to you.
- **When the server is down.** The revoke still happens, and the response lists what was
  left behind (`spire_left_behind`) so you can remove it with `spire-server entry delete`
  and `spire-server agent evict`.

## Rolling back

Drop the overlay on the agent host, and **re-issue an enrolment code** for the agent. That
also clears its SPIFFE binding, removes its SPIRE registration, and puts it back on Ed25519.

## Settings

The agent settings are on **Settings → Integrations → Remote Agents**:

| Setting | Key | Default | What it does |
|---|---|---|---|
| Let agents attest through SPIRE | `spire_attest_enabled` | off | Turns on `POST /api/agent/attest`, the **Migrate to SPIRE** button and the banner |
| Release dashboard-held credentials only to SPIRE-attested agents | `dashboard_secrets_require_spire` | off | Hypervisor credentials, Gateway deploy keys, Config-Management bundles and file-share passwords go only to agents whose key came from a SPIRE attestation |

The server's own settings, **SPIRE server container** (`spire_server_container`) and
**SPIRE server address for agents** (`spire_server_address`), are on **Settings →
Integrations → Platform → SPIFFE and SPIRE**: see
[SPIFFE and SPIRE → Settings](../../spiffe.md#settings-and-environment). The dashboard's own
SPIFFE identity is on the same panel and is not needed for attestation; see
[The dashboard's own SPIFFE identity](../../oidc/dashboard-identity.md).

The server's environment variables, on the overlay, the sidecar and the dashboard's own
containers, are on [SPIFFE and SPIRE](../../spiffe.md#settings-and-environment).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| The SPIRE agent container cannot connect | a TLS-inspecting proxy, or tcp/8081 not published to the agent host | a proxy bypass for the server's host:port, or stay on Ed25519 |
| The join token is refused | spent, or older than 15 minutes | press **Migrate to SPIRE** again for a new one |
| After the host was off, the SPIRE agent logs `SVID is expired` then `join token does not exist or has already been used`, and restarts in a loop. The dashboard agent warns `still no JWT-SVID` after a minute, then exits after six | the agent's own SPIRE identity (7 days at most, about 3.5 at least) ran out while the host was offline, and a join-token agent cannot attest again with its spent token | press **New SPIRE token** on the agent's row, put the new `SPIRE_JOIN_TOKEN` in its `.env` and `up -d` again. Never `down -v` |
| The agent waits at start instead of authenticating | its last token was already presented, and a replay is refused | nothing: it waits for spiffe-helper's next rotation, half the token's lifetime |
| **Migrate to SPIRE** says the server is not running | the server, not the agent | [SPIFFE and SPIRE → Troubleshooting](../../spiffe.md#troubleshooting) |
| `403 … only to agents attested through SPIRE` | the release setting is on and this agent is still Ed25519 | migrate it, or keep its credential on its host for now |
