# Workload identity: the recommended credential model

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are choosing how the dashboard, its remote agents or its integrations authenticate, or you still store a static key, token or signing key and want to know what replaces it.

Part of [Identity and access](../admin.md).

**Recommended: give each workload an identity, attach privilege to the identity, and grant
write privilege just in time.** A static secret is still fully supported: it is the quick
start and the fallback where an identity is not possible. It is no longer the recommended
end state, for the dashboard or for anything it runs.

The reasoning is short. Whoever can read a stored secret *is* the workload, for as long as
the secret lives, from wherever they copy it. An identity is proved afresh at every start
and every token expires in minutes, so there is nothing at rest to steal. But an identity
that holds broad permissions all the time still has **standing privilege**: compromise the
workload and you have everything it may do. So the target pairs the identity with a
credential or grant that exists only while a job needs it.

## The three stages

| Stage | What authenticates | What is stored | Standing privilege | Status |
|---|---|---|---|---|
| **1. Stored secret** | an access key, client secret, service-account key, API token or an agent's Ed25519 key | the secret, encrypted in the database or referenced from a vault | everything the secret may do, always | supported, the quick start; **not recommended** |
| **2. Workload identity** | a short-lived token proving *which workload* this is: a SPIFFE JWT-SVID from the dashboard's own SPIRE server, or a platform identity (an Azure managed identity) | nothing | everything the identity may do, always | **recommended** |
| **3. Workload identity + just-in-time privilege** | the same identity, holding **read** permissions only | nothing | none for writes: a dynamic credential or a JIT grant exists only for the job that needs it | **the target** |

Stage 3 is what removes standing privilege. The identity does everyday reads (inventory,
cost, status). A write gets its privilege one of three ways:

- **a Workload Credentials dynamic lease** (AWS and Azure), minted inside the job and
  expiring after it: see [Dynamic AWS and Azure credentials](../preview/workload-lab/dynamic-credentials.md);
