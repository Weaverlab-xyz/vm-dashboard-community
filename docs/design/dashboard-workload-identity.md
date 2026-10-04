# Design: the dashboard as a SPIFFE workload, and what its own SPIRE server changes

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you are about to give the dashboard an identity of its own, retire one of its stored cloud keys, move a Workload Lab demo off the lab's SPIRE server, or decide whether something else in the codebase should use a SPIFFE token.

**Slices 1 and 3, L1 and L2 are built; the rest is not.** This is an audit, made after the dashboard got
its own SPIRE server (#996–#998), of where that server should be used next. Each section says what it
removes, what it costs, and what is still unknown. Where SPIFFE adds little, the note says so.

## Why now

Until #996 the only SPIRE server in this codebase was the Workload Lab's: a disposable
server on a cloud VM, seeded with bad entries for the Password Safe SPIFFE plugin to
find. Anything that wanted a SPIFFE identity had to stand up that lab first.

The dashboard now runs its own server ([`docker-compose.spire.yml`](../../docker-compose.spire.yml)),
registers its own trust domain (`dashboard_spire.sync_trust_domain`), and mints join tokens
and entries for remote agents (`dashboard_spire.migrate_agent`). That server is real
infrastructure rather than a lab, and two things follow:

- **Some lab demos only depended on the lab for a trust domain.** They can use the
  dashboard's instead and drop a VM, or two.
- **The dashboard can now be a workload in its own trust domain.** That is the
  prerequisite for replacing the long-lived cloud keys it stores with federated,
  short-lived tokens.

## The rule: additive

Same rule as [SPIRE for agents, Dex for people](agent-and-human-identity.md#the-rule-additive).
Nothing below removes a working path:

- **Stored keys keep working.** `aws_access_key_id`, `azure_client_secret`,
  `gcp_service_account_json` and the OCI signing key stay supported. Federation is used
  only when the stored key is blank, the same fall-through the code already has for
  Workload Credentials leases and platform identity.
- **The Workload Lab's own SPIRE server stays.** Its job, a messy trust domain for the
  Password Safe plugin to govern, needs a disposable server; the seeded weak entries must
  never go into the dashboard's real trust domain.
- **Every item is opt-in** and off by default.

## Slice 1: the dashboard gets an identity of its own

Everything else depends on this slice.

**Built.** `dashboard_spire.mint_jwt` and `live_bundle`; `services/dashboard_identity.py`
(token files under an `flock`, status, issuer, public JWKS); `api/spiffe_oidc.py`
(`/spiffe/.well-known/openid-configuration`, `/spiffe/keys`, `/api/spiffe-identity`); the
`_spiffe_token_loop` in `main.py`; `jwt_issuer` and the `spiffe_tokens` volume in the
overlay; `/spiffe/*` on the agent gateway. Settings → Remote Agents. Operator guide:
[The dashboard's own SPIFFE identity](../remote-agents/dashboard-identity.md). Two
differences from the sketch below, both deliberate:

- **Its own loop, every minute,** not a step in the hourly SPIRE refresh loop. Fifteen-minute
  tokens re-minted at half their life cannot wait an hour.
- **The default issuer is the pinned agent audience plus `/spiffe`,** not the public base
  URL. The agent gateway is the part of an install built to be reachable from outside, so
  its Caddyfile publishes `/spiffe/*` and the default works without new ingress.

### One SPIFFE ID, minted through the admin API the app already drives

- **ID:** `spiffe://<td>/dashboard`, shared by `app` and `worker`. They run the same code
  with the same configuration, and a cloud trust policy that has to name two subjects for
  one service is a policy someone will get half right.
- **How it gets SVIDs:** `spire-server jwt mint -spiffeID … -audience … -ttl 15m`,
  through the same `docker exec` path `dashboard_spire._cli` already uses. **Not** a SPIRE
  agent with workload attestation, and the reason is specific to this host: attestation
  proves a workload to a server that does not already trust it. The app already
  *administers* the server. It creates entries, mints join tokens, and holds the Docker
  socket. An agent in front of it would add two containers and a uid and PID-namespace
  arrangement for no assurance at all.
- **The consequence, stated plainly:** whoever compromises the app can mint any SVID in
  the trust domain. That is already true today through the Docker socket. This slice does
  not create that power; it does make it worth writing down, because cloud trust policies
  will now depend on it (see [Threat model](#threat-model-what-changes)).

### Token files: one writer, many readers

Every consumer below reads a JWT from a file: boto3's `AWS_WEB_IDENTITY_TOKEN_FILE`, a GCP
`external_account` credential's `credential_source.file`, Terraform's
`ARM_OIDC_TOKEN_FILE_PATH`, and Workload Credentials' `file` platform. So:

- **One file per audience** on a tmpfs volume, `spiffe_tokens`, mounted read-only into
  `app`, `worker` and any sibling runner container that runs a cloud CLI (Terraform,
  Packer, kubectl).
- **One writer:** the existing SPIRE refresh loop in `main.py`, which already calls
  `dashboard_spire.sync_if_due`. It re-mints each file at half its TTL. A file that is
  stale because the server is down fails the consumer with an expired token, which reads
  as what it is. It never falls back to a stored key that the operator retired on purpose.
- **Audiences:** `sts.amazonaws.com`, `api://AzureADTokenExchange`, the GCP workload
  identity provider's full resource name, `wlc_identity_audience`, and one per on-prem
  cluster (Slice 4).

### An issuer the clouds can fetch

- **`jwt_issuer`** in `examples/spire-server/server.conf`, set to
  `<public base URL>/spiffe`. Today it is unset, so the dashboard's JWT-SVIDs carry no
  `iss` claim, and every federation below requires one that matches the discovery URL
  character for character.
- **The app serves discovery itself:** `<issuer>/.well-known/openid-configuration` and
  `<issuer>/keys`, unauthenticated, through Caddy. The JWKS comes from a live
  `bundle show` cached for five minutes, falling back to the stored bundle. Read live
  because SPIRE publishes the next JWT signing key before it signs with it, so a live
  JWKS is always ahead of rotation; the daily `sync_if_due` copy is not.
- **Not the SPIRE OIDC Discovery Provider container.** The lab runs one, but here it would
  be another container, another certificate and its own hostname, to serve a document
  the app can build from data it already reads.
- **Reachability is a real constraint.** AWS and Azure fetch the issuer from the
  internet and need a publicly trusted certificate. GCP accepts an uploaded JWKS, so a
  private dashboard can federate with GCP, but someone must re-upload it every key
  rotation (`ca_ttl` is 168h). A k3s API server accepts a private CA. The Settings panel
  should say which clouds the configured issuer can reach, not leave it to the first
  failed call.

### Decouple the trust-domain sync from agent attestation

`dashboard_spire.sync_if_due` runs only while `spire_attest_enabled` is on. Slices 1 and
2 need the dashboard's trust domain registered whenever the SPIRE server is configured,
whether or not any remote agent attests. Gate it on the server being configured instead.

## Slice 2: the Workload Lab, revisited

### L1. The agent demo cell can attest to the dashboard's trust domain

**Built.** Choose *This dashboard's own SPIRE server* on the Agent tab's mint form
(`trust_source="dashboard"`). The cell's ID is `spiffe://<td>/demo/agent-cell/<cell id>`
and its node `spiffe://<td>/node/agent-cell-<cell id>`, both per cell, made by
`dashboard_spire.register_workload` (extracted from `migrate_agent`). The mint response
carries the one-use join token and the bundle; the Install dialog's first step becomes
`spire-agent-install.yml` on the worker's host, which now never touches uid 0's account.
**One deviation from the sketch below:** revoking a cell keeps its SPIRE identity, because
the demo is watching authorization end while identity stays. A separate **Remove SPIRE
identity** action, offered only on a revoked dashboard-attested cell, deletes the entry and
evicts the node.

Today `agentcell_service.trust_domain_problem` refuses a cell with no SPIRE lab, and in
practice the cell needs the lab's **k3s link** as well, because the linked k3s node is the
only lab host that runs a SPIRE agent. That is two VMs and a lab build before the demo's
first step.

- Offer the dashboard's trust domain as the cell's attestation target. The worker's host
  gets a SPIRE agent pointed at the dashboard's server on `tcp/8081`, with a join token and
  entry made the way `dashboard_spire.migrate_agent` makes them for a remote agent. One
  VM, any VM the dashboard deployed.
- **Use a different path.** The cell's ID is `spiffe://<td>/agent/mcp-reader`
  (`AGENT_SPIFFE_PATH`). In the dashboard's trust domain, `/agent/` is the remote agents'
  namespace (`/agent/<agent-id>`), so the cell should use `/demo/agent-cell/mcp-reader`
  there. A demo identity sitting in the production agents' namespace is the kind of thing
  the Password Safe plugin's attestation findings exist to catch.
- Keep the lab-backed option for the demo that shows the plugin and the worker together.

### L2. Service accounts on the dashboard's trust domain, with one click

**Built.** `POST /api/users/{id}/oauth-clients/{client}/spire-entry` (admin) for an SVID
client under `spiffe://<td>/workload/`; Users → OAuth clients prefills that path and shows
**Create SPIRE entry** on such clients. Revoking the client deletes the entry and evicts its
node. `service_accounts.create_client` refuses `/dashboard` and `/agent/…` in the
dashboard's own trust domain (`dashboard_spire.RESERVED_PREFIXES`). The trust bundle stays
current once registered, whichever switches are on (`sync_if_due`).

Service accounts can already authenticate at the token endpoint with a JWT-SVID
(`service_accounts.py`, `spiffe_assertion.authenticate`), but only from a registered trust
domain, and for most installs the lab is where that comes from.

- With the sync decoupled (Slice 1), the dashboard's trust domain is always registered.
- Add **Create SPIRE entry** to a service account bound to a SPIFFE ID in that trust
  domain: a join token for the workload's host and an entry selecting the uid the operator
  names, under `spiffe://<td>/workload/<service-account>`. Same mechanics, same one-time
  token display and same audit rule (no token in the audit row) as **Migrate to SPIRE**.
- Result: a pipeline on any host with a SPIRE agent reaches `/mcp` or the API holding
  nothing, and no lab is involved.

### L3. Govern the dashboard's own trust domain

The lab's entries are seeded and fake. The dashboard's trust domain holds real machine
identities: attested remote agents, service-account workloads, the dashboard itself.
Governing those through the Password Safe SPIFFE plugin is the production version of what
the lab rehearses.

- Needs an `admin_ids` identity for the plugin in `server.conf`, and `tcp/8081` reachable
  from the Password Safe resource broker.
- **Blocked on the lab's open question:** whether the gateway populates managed system
  attributes for a plugin action at all ([SPIFFE and SPIRE](../workload-lab/spiffe.md)).
  Answer it in the lab first; this follows from the answer.

### L4. Federation between the lab and the dashboard

The dashboard already trusts the lab's JWT keys in one direction, by JWKS URL
(`spire_lab_service.register_trust_domain`). Real SPIFFE federation (`federates_with` and
a bundle endpoint on both servers) would let a lab workload be authorized by the
dashboard and the reverse. That is a core SPIFFE capability the lab cannot show today, and
it is the honest demonstration for customers who run more than one trust domain.

**Built** (every lab mode). The servers federate: both serve a bundle endpoint on tcp/8082
(`https_spiffe`). **Federate** on a vm or docker lab re-applies the lab's install, opens
8082 on its ACL and host firewall, and creates the lab's relationship
(`spire-federation.yml`), then the dashboard's (`dashboard_spire.federate`). Each is seeded
with the other's current bundle and refreshed once to prove the fetch. Decommissioning
unfederates. The workloads federate too: the lab's k8s workload gets the dashboard in its
`federatesWith`, and agent cells on the dashboard's trust domain get the lab, at mint or
when the lab federates. `spire-federation-proof.yml` proves it on the linked k3s node, as
the workload. A Helm-mode lab serves the
endpoint from the chart (`spire-server.federation`) through a node Service, and declares
its relationship as a `ClusterFederatedTrustDomain`: the chart's controller manager deletes
any relationship no resource names, so the CLI path would be undone. **Not built:** a
dashboard-run proof on a cell host, since the dashboard runs no playbooks there and the
operator runs it by hand.

### L5. Bring the two servers' settings in line

**Built.** The vm and docker plays write `jwt_issuer` from a new var, and the lab service
passes `issuer_url_for(row)`, the URL the OIDC provider publishes and the Helm mode already
used. Without it, those two modes signed tokens with no `iss`, which a Kubernetes JWT
authenticator refuses, so their k3s link most likely accepted nothing. That reading comes
from the Kubernetes spec, not a live lab. `server.conf` carries commented `aws_kms`,
`azure_key_vault` and `gcp_kms` blocks, and
[the identity doc](../remote-agents/dashboard-identity.md#protect-the-spire-servers-ca-key)
says when and how to switch.

- **`jwt_issuer`**: set on the dashboard's server (Slice 1). The lab's server should set
  it too, so the lab's tokens look like what a customer's federated trust domain issues.
  `tests/test_spire_overlay.py` already holds the two configs in step and should hold this.
- **Key manager:** both use `KeyManager "disk"`. On the dashboard server the CA key is now
  production material. Hosted installs should be told to switch to `aws_kms`,
  `azure_key_vault` or `gcp_kms`, which the config's own comment already suggests and no
  doc yet tells an operator to do.

## Slice 3: the dashboard's cloud credentials, federated

**Built** (`services/cloud_federation.py`). The order is Workload Credentials lease →
stored key → this identity → ambient, so a stored key still wins; `cloud_federation.reason`
names what is missing and Settings shows each cloud's live source. AWS assumes a role with
`AssumeRoleWithWebIdentity` (unsigned, cached to five minutes before expiry); Azure has one
constructor, `azure_credential`, whose assertion re-reads the token file per request, and
Terraform gets `ARM_USE_OIDC`; GCP gets an `external_account` config, written beside the
token for subprocesses. **A correction to the table below:** Terraform and Packer run as
local subprocesses of the app and worker, not as sibling containers, so nothing has to be
passed into another container — they read the files directly. **Not covered:** the Packer
azure-arm build (credential through the template, unverified) and OCI.

The largest credential this removes. On PaaS hosting, Workload Credentials and the
platform's managed identity already avoid stored keys. A self-hosted Docker install has no
platform identity, so today it stores a key per cloud. With Slice 1 it has an identity
every major cloud can federate with.

| Cloud | Mechanism | Trust condition | Code |
|---|---|---|---|
| **AWS** | `AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE` → `sts:AssumeRoleWithWebIdentity` | role trust: `<issuer>:sub` = `spiffe://<td>/dashboard`, `<issuer>:aud` = `sts.amazonaws.com` | **None.** `aws_service._aws_kwargs` passes no credentials when none are stored, so boto3's default chain picks the variables up. `terraform_provider_env.aws_env` returns `None` the same way. (Its docstring says it never falls back; the code does. Fix the docstring.) |
| **GCP** | `GOOGLE_APPLICATION_CREDENTIALS` → an `external_account` config whose `credential_source.file` is the token file | provider attribute condition on `assertion.sub` | **Mostly none.** `gcp_service._gcp_creds` and `secrets_backend_service._gcp_client` fall back to ADC. **`packer_service` does not:** its GCS upload reads `gcp_service_account_json` unconditionally. It also derives the project from the key's `project_id`, so federation needs `gcp_project_id` set explicitly. |
| **Azure** | a federated credential on the app registration → `ClientAssertionCredential` reading the token file; Terraform `ARM_USE_OIDC=true` + `ARM_OIDC_TOKEN_FILE_PATH` | issuer, subject `spiffe://<td>/dashboard`, audience `api://AzureADTokenExchange` | **Yes, small.** Every `ClientSecretCredential(` site (`azure_service`, `storage_service`, `secrets_backend_service`, `packer_service`) needs the assertion branch, and `terraform_provider_env.azure_env` the OIDC variables |
| **Workload Credentials** | `wlc_identity_platform=file`, `wlc_identity_token_file` → the token file | a Custom IDP registration for the dashboard's issuer | **None.** The `file` platform exists |
| **OCI** | — | — | No clean fit. OCI's token exchange is tied to identity domains and is not worth the code here. Stays on its signing key or Password Safe |

Three things apply across the table:

- **Pin the subject, never the trust domain.** Remote agents, service-account workloads
  and lab-adjacent demos share the trust domain. A cloud trust policy that accepts any
  `spiffe://<td>/…` gives every one of them the dashboard's cloud access.
- **Sibling containers need the file too.** Terraform, Packer and kubectl run in sibling
  containers launched over the Docker socket. The token volume and the variables have to
  be passed into each one, in the same place `terraform_provider_env.provider_env`
  passes credentials today.
- **Say which source is live.** `azure_service` already logs the credential source it
  used. All four clouds should show it in Settings, so an operator who clears a stored
  key can see that federation took over rather than inferring it from a working page.

## Slice 4: on-prem k3s without the dashboard's admin kubeconfig

For `cloud="local"` clusters the dashboard keeps the admin kubeconfig from
`k3s-kubeconfig.yml`, a client certificate and key. #995 stopped handing it to *people*
(they go through Dex). The dashboard itself still uses it.

**Built**, with three changes from the sketch below. A play of its own,
`k3s-dashboard-auth.yml` (prefix `dashboard:`, binding `dashboard:<spiffe id>` →
`cluster-admin`), instead of reusing the lab's `spiffe:` entry, so the lab's authenticator
and the dashboard's never share one. The audience is per cluster, `<issuer>/k8s/<cluster id>`
(`k8s_service.dashboard_audience`), so one cluster cannot replay the dashboard's token at
another. And the switch is `k8s_service.resolve_kubeconfig`, the one function every routine
caller already goes through: for a local cluster marked `k8s_spiffe_trusted_<cid>` it keeps
the stored cluster and replaces the user with a thirty-minute SVID, cached until ten minutes
remain. A mint that fails is a `K8sError` naming the break-glass (untick the flag), never the
admin certificate. `stored_kubeconfig` keeps the raw file for the API-tunnel download.

- `examples/playbooks/k3s/k3s-spiffe-auth.yml` already maintains a shared
  `AuthenticationConfiguration` with one JWT authenticator per issuer, each replaced in
  place by its username prefix. Point one at the dashboard's issuer, and bind RBAC to
  `spiffe:spiffe://<td>/dashboard`.
- `k8s_service._runner_kubeconfig` and the in-app client mint a fresh SVID for that
  cluster's audience, the way they mint EKS, AKS and GKE tokens today.
- **The admin kubeconfig stays** as break-glass, exactly as
  [the identity note](agent-and-human-identity.md#on-prem-the-one-path-that-changes)
  already says. It is no longer the credential every routine call uses.
- The play's own constraint applies: the issuer must be a hostname, not an IP. The
  dashboard's public base URL is one.

## Slice 5: two smaller items

**Ephemeral cloud secrets for the ECS and Cloud Run runners.** Today a Password Safe
credential the dashboard checked out is briefly *copied* into AWS or GCP Secrets Manager
for the runner task to read (`ephemeral_secrets`, `ephemeral_gc`). A single-use,
audience-bound token passed to the task, exchanged for a credential sealed to the task
(the `dashboard_secret` pattern), keeps the credential out of a second store. **Be honest
about SPIFFE's part:** a Fargate or Cloud Run task cannot run a SPIRE agent, so that token
would be *minted*, not attested, which is the downgrade
[SPIFFE and SPIRE](../workload-lab/spiffe.md#minting-is-a-deliberate-downgrade-and-the-honest-version-matters)
warns about. It still beats copying the real credential, but the win comes from
seal-and-exchange, not from SPIFFE. It also needs the runner to reach the dashboard, which
the current design does not assume.

**Built**, opt-in (`ansible_runner_credential_callback`), and stronger than the token
sketched above. The token is single-use and bound to the run, but it is not the
authenticator. The task also proves its **platform identity**, bound to the token's hash:
ECS through a presigned `sts:GetCallerIdentity` signed with its task role, Cloud Run
through a Google-signed ID token for its service account. The answer is sealed in
`agent_sealing`'s format to a key the task made (`services/runner_credential`,
`services/runner_fetch`). The grant waits in the database rather than in memory, because
the worker issues it and the app redeems it. It is encrypted and lives at most ten
minutes. The Secrets Manager copy stays as the fallback setting.

**Agent file-share credentials.** `shares.yaml` SMB passwords stay on the agent host and
have no `dashboard_secret` option, unlike `connections.yaml`. For a SPIRE-attested agent
the same per-job sealed fetch, under the same **Release dashboard-held credentials only to
SPIRE-attested agents** setting, completes
[central storage with SPIRE](../remote-agents/credentials.md#central-storage-with-spire-the-recommended-model).

**Built** (agent 2.7.0). `dashboard_secret: true` on a `shares.yaml` entry, and one
dashboard-held password, `storage_agent_password`, for the one share the storage backend is
configured with. The existing `POST /api/agent/jobs/{id}/secret` serves `agent_storage`
jobs too: the share is derived from the job row and must be the configured share on the
configured agent, the SPIRE-only setting applies, and each release is audited as
`agent.share_secret`. The storage preflight refuses an older agent while a password is
held. Username, domain and path stay in `shares.yaml`.

## Considered and left alone

| What | Why not |
|---|---|
| Password Safe, PRA, Entitle and the Workload Credentials management API | They authenticate with OAuth client secrets and accept no JWT assertion. Keep those secrets in Password Safe, which is already supported. |
| Hypervisor endpoints (vSphere, Proxmox, Nutanix, XCP-ng, Hyper-V) | They take a username and password. Central storage for attested agents (#998) is the answer there. |
| `app` ↔ `worker` ↔ Postgres | One compose network on one host. mTLS from X.509-SVIDs adds rotation and failure modes for no boundary that does not already exist. |
| Remote agent ↔ dashboard | Done in #996–#998. |
| Managed-cluster access for the dashboard (EKS, AKS, GKE, OKE) | Already short-lived, minted server-side from the cloud's own identity (`_runner_kubeconfig`). Slice 3 moves the credential *behind* those mints; the cluster side needs nothing. |

## Threat model: what changes

- **App compromise.** An attacker in the app can mint `spiffe://<td>/dashboard` tokens
  and use them against every federated cloud while they are present. Today the same
  attacker reads the stored keys and keeps them. Federation turns a durable, offline
  credential into one that needs continued presence and leaves a mint on the SPIRE
  server's log for every token. That is better, but it is not "the app holds nothing".
- **Trust-domain scope.** Pinning the cloud trust policies to the exact subject is what
  stops an attested agent from becoming a cloud admin. A wildcard trust policy is the
  likeliest mistake, and Settings should check the configured trust policy where a cloud
  API allows it.
- **No revocation.** A minted JWT lives until it expires. The 15-minute TTL is the
  containment window; stopping the refresher is the kill switch.
- **The discovery endpoint is public.** It serves only public keys. That is by
  construction, and worth saying so nobody puts it behind SSO and breaks every
  federation at once.

## Order

1. ~~**Slice 1:** `jwt_issuer`, minted token files with one writer, in-app discovery, and
   the sync decoupled from agent attestation.~~ Built.
2. ~~**L1 and L2:** the agent cell and service accounts on the dashboard's trust domain.~~
   Built.
3. ~~**Slice 3:** AWS and GCP (configuration and docs, plus the `packer_service` fix), then
   Azure (the `ClientAssertionCredential` branch).~~ Built.
4. ~~**Slice 4:** on-prem k3s through the dashboard's SVID.~~ Built.
5. **L3:** after the lab answers the Password Safe attribute question. ~~L4 and L5~~ built.
6. ~~**Slice 5:** agent file-share credentials, then the ECS and Cloud Run runners.~~ Built.

## Not verified

- No cloud has yet accepted a SPIRE-minted JWT-SVID from this dashboard. The table's
  trust conditions follow each cloud's federation documentation, not a run.
- `jwt mint` output shape: read from the 1.15.3 source (`-output json` prints the
  `MintJWTSVIDResponse`, token at `svid.token`; `-ttl` is a Go duration) and pinned in
  `tests/test_dashboard_spiffe_identity.py`'s fake, but not run against a real server.
- No k3s API server has yet accepted a dashboard SVID: `k3s-dashboard-auth.yml` shares its
  tested structure with `k3s-dex-auth.yml`, but has not been run.
- Whether SPIRE logs a `jwt mint` with enough detail to serve as the audit trail the
  threat model leans on. If not, the dashboard should write its own audit row per mint.
