# SPIFFE and SPIRE

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are running the dashboard's own SPIRE server (on Docker Compose or a managed container runtime), deciding which SPIFFE feature you need, or a SPIFFE feature fails and you need to know whether the server is the cause.

[SPIFFE](https://spiffe.io/) is a standard for naming a workload (`spiffe://<trust
domain>/<path>`) and handing it short-lived, verifiable credentials: SVIDs, as an X.509
certificate or a JWT. [SPIRE](https://spiffe.io/docs/latest/spire-about/) is the
reference implementation. A SPIRE server holds a trust domain's keys. A SPIRE agent proves
which machine it runs on (node attestation) and hands SVIDs to the processes on it
(workload attestation).

The dashboard uses SPIFFE in two quite different ways:

- **It runs a SPIRE server of its own**, for *its* trust domain. Remote agents attest to
  it, the dashboard mints its own cloud tokens from it, and service accounts and demo
  cells can be issued identities in it. One per install, and this page is its home.
- **It builds and governs other people's SPIRE servers.** The Workload Lab stands up
  SPIRE trust domains for demos, and the Password Safe **SPIFFE SVID** plugin governs
  them. Those servers are covered on [their own page](workload-lab/spiffe.md).

## Where SPIFFE shows up

| Feature | What SPIFFE does there | Trust domain | Read |
|---|---|---|---|
| Remote agent attestation | an agent proves itself at every start with a JWT-SVID, and keeps its signing key only in memory | the dashboard's | [Attesting an agent through SPIRE](remote-agents/spire-attestation.md) |
| The dashboard's own identity | the dashboard reaches AWS, Azure, GCP, k3s and Workload Credentials with tokens its own server signs, instead of a stored key | the dashboard's | [The dashboard's own identity](oidc/dashboard-identity.md) |
| Service accounts | a workload calls the dashboard's API with its SVID and holds no dashboard secret | any registered one, including the dashboard's | [SPIFFE workloads](access/service-accounts.md#spiffe-workloads-authenticate-with-the-svid-hold-nothing) |
| POV control node agents | a POV's control node attests at every start, falling back to its enrolment code where the guest cannot reach the server | the POV instance's | [SPIRE on the control node](profiles/pov/skytap.md#spire-on-the-control-node) |
| Agent Demo Cell | a demo agent attests, then reaches PRA, Password Safe and the clouds with no static secret on its host | the dashboard's, or a lab's | [Agent Demo Cell](profiles/demo/agent-demo-cell.md) |
| Workload Lab | builds a SPIRE trust domain on a VM, Docker or k8s, governs it with the SPIFFE SVID plugin, and federates it with the dashboard's | a lab's | [Workload Lab: SPIFFE and SPIRE](workload-lab/spiffe.md) |
| Proving it live | step-by-step checks against real clouds and a real BeyondInsight | both | [SPIRE lab standup](runbooks/spire-lab-standup.md), [Federation verification](runbooks/spiffe-federation-verification.md) |

The designs behind these are
[SPIRE for agents, Dex for people](design/agent-and-human-identity.md),
[The dashboard as a SPIFFE workload](design/dashboard-workload-identity.md) and
[A workload reaching Kubernetes with a short-lived token](design/workload-k8s-short-lived-token.md).

Every one of these is **off by default, for compatibility**: nothing changes for an install
that does not turn one on. They are, however, the **recommended** way to run: an identity
proved at every start replaces a key, token or secret stored on disk, and pairing it with
just-in-time privilege removes standing privilege too. See
[Workload identity: the recommended credential model](access/workload-identity.md). Ed25519
agent enrolment and stored cloud keys stay supported, as the quick start and for sites that
cannot reach a SPIRE server.

## The dashboard's trust domain

You pick the name once, as `SPIRE_TRUST_DOMAIN` (for example `dashboard.example.com`).
Renaming it later creates a new trust domain: every agent would have to be migrated again
and every cloud trust policy rewritten.

The dashboard owns these paths in it, and refuses to bind any other client to the
reserved ones:

| SPIFFE ID | Who holds it | Reserved |
|---|---|---|
| `spiffe://<td>/agent/<agent id>` | a remote agent, under the node `spiffe://<td>/node/<agent id>` | yes |
| `spiffe://<td>/dashboard` | the dashboard itself: app and worker share it | yes |
| `spiffe://<td>/demo/agent-cell/<cell id>` | an Agent Demo Cell | no |
| `spiffe://<td>/workload/…` | a service-account workload | no |
| `spiffe://<td>/spire/server` | the server's own bundle endpoint, for federation | — |

The dashboard registers its own trust domain under **Users → SPIFFE trust domains**
(created by `dashboard-spire`). It re-reads the bundle once a day, which is ahead
of SPIRE's own key rotation. It also re-reads it at once when an assertion names a key it
does not hold yet, at most once a minute. It never overwrites a registration of the same
name made by hand or by a SPIRE lab, and says so.

| Port | Who reaches it | What |
|---|---|---|
| tcp/8081 | agent hosts | the agent API: mTLS gRPC. TCP passthrough only, never through Caddy, an HTTP load balancer or a TLS-inspecting proxy |
| tcp/8082 | a federated Workload Lab's server | the SPIFFE bundle endpoint: TLS with the server's own SVID, so also passthrough only. Leave it closed if you federate nothing |
| admin socket | the dashboard's own containers only | entries, join tokens, JWT minting, the bundle. A unix socket on a volume; **nothing administrative listens on the network** |
| tcp/8088 | the platform's probes (sidecar only) | `/live`, `/ready`. Never published |

## How the dashboard drives it

Always with the server's own CLI and its `-output json`: no gRPC client, no new
dependency. **How** that CLI reaches the server is chosen per process, because the app and
the worker can differ:

| Transport | Used when | How |
|---|---|---|
| `docker` | the process has a Docker socket (Docker Compose: app and worker both do) | `docker exec vmdash-spire-server spire-server …` |
| `socket` | `SPIRE_SERVER_SOCKET` is set (the app on a managed runtime) | the `spire-server` binary in the dashboard image, against the sidecar's admin socket |
| `relay` | neither (the worker on a managed runtime) | the call goes through the database to an app process that has the socket |

`SPIRE_SERVER_TRANSPORT=docker|socket|relay` overrides the choice. The same argument list
and the same result checks serve all three, so a feature cannot tell which one answered.

**The relay.** A process with neither socket writes each call to the `spire_admin_calls`
table. An app process with the socket claims the row (the same `UPDATE … WHERE
status='pending'` claim the job queue uses), runs it and writes the result back. The
requester then reads the result and deletes the row. The database is the one thing every
deployment already shares on every platform, so this needs no port, no route between the
two services, and no credential of its own:

- **Encrypted.** A join token or JWT-SVID in transit is encrypted with the same key as
  `app_config`.
- **Allow-listed.** The serving side runs only the subcommands the dashboard uses.
- **Fast, and fails fast.** A call takes about half a second. If no app process with a
  socket has checked in within the last 20 seconds, the call fails at once with a message
  that says so, rather than after a timeout.

## Running it

### On Docker Compose

Layer the overlay over the base compose file. `SPIRE_JWT_ISSUER` is needed only for
[the dashboard's own identity](oidc/dashboard-identity.md#setting-it-up):

```bash
SPIRE_TRUST_DOMAIN=dashboard.example.com SPIRE_JWT_ISSUER=https://agents.example.com/spiffe docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
```

```powershell
$env:SPIRE_TRUST_DOMAIN = 'dashboard.example.com'
$env:SPIRE_JWT_ISSUER   = 'https://agents.example.com/spiffe'
docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
```

[`docker-compose.spire.yml`](../docker-compose.spire.yml) runs the server as
`vmdash-spire-server`, keeps its datastore and CA keys on the `spire_data` volume, and
publishes 8081 and 8082. Back that volume up: losing it re-keys the trust domain.

### On a managed container runtime

Azure Container Apps, GCP Cloud Run, AWS ECS/Fargate and a Kubernetes pod all run the same
shape. [`docker-compose.spire.yml`](../docker-compose.spire.yml) cannot be lifted
across as it is, for three reasons that have nothing to do with SPIRE:

- **There is no Docker socket**, so the dashboard cannot `docker exec` into a server.
- **No volume crosses two deployments.** The app and the worker are separate services
  ([Cloud Hosting → The shape](operations/cloud-hosting.md#the-shape)), and a pod's
  volume is that pod's only.
- **Nothing on disk survives a restart.** A server keeping its CA keys and datastore on
  container disk re-keys the trust domain on every revision, and every attested agent is
  stranded with nothing in the logs saying why.

So on these platforms the server is a **sidecar of the app**, built from
[`examples/cloud-spire`](../examples/cloud-spire/Dockerfile), and each of those three
gets an answer:

```
 app: one pod / task / replica                   worker: its own deployment
 ┌─────────────────────────────────────────┐     ┌─────────────────────────────┐
 │ gateway ──▶ app ──┐                     │     │ worker                      │
 │                   │ spire-server CLI    │     │ no socket, no Docker        │
 │                   ▼                     │     └──────────────┬──────────────┘
 │      /run/spire-server   (pod volume)   │                    │ each call
 │                   ▲                     │                    ▼
 │ spire ────────────┘ admin socket        │ ◀─ claims, runs, ─ spire_admin_calls
 │   :8081 agents   :8082 federation       │    answers         (dashboard's DB)
 └────────────────────┬────────────────────┘
                      ▼
        PostgreSQL `spire` database: the server's datastore
```

| What | Compose | Managed runtime |
|---|---|---|
| How the app reaches the server | `docker exec vmdash-spire-server …` | the `spire-server` CLI in the app image, against the sidecar's admin socket (`SPIRE_SERVER_SOCKET`) |
| How the worker reaches it | the same `docker exec` | **relayed through the database** to an app process ([above](#how-the-dashboard-drives-it)). Nothing to configure |
| Datastore | SQLite on the `spire_data` volume | PostgreSQL, a `spire` database on the server the dashboard already uses |
| Trust anchor | the server's own CA keys on that volume | an **upstream CA** you supply as two secrets. The server's own keys are in memory; every one it makes is signed by that same root |
| The dashboard's own JWT-SVID files | one `spiffe_tokens` volume: the app writes it, the worker reads it | each container keeps its own copy in `/run/spiffe-tokens`; the worker mints its copy through the relay |

**The worker relays** through the database, as described under [How the dashboard drives it](#how-the-dashboard-drives-it). There is nothing to configure on it.

#### The upstream CA

Generate it once, offline, and keep it the way you keep `JWT_SECRET_KEY`. **The private
key is the trust domain.** Lose it, and every agent has to be migrated again. Leak it,
and whoever has it can mint identities in your trust domain.

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 -subj "/O=vm-dashboard/CN=dashboard SPIRE upstream CA" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout spire-ca.key -out spire-ca.crt
```

Store both files as platform secrets (`SPIRE_UPSTREAM_CA_CERT`, `SPIRE_UPSTREAM_CA_KEY`).
The sidecar refuses to start without them rather than fall back to keys that die with the
container. The bundle that **Migrate to SPIRE** hands an agent host as `bootstrap.crt` is
this certificate, so it stays valid across every restart and every replica.

SPIRE logs `UpstreamAuthority plugin does not support JWT-SVIDs` at start. That is
expected: JWT signing keys stay the server's own, and a restart starts signing with a new
one at once. Two things keep that from breaking anything:

- The previous keys stay in the bundle in PostgreSQL until they expire.
- An agent token signed with a key the dashboard has not stored yet makes it re-read its
  own bundle on the spot, at most once a minute.

The dashboard's `/spiffe/keys` endpoint always reads the live bundle.

#### The datastore

Create a database for the server on the PostgreSQL the dashboard already uses. Keep it out
of the dashboard's own database: SPIRE manages its own schema.

```sql
CREATE DATABASE spire;
```

`SPIRE_DATASTORE_URL` is a secret:
`postgresql://<user>:<password>@<host>:5432/spire?sslmode=require`. Several replicas may
share one datastore and one upstream CA. That is SPIRE's own HA shape, and each replica's
JWT key is published in the shared bundle.

The sidecar holds at most **5** connections to it (`max_open_conns`), per replica. Count
them against the server's limit next to the dashboard's own pools: a small tier such as
Azure's B1ms allows 50 in all.

#### The sidecar

```bash
docker build -t <registry>/dash-spire:1 examples/cloud-spire
docker push <registry>/dash-spire:1
```

| Variable | | |
|---|---|---|
| `SPIRE_TRUST_DOMAIN` | required | e.g. `dashboard.example.com`. Pick it once: renaming it creates a new trust domain |
| `SPIRE_DATASTORE_URL` | required, secret | above |
| `SPIRE_UPSTREAM_CA_CERT`, `SPIRE_UPSTREAM_CA_KEY` | required, secret | the PEMs above. Each also takes a `*_FILE` variant naming a mounted secret file, where the platform can mount one |
| `SPIRE_JWT_ISSUER` | optional | only for [the dashboard's own identity](oidc/dashboard-identity.md) |
| `SPIRE_RUN_UID` | optional | default `10001`, the dashboard's uid. SPIRE makes the admin socket `0770`, so the server and the app must share it |

It mounts a pod-local volume at **`/run/spire-server`**, and so does the app container.
Probe it with `httpGet` on **8088**: `/live` for liveness, `/ready` for readiness.
**Never publish 8088.** The sidecar starts as root only to take ownership of that volume
and write the CA files `0600`. It then runs as `SPIRE_RUN_UID`.

#### The app container

Two additions, both additive:

- `SPIRE_SERVER_SOCKET=/run/spire-server/api.sock`, which switches the app from `docker
  exec` to the sidecar's socket;
- the same volume mounted at `/run/spire-server`.

The app image must be from this release or later, because that is the first one to carry
the `spire-server` CLI at `/usr/local/bin/spire-server`. The worker needs **no change at
all**: it has neither socket, so it relays, and it keeps its own token files.

#### Publishing 8081 to agents

The agent's connection is mTLS gRPC and must reach the server **untouched**: TCP
passthrough, never an HTTP load balancer or a TLS-inspecting proxy. Port 8082, for
[federation with a Workload Lab](workload-lab/spiffe.md#federating-with-the-dashboard),
has the same requirement. Restrict both to the networks that need them.

| Platform | 8081 for agents | Notes |
|---|---|---|
| **Azure Container Apps** | an `additionalPortMappings` entry on the app's ingress: `external: true`, `targetPort: 8081`, `exposedPort: 8081` (and the same for 8082) | TCP ports are per environment, so each exposed port can be used by only one app in it. The agent gateway's custom domain resolves to the same environment address, so **SPIRE server address for agents** can stay blank. [Walkthrough](operations/cloud-hosting/azure-container-apps.md#spire-sidecar) |
| **AWS ECS/Fargate** | a **Network Load Balancer** with TCP listeners on 8081/8082, target group type `ip` on the sidecar's container ports | Not the ALB, which terminates TLS. The NLB has its own DNS name: put it in **SPIRE server address for agents** (`spire_server_address`). [Notes](operations/cloud-hosting/aws-ecs.md#spire-sidecar) |
| **Kubernetes** | a `Service` of type `LoadBalancer` (or `NodePort`) on the pod's 8081/8082 | Same as ECS when the load balancer's name differs from the agent gateway's |
| **GCP Cloud Run** | **not possible.** Cloud Run ingress is HTTPS only and terminates TLS itself | The sidecar still gives the dashboard [its own identity](oidc/dashboard-identity.md) and Workload Lab federation in the dashboard's direction. Agents stay on Ed25519. [Notes](operations/cloud-hosting/gcp-cloud-run.md#spire-sidecar) |

The **Migrate to SPIRE** modal hands the agent host whatever that setting resolves to,
as `SPIRE_SERVER_ADDRESS` and `SPIRE_SERVER_PORT`.

### Protecting the keys

Whoever holds the trust domain's signing keys can mint every identity in it: agents, the
dashboard's own cloud tokens, service-account SVIDs. Where those keys live depends on how
the server runs:

| How it runs | The keys | Protect |
|---|---|---|
| Docker Compose | `keys.json` on the `spire_data` volume (`KeyManager "disk"`) | the volume, as you would the key itself. On a cloud VM, use that cloud's KMS instead (below) |
| A managed runtime | the server's own keys are in memory and die with it; the root that matters is the **upstream CA** | the two upstream CA secrets, as you would `JWT_SECRET_KEY` |

On a host with a cloud identity of its own, the server's keys can live in that cloud's
KMS. `examples/spire-server/server.conf` carries a commented block for each:

| Host | KeyManager | The SPIRE container needs |
|---|---|---|
| AWS | `aws_kms` | an instance role allowed to create and use KMS keys |
| Azure | `azure_key_vault` | a managed identity with key permissions on the vault |
| GCP | `gcp_kms` | a service account with KMS rights on the key ring |

Comment out the `disk` line, uncomment one block, fill it in, and restart the
`spire-server` container. Check the field names against SPIRE 1.15's
`server_keymanager_*` plugin docs first: the examples are starting points.

**Switching creates a new CA.** The old keys stay in `keys.json`, and the server will not
read them through a KMS plugin. Every agent attested to the old CA has to attest again,
and the published JWT keys change. Each cloud re-fetches `/spiffe/keys` on its own
schedule, so expect federated calls to fail for a while after the switch. Switch before
you migrate agents, or plan the re-attestation. A self-hosted Docker host with no cloud
identity has nothing to authenticate to a KMS with, so it stays on `disk`.

The app administers this server, so anyone who compromises the app can mint tokens for
any identity in it while they are present. That is still better than a stored key, which
can be copied and used offline for ever. It is not "the app holds nothing".

## Settings and environment

The switches that **use** the server are on the feature pages:
- [Let agents attest through SPIRE](remote-agents/spire-attestation.md#settings) (`spire_attest_enabled`);
- [Give the dashboard its own SPIFFE identity](oidc/dashboard-identity.md#settings) (`dashboard_spiffe_identity_enabled`).
- On a POV instance, [AWS for the POV cloud provider with no stored key](profiles/pov/public-cloud.md#keyless-aws-the-dashboards-spiffe-identity), which uses that identity.

These two are about the server itself, both on **Settings → Integrations → Remote
Agents**:

| Setting | Key | Default | What it does |
|---|---|---|---|
| SPIRE server container | `spire_server_container` | `vmdash-spire-server` | The container the `docker` transport execs into. Ignored where `SPIRE_SERVER_SOCKET` is set |
| SPIRE server address for agents | `spire_server_address` | blank | `host` or `host:port` an agent host dials for SPIRE. Blank means the agent audience's host on 8081. Set it when 8081 is published under another name, such as an AWS NLB's |

Environment, on the dashboard's containers (all optional):

| Variable | What it does |
|---|---|
| `SPIRE_SERVER_SOCKET` | The sidecar's admin socket in this container, normally `/run/spire-server/api.sock`. Set it on the app container on a managed runtime |
| `SPIRE_SERVER_TRANSPORT` | `docker`, `socket` or `relay`, to override the choice above |
| `SPIFFE_TOKEN_DIR` | Where the dashboard's own JWT-SVID files go. Default `/run/spiffe-tokens` |

On the compose overlay: `SPIRE_TRUST_DOMAIN` (required), `SPIRE_BIND_PORT` (8081),
`SPIRE_FEDERATION_PORT` (8082) and `SPIRE_JWT_ISSUER`. On the sidecar: see
[The sidecar](#the-sidecar).

## Troubleshooting

Every SPIFFE feature reports a server problem in the same words, so start here when one
says the server "is not running".

| Symptom | Likely cause | Fix |
|---|---|---|
| "the SPIRE server container … is not running" | Compose: the overlay is not up, or **SPIRE server container** names another container | start the overlay, or correct the setting |
| "the SPIRE server sidecar is not running" | the sidecar has not started, has crashed (its log names the missing variable), or does not mount the same volume at `/run/spire-server` as the app | check the sidecar's log and its `/ready` probe |
| The sidecar crash-loops with `datastore-sql: stat /root/.postgresql/postgresql.crt: permission denied` | a sidecar image built before this was fixed. With `sslmode=require` the Postgres driver looks for a client certificate under `$HOME`, and `HOME` was still root's unreadable `/root` | rebuild the image from the current `examples/cloud-spire`, which points `HOME` at the server's own data directory |
| "refused this container on its admin socket" | the sidecar runs as a different uid from the app | leave `SPIRE_RUN_UID` at 10001, or set it to the app's uid |
| "the spire-server binary is not in this image" | the app runs an image older than the managed-runtime support | update the app image |
| A worker job fails with "the SPIRE server relay is not running" | no app process with `SPIRE_SERVER_SOCKET` has checked in for 20 seconds: the app is restarting, or runs an older image | wait for the app, or update its image. The worker needs no setting of its own |
| "the trust domain … is already registered" | a SPIRE lab, or someone by hand, registered a trust domain with the same name | remove that registration, or give this server another `SPIRE_TRUST_DOMAIN` |
| A POV control node comes up **Ed25519** and its job says the address "answers with a SPIRE server this dashboard does not trust" | **SPIRE server address for agents** is blank or wrong, and that address reaches another dashboard's server (a shared Container Apps environment publishes each one on its own port) | set it to `host:port` for this dashboard's server, then press **Rebuild control node** again |
| Every attested agent fails after the server restarts | the trust anchor changed: the compose volume was lost, or the sidecar's upstream CA secret is not the same on every revision | restore the volume or the CA. An agent bootstrapped with another root must be migrated again |
| AWS refuses the dashboard's token with `InvalidIdentityToken` | AWS could not fetch `<issuer>/keys`: on a managed runtime, the cloud gateway image predates `/spiffe/*` on the agent hostname, so it answers 404 | rebuild the gateway from `examples/cloud-gateway` and roll the sidecar, then **Check the issuer is reachable** |
| A cloud refuses the dashboard's token with an issuer error | `SPIRE_JWT_ISSUER` differs from the issuer the dashboard publishes | see [The dashboard's own identity → Troubleshooting](oidc/dashboard-identity.md#troubleshooting) |
