# Design: SPIRE for agents, Dex for people, and nothing existing removed

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you are about to give the remote agent a SPIFFE identity, put Dex in front of the dashboard's SSO or a cluster's API server, or you are wondering why the Ed25519 agent path was not retired when SPIRE arrived.

**Mostly not built.** What exists:
- the two compose overlays: [`docker-compose.spire.yml`](../../docker-compose.spire.yml) for the dashboard and [`examples/remote-agent/docker-compose.spire.yml`](../../examples/remote-agent/docker-compose.spire.yml) for the agent,
- the Dex lab plays: [`examples/playbooks/dex/`](../../examples/playbooks/dex/README.md) and `examples/playbooks/k3s/k3s-dex-auth.yml`,
- the shared AuthenticationConfiguration handling that lets the SPIFFE and Dex authenticators sit on one k3s API server.

Everything under [Phase 2](#phase-2-what-is-specified-here-and-not-built) is design only. None of it has run against a live host.

## The problem

There are two problems, and they are different enough that one tool should not solve both.

1. **The remote agent keeps a long-lived private key on disk.**
   - `runners/agent/agent.py` generates an Ed25519 key at enrolment and writes it, unencrypted, to `/var/lib/dashboard-agent/identity.json`.
   - [`docs/remote-agents/agent-host.md`](../remote-agents/agent-host.md) is honest about this: whoever can read the volume *is* the agent until someone re-issues it.
2. **People reach Kubernetes six different ways.**
   - EKS, AKS, GKE and OKE each have their own token exec plugin and their own user model.
   - k3s and other on-prem clusters have whatever the operator set up.
   - The dashboard's own SSO is a separate OIDC client again.
   - There is no single place where "this person, in this group" means the same thing everywhere.

**The split**
- **SPIRE is for workloads.** It *attests* a machine or process and issues short-lived SVIDs. The agent is a workload.
- **Dex is for people.** It *federates* upstream human IdPs (Entra, Okta, LDAP, GitHub) into one OIDC issuer that every cluster and the dashboard can trust.

Dex was looked at for the agent first and rejected for it. See [Rejected](#rejected).

## The rule: additive

Nothing here removes or changes a path that works today, with **one deliberate exception**: how people get a kubeconfig for an on-prem cluster (see [On-prem: the one path that changes](#on-prem-the-one-path-that-changes)).

| Today | Stays | New, opt-in |
|---|---|---|
| Agent enrols with `agte_…` and keeps Ed25519 in `identity.json` | **Yes, permanently**, and still the default | Agent attests through a SPIRE sidecar |
| SSO points straight at an IdP (`oidc_*` settings) | **Yes**, and still the default | SSO points at Dex, which points at the IdP |
| Cluster access per cloud (EKS/GKE/OKE exec plugins, AKS with Entra) | **Yes**, and still the default | A managed cluster's API server also trusts Dex, per cluster, opt-in |
| On-prem clusters (k3s, `cloud="local"`): no standard way for people, only the admin kubeconfig | **The admin kubeconfig stays**, as the dashboard's own credential and break-glass; it is never handed to people | Dex, as **the only** way people reach an on-prem cluster |

Three reasons the legacy agent path is permanent rather than deprecated:
- **The proxy constraint below.** Some sites can never reach a SPIRE server.
- **Agents already in the field must keep working** after a dashboard upgrade, with no action from whoever runs them.
- **Rollback.** If a SPIRE migration goes wrong, the fix is to re-issue an `agte_` code, which is the procedure operators already know.

The dashboard *recommends* SPIRE with a banner. It never forces it. See [The banner](#the-banner).

---

## Part 1: the agent attests through SPIRE

### Two facts that bound what this buys

**1. "No private key on disk" depends on the node attestor.** It is not a given.

The SPIRE agent has to prove which node it is on before it gets an SVID. How it does that decides whether a key is stored anywhere:

| Where the agent runs | Node attestor | Agent KeyManager | Private key at rest? | Restart behavior |
|---|---|---|---|---|
| Bare Docker host, no platform identity | `join_token` | `disk` | **Yes.** The SPIRE agent's own SVID key, rotated hourly and revocable with `spire-server agent ban` | Resumes from disk; the token is already spent |
| AWS EC2 | `aws_iid` | `memory` | **No** | Re-attests from the instance identity document |
| Azure VM | `azure_imds` (`azure_msi` on older SPIRE) | `memory` | **No** | Re-attests from IMDS |
| GCP VM | `gcp_iit` | `memory` | **No** | Re-attests from the instance identity token |
| Pod on a cluster the SPIRE server can reach | `k8s_psat` | `memory` | **No** | Re-attests from a projected SA token |
| Host with a TPM | `tpm_devid` (or a TPM plugin) | `memory` | **No**; the key is in the TPM | Re-attests from the TPM |

- With `join_token` the SPIRE agent still stores a key. That is better than today: the key lasts an hour instead of indefinitely, rotates, and can be revoked centrally. But it is not zero, and the docs must not say it is.
- `join_token` + `memory` is not an option. The token is single-use, so the first restart would leave the agent unable to attest.
- `k8s_psat` needs the SPIRE server to call that cluster's TokenReview API. That works for a cluster the dashboard already manages. It does not work for an on-prem cluster behind a firewall, which is exactly where remote agents live. For those, the agent is a `join_token` host, or a TPM host.

**2. SPIRE agent ↔ server traffic is mTLS gRPC. TLS-inspecting proxies break it.**

`agent_signing.py` explains why the agent signs requests instead of using mTLS: an inspecting proxy terminates TLS and cannot forward a client certificate. The SPIRE agent's connection to the server is mTLS gRPC on port 8081, so it hits the same wall.

- A site behind an inspecting proxy needs a **bypass rule** for the dashboard's SPIRE host and port.
- Without one, the agent cannot attest, and the site stays on Ed25519.
- That is the main reason the legacy path is permanent rather than a migration with an end date.

**`sealing.key` is a separate key and is out of scope.**
- It is the agent's host-local AES key for `password_sealed:` / `client_secret_sealed:` values (`agent.py:438`).
- It is not an authentication credential, and SPIRE does not replace it.
- It stays in the `agent_state` volume in both modes. The agent overlay keeps that volume for this reason.

### The flow

```
spire-agent (sidecar)                dashboard-agent (uid 10001)               dashboard
──────────────────────               ───────────────────────────               ─────────
node attestation ──── mTLS gRPC :8081 ───────────────────────────────────────▶ spire-server
                     ◀── agent SVID (memory or disk) ──

                     ◀── Workload API (unix socket, attested by uid 10001) ──
                         FetchJWTSVID(aud = <dashboard>/api/agent/attest)
                                     generate Ed25519 keypair IN MEMORY
                                     POST /api/agent/attest ──── HTTPS ───────▶ verify SVID
                                       body: {svid, public_key}                  (spiffe_assertion:
                                       signed with the NEW key                    sig, aud, lifetime
                                                                                  cap, single use)
                                                                                 map SPIFFE ID →
                                                                                   RemoteAgent row
                                                                                 bind public_key
                                     ◀── {agent_id, dashboard_public_key, audience}
                                     every later request: the EXISTING signed path
                                       (X-Agent-Id / -Timestamp / -Nonce / -Signature)
```

- **One token crosses the proxy per agent start.** It is audience-bound to the attest route and lives at most `default_jwt_svid_ttl` (5 minutes in the lab config). `spiffe_assertion` records its hash, so it is single-use: a copy lifted from a proxy log after the agent has used it is refused.
- **Signing after that is unchanged.** `signed_agent`, `agent_service.authenticate` and the nonce table are not modified.
  - Request signing is not what puts a key on disk. Persisting the key across restarts is. Keeping signing means the proxy-safety argument in `agent_signing.py` still holds without change.
- **Signing in the other direction is unchanged too.** Job envelopes are still signed with the dashboard's own key, and the agent learns that public key from the attest response, as it does at enrolment today.
- **On restart the agent attests again.** It writes nothing to `identity.json`.
- **Workload attestor: `unix`, selector `uid:10001`.** The two containers share a PID namespace (`pid: "service:spire-agent"`), so the SPIRE agent can see the caller's uid.
  - The alternative, the `docker` workload attestor, needs `/var/run/docker.sock` mounted into the sidecar. That is root on the host, and the agent's whole hardening story (`docs/remote-agents/agent-host.md`) is that nothing on this host has it.

### How each side picks a mode

**Agent side**
- `SPIFFE_ENDPOINT_SOCKET` set and the socket present → attest through SPIRE.
- Otherwise → today's behavior exactly: use `identity.json` if present, else redeem `AGENT_ENROLLMENT_CODE`.
- A new agent image without the sidecar is indistinguishable from the old one.

**Server side**
- `RemoteAgent` gets two new columns:
  - `auth_mode` (`ed25519` | `spiffe`, default `ed25519`),
  - nullable `spiffe_id` (unique).
- The Alembic migration needs no backfill: every existing row is `ed25519` by default, which is what it already is.
- `/api/agent/attest` accepts only rows whose `spiffe_id` matches the SVID's `sub`. An `ed25519` row cannot be taken over by someone who happens to hold an SVID.

### Migrating one agent

1. An admin clicks **Migrate to SPIRE** on the agent's row. This needs the SPIRE server enabled (`spire_server_enabled`).
2. The dashboard creates the SPIRE entries through the server's local admin socket:
   - a node entry for the chosen attestor (or a join token),
   - a workload entry `spiffe://<td>/agent/<agent-id>` with parent = that node and selector `unix:uid:10001`.

   It then sets `spiffe_id` on the row. `auth_mode` stays `ed25519` for now.
3. The operator adds the sidecar overlay on the agent host and restarts.
4. On the first successful attest, the server sets `auth_mode = spiffe` and nulls the old `public_key` in the same transaction.
   - This mirrors what re-issue does today. The legacy key stops working only once the new path has proven itself.
   - Agent `id`, job history and policy hash carry over.
5. **Rollback:** re-issue an `agte_` code. Re-issue already clears the key. It now also clears `spiffe_id` and sets `auth_mode = ed25519`, and deletes the SPIRE entries.

### The banner

- **Shown** on the Agents list and on the agent detail view, for each agent with `auth_mode = ed25519`, **only when `spire_server_enabled` is on.** A dashboard that has not opted in to SPIRE never shows it.
- **Text depends on `agent_version`**, which every poll already reports and which `api/agent.py` already gates on through `_major_version`:
  - below the first SPIRE-capable major: *"Update the agent image, then migrate it to SPIRE to stop storing a long-lived key."*
  - otherwise: *"This agent can move to SPIRE."*, with the Migrate button.
- **Dismissible per agent.** "Not now" is a legitimate answer for a site behind an inspecting proxy, and a banner that cannot be dismissed trains people to ignore banners.

### The dashboard-hosted SPIRE server

- **Packaged as an opt-in compose overlay**, [`docker-compose.spire.yml`](../../docker-compose.spire.yml). It reuses the lab's hardening and image-digest pins from `examples/playbooks/spire/spire-docker-server.yml`, and `tests/test_spire_overlay.py` keeps the two from drifting.
  - It stays on docker-compose, as the roadmap requires (`docs/saas-roadmap.md`, "Feasibility flag").
- **Port 8081 is published directly, not through Caddy.**
  - Caddy terminates TLS, and SPIRE's agent-to-server channel is mTLS that has to reach the server untouched.
  - Passing it through would need Caddy's layer-4 plugin, a custom build for one port.
- **The admin API stays private.** It is a unix socket on a named volume that only the dashboard containers mount. Nothing about administration listens on the network.
- **CA key storage**
  - `disk` KeyManager in community.
  - The hosted topology should use `aws_kms` / `azure_key_vault` / `gcp_kms`, which is the same move the roadmap already made for the JWT root key.
- **Gated by `spire_server_enabled`.** It is off by default. While it is off:
  - no attest route answers,
  - no banner shows,
  - no Migrate button exists.

---

## Part 2: Dex for people

### What changes, and what doesn't

- **Dashboard SSO: Settings → Identity → SSO provider**
  - `direct` (default) is today's behavior.
  - `dex` fills `oidc_issuer` / `oidc_client_id` / `oidc_client_secret` from the Dex settings. The **same** `oidc_service` code path runs either way.
  - Dex is a different issuer URL, not a different integration.
- **Clusters: how people authenticate depends on where the cluster runs.**

  | Cluster | Human authentication | Default | Can it be changed? |
  |---|---|---|---|
  | EKS, GKE, OKE, AKS (managed) | `native` (the cloud's own: EKS/GKE/OKE exec plugins, AKS with Entra) or `dex` | **`native`** | Yes, per cluster, opt-in: **Settings → Kubernetes → Human authentication** (`k8s_human_auth_mode`), overridable on the cluster |
  | k3s / on-prem (`cloud="local"`) | `dex` | **`dex`** | No. There is no native option to fall back to |

  - **Managed: Dex is optional, never the default, on every cloud.** Not only on AKS, where the cluster side is still preview. One default for all four is easier to reason about than a per-cloud exception that changes when a preview ends. Flip a cluster to `dex` only once its API server trusts Dex (the Terraform wiring below).
  - **On-prem: Dex is the only way.** A self-built cluster has no cloud identity to be "native" to; the alternative is handing people copies of the admin client certificate, which cannot be revoked per person and leaves no per-person audit trail. So kubeconfigs the dashboard generates for people on a `cloud="local"` cluster always use the `oidc-login` exec plugin against Dex, and the setting is not shown for those clusters.
  - **The on-prem admin kubeconfig is not removed.** It is how the dashboard registers and operates the cluster, and it is the break-glass path if Dex is down. It stays where it is stored today (the encrypted config store or an external vault) and is never offered to a person.
  - **Consequence for building an on-prem cluster:** `k3s-dex-auth.yml` becomes a required step after `k3s-server-init.yml`, not an extra. Until a cluster's API server trusts Dex, the dashboard has no way to give people access to it, and says so on the cluster's row rather than falling back to the admin kubeconfig.
  - `dex` generates kubeconfigs that use an `oidc-login` exec plugin against Dex; `native` is unchanged from today.

### On-prem: the one path that changes

Today a registered `cloud="local"` cluster shows the **API tunnel** button, and its download (`GET /api/k8s/clusters/{id}/api-tunnel-kubeconfig` → `k8s_service.build_api_tunnel_kubeconfig`) returns the **stored** kubeconfig, repointed at the tunnel.
- For EKS, GKE and AKS that file is token-free: its `users` entry is the cloud's exec plugin, so each person authenticates as themselves.
- For k3s the stored kubeconfig is the **admin** one from `k3s-kubeconfig.yml`, with the client certificate and key inline. So the same download hands cluster-admin credentials to anyone with `k8s:read`. That credential cannot be revoked per person, and the audit trail cannot tell people apart.

"Dex is the only way on-prem" closes that. In Phase 2, for `cloud="local"`:
- the route returns a kubeconfig whose `users` entry is the Dex `oidc-login` exec block (cluster CA kept, no client certificate, no key);
- if the cluster's API server does not trust Dex yet, the route refuses with a message naming `k3s-dex-auth.yml`. It never falls back to the stored file.

The stored admin kubeconfig keeps doing what it does for the dashboard itself (registration, the transient runners, break-glass). It just stops being downloadable by people. This is the only behavior in this note that is removed rather than added, and it is removed because it hands out a shared admin credential.
- **Not affected:** the transient-runner kubeconfig (`k8s_service._runner_kubeconfig`). It swaps in a server-minted token for the dashboard's own runs and has nothing to do with people.
- **Groups:** Dex passes the upstream IdP's groups through in the `groups` claim. The cluster maps them to RBAC with a prefix (`dex:` in the lab play), so a Dex group can never collide with a built-in `system:` group.

### Every cluster type can trust Dex

Standardizing on Dex is therefore always *possible*: mandatory on-prem, available on every managed cloud for an administrator who wants one identity everywhere.

| Cluster | Mechanism | Where it gets wired | Caveat |
|---|---|---|---|
| k3s / on-prem | `--authentication-config` JWT authenticator (structured authentication, GA in 1.34) | `examples/playbooks/k3s/k3s-dex-auth.yml` | none |
| EKS | OIDC identity provider config | `aws_eks_identity_provider_config` in `terraform/k8s_cluster/aws_eks` | one external OIDC provider per cluster |
| GKE | Identity Service for GKE | `identity_service_config` and its `ClientConfig` in `terraform/k8s_cluster/gcp_gke` | Identity Service must be enabled on the cluster |
| OKE | Cluster OIDC token authentication | `open_id_connect_token_authentication_config` in `terraform/k8s_cluster/oci_oke` | check the pinned OCI provider supports it |
| AKS | Structured authentication, **preview** | an `azapi` resource in `terraform/k8s_cluster/azure_aks` (azurerm likely lacks it) | preview, Kubernetes ≥ 1.30 |

On every managed row this is **opt-in**: the cluster stays on `native` unless an administrator switches it. On the k3s row it is required (see above).

- Each Terraform change takes a `dex_issuer_url` variable. **Empty means no change**, so every existing cluster plans with an empty diff.
- **This corrects an earlier claim.** The header of `k3s-spiffe-auth.yml` said managed clusters cannot be pointed at an external issuer. That is true for that play's *workload* case: arbitrary `--authentication-config`, an issuer on a private address, a SPIFFE CA. It is not true for a public OIDC issuer used by people.

### Dex must be public

Managed control planes fetch the issuer's discovery document and JWKS **from the internet**.

- A Dex instance that only resolves inside a lab can serve k3s and nothing else.
- To serve EKS, GKE, OKE or AKS, Dex needs a public DNS name and a certificate from a public CA.
- The k3s play also accepts a private CA, through `dex_ca_pem`, for the lab case.

### Dex and SPIRE on one k3s API server

k3s takes one `--authentication-config` file. That file holds a list of JWT authenticators, so both can live there, as long as neither play overwrites the other's entry. The rules:

- **Each play owns its entries by username prefix.**
  - `k3s-spiffe-auth.yml` owns entries whose `claimMappings.username.prefix` is its `username_prefix` (`spiffe:`).
  - `k3s-dex-auth.yml` owns `dex:`.
- **Each play reads the current file and replaces only its own entry, in place** (appending it the first time). Re-running either play never removes or reorders the other's entry. In place matters: if each play moved its entry to the end, running them alternately would rewrite the file, and restart k3s, every time.
- The prefix is the ownership key, not the issuer URL. Kubernetes rejects unknown fields in this file, so there is nowhere to put an ownership marker.
- Keying by prefix also means a play re-run with a *new* issuer replaces its old entry instead of leaving a stale second one.
- **Both plays point the API server at the file through the same drop-in.** The file and drop-in names keep their historical `spiffe-` spelling, so an existing SPIFFE-linked node is upgraded in place and never ends up with two `authentication-config` flags.
- **Teardown.** `k3s-spiffe-unlink.yml` and `dex-remove.yml` each remove only their own entry. The drop-in goes only when the list is empty, and k3s restarts only when the file actually changed.
- **Bug fixed along the way.** The unlink play used to target paths the link play never wrote (`90-spiffe-auth.yaml`, `/etc/rancher/k3s/spiffe-auth.yaml`), so unlinking left the flag in place.

---

## Rejected

- **Dex for the agent.**
  - Dex has no way to attest a machine. An agent would authenticate to Dex with `client_credentials`, swapping an Ed25519 key for a client secret, which is still a secret on disk.
  - The resulting token is a bearer token on every poll, which is exactly what `agent_signing.py` refuses to put through an inspecting proxy.
  - Dex's RFC 8693 token exchange only *moves* the problem to whatever upstream token the agent presents. SPIRE is that upstream, minus the extra hop.
- **X509-SVID mTLS from the agent to the dashboard.** Same proxy problem as any mTLS. The SVID is used as a JWT to bind a key, never as a client certificate on the dashboard connection.
- **The `docker` workload attestor.** It needs the Docker socket, which is root on the host. A shared PID namespace plus `unix:uid` gets the same answer without it.
- **Forced migration with a deadline.** Agents behind inspecting proxies could never meet it. A deadline would turn a security improvement into an outage for exactly the customers the agent exists for.
- **Running SPIRE behind Caddy.** See [The dashboard-hosted SPIRE server](#the-dashboard-hosted-spire-server).

## Phase 2: what is specified here and not built

**Built since:** the on-prem Dex kubeconfig. For `cloud="local"` clusters the API-tunnel download is a `kubectl oidc-login` kubeconfig against Dex (`k8s_service._onprem_dex_tunnel_kubeconfig`), gated on the Dex settings (`dex_issuer_url`, `dex_k8s_client_id`, `dex_ca_pem`) and the per-cluster **Trusts Dex** flag (`POST /api/k8s/clusters/{id}/dex-trust`). Without either it refuses with the missing step. The stored admin kubeconfig is never returned. See `docs/kubernetes.md`.

Still to build:

- `POST /api/agent/attest`, reusing `services/spiffe_assertion.py` for verification and `agent_service` for binding.
- In `runners/agent/agent.py`: socket detection, the Workload API fetch (via the `spiffe` Python package or the gRPC stubs), and the in-memory key.
- Alembic migration: `RemoteAgent.auth_mode`, `RemoteAgent.spiffe_id`.
- The **Migrate to SPIRE** action, entry creation through the admin socket, the re-issue changes, and the banner.
- Settings: `spire_server_enabled`, SSO provider `direct | dex`, `k8s_human_auth_mode` (managed clusters only, default `native`) with a per-cluster override.
- `dex_issuer_url` wiring in the four managed-cluster Terraform modules, empty by default.
- A Dex compose overlay for the dashboard host, for sites that want Dex beside the dashboard rather than on a cluster.