- **an Entitle just-in-time grant** to the identity (GCP, and any resource Entitle governs):
  see [Entitle machine identity](../beyondtrust/entitle.md#machine-identity--jit-cloud-credentials-via-entitle);
- **a per-job Password Safe checkout**, released only to an attested agent: see
  [Central storage with SPIRE](../admin/remote-agents/credentials.md#central-storage-with-spire-the-recommended-model).

You can stop at stage 2. It already removes every secret at rest, and it is one setting
away from stage 3 on AWS and Azure.

## Where each surface stands

"Proven live" means it has run end to end against a real host or cloud. "Built" means it is
covered by tests and documented, but no live install has used it yet. Treat the first
install of a "built" path as the test.

### Remote agents

| Today | Recommended | Target | Status |
|---|---|---|---|
| Ed25519 key in the agent's state volume | [attest through SPIRE](../admin/remote-agents/spire-attestation.md): the key lives only in memory | SPIRE, plus credentials kept by the dashboard and released per job (`dashboard_secret: true`), with **Release dashboard-held credentials only to SPIRE-attested agents** ticked | proven live (2026-10-13) |

The Agents page already shows a **Migrate to SPIRE** banner for every agent still on a key
on disk. POV control-node agents attest the same way, falling back to their enrolment
code only where the guest cannot reach the server: see
[SPIRE on the control node](../profiles/pov/skytap.md#spire-on-the-control-node) (proven live, 2026-10-14).

### The dashboard's cloud credentials

The dashboard's own SPIFFE identity replaces the stored key for its AWS, Azure and GCP
calls, Terraform and Packer included. Setup is
[The dashboard's own identity](oidc/dashboard-identity.md).

#### Dashboard to AWS

| Today | Recommended | Target | Status |
|---|---|---|---|
| IAM user access key | the SPIFFE identity assumes a role with `AssumeRoleWithWebIdentity` | that role holds read only, and jobs take a [Workload Credentials](../preview/workload-lab/workload-credentials.md) lease (`wlc_aws_everyday_source = federation`) | proven live (2026-10-15) |

#### Dashboard to Azure

| Today | Recommended | Target | Status |
|---|---|---|---|
| service principal client secret | the SPIFFE identity as a federated credential on the same app registration | that app holds read only, and jobs take a Workload Credentials lease (`wlc_azure_everyday_source = federation`) | built |

#### Dashboard to GCP

| Today | Recommended | Target | Status |
|---|---|---|---|
| service-account JSON key | the SPIFFE identity through a workload identity pool, optionally impersonating a service account | that account holds read only, and each write runs inside an Entitle JIT grant (Workload Credentials does not mint GCP credentials) | built |

On a POV instance, the **POV cloud provider** panel offers all three stages per cloud as
radio buttons: see [Keyless AWS](../profiles/pov/public-cloud.md#keyless-aws-the-dashboards-spiffe-identity),
[Keyless Azure](../profiles/pov/public-cloud.md#keyless-azure-the-dashboards-spiffe-identity) and
[Keyless GCP](../profiles/pov/public-cloud.md#keyless-gcp-the-spiffe-identity-and-entitle-for-writes).

#### OCI

A stored API signing key is the only option. OCI's token exchange is tied to its identity
domains, and the dashboard does not federate with it. Keep the key's user in a group with
the narrowest policy that works, and keep the key in a vault (Secrets Management Tier 2+).

### BeyondTrust products

| Today | Recommended | Status |
|---|---|---|
| Password Safe and PRA OAuth client secrets, the EPM-L PAT, the Entitle API token, the Workload Credentials PAT | [BeyondTrust Platform Identity](../beyondtrust/platform-identity.md): each product's **Authentication** set to **Workload identity**, using the dashboard's SPIRE identity (or an Azure managed identity) | built (the Workload Credentials `/session` call has run live over SPIFFE) |

Platform identity covers the dashboard's own REST calls only. The Terraform providers,
`ps-cli` and the Secrets Safe backend still read the stored credentials on each panel: see
[What it does not cover](../beyondtrust/platform-identity.md#what-it-does-not-cover).

### Things that call the dashboard

| Today | Recommended | Status |
|---|---|---|
| a PAT or a service-account client secret | a service account bound to a [SPIFFE ID](../admin/service-accounts.md#spiffe-workloads-authenticate-with-the-svid-hold-nothing), or to a token [from your own IdP](../admin/service-accounts.md#tokens-from-your-own-idp-no-dashboard-secret) | built |
| a kubeconfig with a long-lived token for an on-prem k3s cluster | the cluster [trusts the dashboard's identity](oidc/dashboard-identity.md#on-prem-k3s) | built |

## Where a stored secret is still the answer

- **OCI**, above.
- **A site behind a TLS-inspecting proxy.** The agent's TLS connection to the SPIRE server
  must reach it intact, so such a site keeps Ed25519 enrolment.
- **A dashboard on Cloud Run** cannot host agent attestation: Cloud Run's ingress is
  HTTPS-only and terminates TLS. Its agents stay on Ed25519. See
  [GCP Cloud Run](cloud-hosting/gcp-cloud-run.md).
- **Terraform providers and `ps-cli`** for BeyondTrust products, and the Entitle Terraform
  provider key, as above.
- **The dashboard's JWT root key**, which encrypts the database. Removing it is a SaaS-edition
  item: see [Editions](../editions/comparison.md).

Keep each of these in a vault rather than the database once you can: see
[Secrets Management](../admin/secrets-management.md).

## Moving a surface over

1. **Make the cloud or product trust the identity first.** Nothing changes until you remove
   the stored secret, because **a stored key always wins**: the order is a Workload
   Credentials lease, then a stored key, then the SPIFFE identity, then whatever the SDK
   finds on its own.
2. **Check it.** The panel shows which credential each cloud is using. When federation is
   ready but a stored key is still set, it says so in amber: that install *looks* keyless
   and is not.
3. **Remove the stored secret** with the panel's **Remove stored key** button, then press
   **Test connection**. Test connection never mints a billed credential.
4. **Narrow the identity to read**, and give writes to a dynamic lease or a JIT grant
   (stage 3). On AWS, add a deny on privilege escalation to the everyday role, so a
   compromised read path cannot grant itself more.

Rolling back is putting the stored secret back: it wins again at once.

## Operating it

- **The SPIRE server's upstream CA key is now the most valuable secret in the install.**
  It is what every agent and cloud trusts. Keep a copy outside the install (Secrets Safe
  works) and plan its rotation: see
  [Protecting the keys](spiffe.md#protecting-the-keys).
- **An agent can be offline for about a week.** A join-token agent re-attests with its node
  SVID, which lives `agent_ttl` (168h, capped by the CA's remaining life). Off longer than
  that, it needs a fresh token from **New SPIRE token** on the Agents page.
- **A dynamic credential is billed per issuance.** Give Workload Credentials dynamic
  secrets a TTL of an hour or more; the dashboard shares one lease across every process.
- **A JIT grant is a grant.** Entitle may report a grant before the cloud applies it, and
  an auto-approved request is a real grant, so the dashboard never requests one to "test".

## Related

- [SPIFFE and SPIRE](spiffe.md): the dashboard's SPIRE server, and every feature that uses it.
- [Secrets Management](../admin/secrets-management.md): where a secret lives while you still store one.
- [Design: the dashboard as a SPIFFE workload](../design/dashboard-workload-identity.md).
