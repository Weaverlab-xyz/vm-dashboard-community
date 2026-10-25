# The dashboard's own identity: federating with the clouds

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the dashboard to authenticate to a cloud with a short-lived token from its own SPIRE server instead of a stored key, or you are checking why a token file is missing or refused.

Part of [OIDC and single sign-on](../oidc.md). The dashboard publishes an OIDC issuer of its own, and AWS, Azure, GCP, k3s and BeyondTrust Workload Credentials can trust the short-lived tokens it signs instead of a stored key. The tokens come from the same SPIRE server that [attests remote agents](../../admin/remote-agents/spire-attestation.md); the server itself is on [SPIFFE and SPIRE](../spiffe.md).

**This is the recommended way for the dashboard to reach a cloud**, in place of a stored
access key, client secret or service-account key; why, and what to pair it with so the
identity holds no standing write privilege, is on
[Workload identity](../workload-identity.md#the-dashboards-cloud-credentials). The
dashboard keeps a short-lived JWT-SVID per audience in a file, publishes the discovery
document and keys a cloud fetches to verify it, and — once you retire a stored key — uses
those tokens for its own AWS, Azure and GCP calls, including Terraform and Packer.

| Cloud | Status |
|---|---|
| AWS | **proven live** 2026-10-15: `AssumeRoleWithWebIdentity` from a dashboard on Azure Container Apps, with Workload Credentials authenticated over the same identity; also run as the everyday credential, with jobs on a Workload Credentials provisioning lease |
| Azure | built and covered by tests; not yet run against a live tenant |
| GCP | built and covered by tests; not yet run against a live project |

The design, including what is not built, is
[The dashboard as a SPIFFE workload](../../design/dashboard-workload-identity.md).

## What it does

With **Settings → Integrations → Remote Agents → Give the dashboard its own SPIFFE
identity** ticked, the app keeps one token file per audience you tick:

| Audience | File | Token `aud` |
|---|---|---|
| AWS | `/run/spiffe-tokens/aws.jwt` | `sts.amazonaws.com` |
| Azure | `/run/spiffe-tokens/azure.jwt` | `api://AzureADTokenExchange` |
| GCP | `/run/spiffe-tokens/gcp.jwt` | the workload identity provider's full resource name, as entered |
| Workload Credentials | `/run/spiffe-tokens/wlc.jwt` | the identity audience under Settings → Workload Credentials |

Every token is for `spiffe://<trust domain>/dashboard`, lives fifteen minutes, and is
re-minted at half that. The files sit on a memory-only volume: written by the app, readable
by the worker, never on the host disk. Unticking an audience deletes its file at once, so
anything still pointed at it fails now rather than when it expires.

**A stored key still wins.** For each cloud the order is: a Workload Credentials lease, then
the stored key, then this identity, then whatever the SDK finds on its own. So ticking
the box changes nothing until you clear that cloud's stored key — do that only after the
cloud trusts the issuer (below). The panel shows which one each cloud is using, and when it
is not this identity, why not. When federation is otherwise ready but a stored key is still
set, it says so in amber: the install looks keyless and is not until you remove the key.

## Setting it up

1. **Run the dashboard's SPIRE server** with an issuer. On Docker Compose that is the
   overlay below; on a managed container runtime, set `SPIRE_JWT_ISSUER` on
   [the sidecar](../spiffe.md#the-sidecar) instead.

   ```bash
   SPIRE_TRUST_DOMAIN=dashboard.example.com \
   SPIRE_JWT_ISSUER=https://agents.example.com/spiffe \
   docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
   ```

   ```powershell
   $env:SPIRE_TRUST_DOMAIN = 'dashboard.example.com'
   $env:SPIRE_JWT_ISSUER   = 'https://agents.example.com/spiffe'
   docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
   ```

2. **Tick the setting** and the audiences you want. The **Issuer** field shows what this
   dashboard publishes. By default that is the pinned agent audience plus `/spiffe`,
   because the agent gateway is the part of an install built to be reachable from
   outside, and its Caddyfile publishes `/spiffe/*`. `SPIRE_JWT_ISSUER` must equal it
   character for character.
3. **Check the files.** The panel lists each one with its expiry or the reason it is
   missing.
4. **Register the issuer with the cloud.** Use the issuer URL, and pin the subject to
   `spiffe://<trust domain>/dashboard` exactly.

## Making each cloud trust it

**With Terraform:** [`terraform/dashboard_federation`](../../../terraform/dashboard_federation/README.md)
has one small module per cloud (`aws/`, `gcp/`, `azure/`). Each creates the trust below and
outputs the values to paste into the panel. Apply only the clouds you use.

**By hand:** the commands below do the same. Replace `agents.example.com/spiffe` with your
issuer and `dashboard.example.com` with your trust domain. In every case the condition is
on the **exact subject** `spiffe://<trust domain>/dashboard` — see the next section for why.

This works **with or without BeyondTrust Workload Credentials**. Without it, the federated
identity is the dashboard's only cloud credential and nothing secret is stored. With it,
the federated identity can be the everyday credential while jobs take a Workload
Credentials lease (`wlc_aws_everyday_source` / `wlc_azure_everyday_source` set to
`federation`; see [dynamic credentials](../../preview/workload-lab/dynamic-credentials.md)).

### AWS

```bash
aws iam create-open-id-connect-provider \
  --url https://agents.example.com/spiffe --client-id-list sts.amazonaws.com

cat > trust.json <<'JSON'
{"Version": "2012-10-17", "Statement": [{
  "Effect": "Allow",
  "Principal": {"Federated": "arn:aws:iam::123456789012:oidc-provider/agents.example.com/spiffe"},
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {"StringEquals": {
    "agents.example.com/spiffe:sub": "spiffe://dashboard.example.com/dashboard",
    "agents.example.com/spiffe:aud": "sts.amazonaws.com"}}}]}
JSON
aws iam create-role --role-name vm-dashboard --assume-role-policy-document file://trust.json
```

Attach the policies the dashboard needs to that role, then tick **AWS**, paste the role
ARN, and clear the stored access key. Terraform's provider and S3 state backend and Packer
get the assumed role's session credentials.

### Azure

On the app registration the dashboard already uses (its client id and tenant id stay
under the Azure setup):

```bash
az ad app federated-credential create --id <application client id> --parameters \
  '{"name": "vm-dashboard-spiffe",
    "issuer": "https://agents.example.com/spiffe",
    "subject": "spiffe://dashboard.example.com/dashboard",
    "audiences": ["api://AzureADTokenExchange"]}'
```

```powershell
@{ name = 'vm-dashboard-spiffe'; issuer = 'https://agents.example.com/spiffe'
   subject = 'spiffe://dashboard.example.com/dashboard'
   audiences = @('api://AzureADTokenExchange') } | ConvertTo-Json | Set-Content fic.json
az ad app federated-credential create --id <application client id> --parameters '@fic.json'
```

Then tick **Azure** and clear the Azure client secret. Every Azure call, Key Vault, Blob
storage and Terraform (`ARM_USE_OIDC`) then use the identity.

**Packer image builds** use it too, differently. The azure-arm builder reads its credential
once, from a template variable, and signs in again whenever its Azure token runs out, so
the 15-minute token file would expire mid-build. Each build instead gets its own client
assertion (`client_jwt`): the same identity, audience and checks, minted for that build
with a lifetime set by *Packer image builds: assertion lifetime* (default 120 minutes,
15–480). Set it above your longest Azure image build. The assertion lives only in the
build's environment as a sensitive variable, never in the template file or the log. It is
a bearer credential for its lifetime, which is still far less than a permanent secret. A
Workload Credentials lease or a stored secret, when there is one, is used instead.

### GCP

```bash
gcloud iam workload-identity-pools create vm-dashboard --location=global
gcloud iam workload-identity-pools providers create-oidc dashboard \
  --location=global --workload-identity-pool=vm-dashboard \
  --issuer-uri=https://agents.example.com/spiffe \
  --attribute-mapping="google.subject=assertion.sub" \
  --attribute-condition="assertion.sub == 'spiffe://dashboard.example.com/dashboard'"
```

Paste the provider's full name —
`//iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/vm-dashboard/providers/dashboard`
— as the GCP audience, and set `gcp_project_id` (a federated credential carries no
project). Then either grant roles to the principal
`principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/vm-dashboard/subject/spiffe://dashboard.example.com/dashboard`
directly, or let it impersonate a service account:

```bash
gcloud iam service-accounts add-iam-policy-binding vm-dashboard@<project>.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/vm-dashboard/subject/spiffe://dashboard.example.com/dashboard"
```

and enter that service account in the panel. Clear the stored service-account key. The app
writes `gcp-external-account.json` beside the token, and Terraform and Packer read it
through `GOOGLE_APPLICATION_CREDENTIALS`.

**OCI** has no federated path here; it keeps its signing key.

### On-prem k3s

Here the identity replaces the admin client certificate the dashboard uses on a `cloud=local`
cluster. Nothing needs to be ticked under the audiences: the token is minted on demand, per
cluster, for the audience `<issuer>/k8s/<cluster id>`, lives thirty minutes, and goes only
into the kubeconfig of the call that needs it — never into a file. Run
`examples/playbooks/k3s/k3s-dashboard-auth.yml` on the server node, then tick **Trusts
dashboard identity?** on the cluster's row; the button shows the command with the values
filled in. The API server must be able to fetch `<issuer>/.well-known/openid-configuration`
and `<issuer>/keys`; for an issuer with a private CA, pass it as `dashboard_issuer_ca_pem`.
Details and the break-glass are in [Kubernetes](../../cloud-services/kubernetes.md#access--identity).

### BeyondTrust Workload Credentials

Pathfinder's **Workload Identity federation** accepts any OIDC issuer as a **Custom IDP**,
so it can trust this one the way the clouds do. The dashboard then authenticates to
Workload Credentials with its own SPIFFE token instead of a stored Personal Access Token
(PAT), and needs no cloud identity underneath. That makes it the PAT-free option for the
installs the other identity platforms cannot serve: on-prem, Docker on a VM, and AWS ECS.

1. **Choose an audience**, any string, for example `beyondtrust-wlc`. Under
   **Settings → Integrations → Workload Credentials**, set **Authentication** to *Workload
   identity (nothing stored)*, **Identity platform** to *The dashboard's own SPIFFE identity*, and
   **Token audience** to that string.
2. **Tick the audience** under **Settings → Integrations → Remote Agents → Give the
   dashboard its own SPIFFE identity → Workload Credentials**. The app keeps
   `/run/spiffe-tokens/wlc.jwt` with that `aud`. The worker reads the same file, so jobs
   authenticate the same way.
3. **Register the trust in Pathfinder:** **Administration → Workload Identities →
   Register Workload Identity**, type **Custom IDP**:

   | Field | Value |
   |---|---|
   | Issuer | the issuer from the panel, for example `https://agents.example.com/spiffe` |
   | Claim condition | `sub` equals `spiffe://<trust domain>/dashboard` |
   | Claim condition | `aud` equals the audience from step 1 |
   | Site | the site in `wlc_site_id` |

   Copy the registration's **Service Name** into **Service name** on the Workload
   Credentials panel. It travels as `X-BT-Service-Name` on every request.
4. **Test connection** on the Workload Credentials panel. Then clear the stored PAT.

Pathfinder is a hosted service, so the issuer must be reachable from the internet with a
publicly trusted certificate, as for AWS and Azure (see
[where it has to be reachable from](#where-it-has-to-be-reachable-from)).

**Not yet run against a live Pathfinder tenant.** The token, the issuer and the request are
the same ones the clouds use. Whether a Custom IDP registration fetches the keys from
`<issuer>/keys` through the discovery document, as the clouds do, is unconfirmed.

**What it concentrates.** On this platform, Workload Credentials and every lease it issues
rest on the SPIRE server's signing key. Whoever controls the app can mint the token (see
[what it does not protect against](#what-it-does-not-protect-against)), and a PAT carries the
same exposure. [Protect the SPIRE server's CA key](#protect-the-spire-servers-ca-key)
applies with more force here.

## Pin the subject, never the trust domain

Remote agents and service-account workloads share the dashboard's trust domain. A trust
policy that accepts any `spiffe://<trust domain>/…` hands each of them the dashboard's cloud
access. Every cloud lets you condition on the exact `sub`; use it.

## Where it has to be reachable from

The issuer serves two public documents, both 404 until the setting is on:

- `<issuer>/.well-known/openid-configuration`
- `<issuer>/keys`, the JWT signing keys, read live from the SPIRE server and cached five
  minutes, falling back to the stored daily copy if the server is down.

AWS and Azure fetch them anonymously from the internet, over HTTPS with a publicly trusted
certificate. A plain-`http` issuer is refused before anything is minted. GCP can instead be
given the keys by upload, which works for a private dashboard but has to be repeated at
every key rotation (weekly by default).

These documents carry public keys and nothing else. Do not put them behind SSO: every cloud
that trusts the issuer would stop accepting the dashboard's tokens at once.

Both gateways publish them on the agent hostname: `examples/remote-agent/Caddyfile` on
Docker Compose, and `examples/cloud-gateway/Caddyfile` — the sidecar on Azure Container Apps,
Cloud Run and ECS. A cloud gateway image built before `/spiffe/*` was added answers 404 on
the agent hostname, and AWS then refuses every token with `InvalidIdentityToken`. Rebuild the
gateway image and roll the sidecar.

On a POV instance the POV cloud provider panel can use this identity in place of an AWS key:
see [Keyless AWS](../../profiles/pov/public-cloud/keyless-aws.md).

**Check before retiring a key.** On the panel, **Check the issuer is reachable** fetches both
documents over the issuer's public URL with TLS verified, the way a cloud does. It reports
whether the name resolves, the certificate is publicly trusted, the document names exactly
this issuer, and the keys served are this dashboard's current ones. It runs from inside the
dashboard, so a firewall that lets the dashboard out but not the internet in can still pass
it; the first token a cloud accepts is the final proof.

The SPIRE server also serves its trust bundle on **tcp/8082**, a SPIFFE bundle endpoint.
That is a different thing from the issuer: another SPIRE server fetches it once a
[Workload Lab is federated](../../preview/workload-lab/spiffe.md#federating-with-the-dashboard) with
this one. `docker-compose.spire.yml` publishes it directly, not through Caddy, because it is
TLS with the server's own SVID. Firewall it to the lab hosts, or leave it closed if you
federate nothing.

## What it does not protect against

The app administers the SPIRE server, so anyone who compromises the app can mint tokens
for the dashboard's identity, and use them while they are present. A stored key, by
contrast, can be copied and kept. That is the improvement: a credential that needs continued
presence, instead of one that works offline for ever. It is not "the app holds nothing".

## Protect the SPIRE server's CA key

Once this identity is on, the SPIRE server's keys sign every token a cloud accepts in
place of the stored key you retired, as well as agent attestation and service-account
SVIDs. Where those keys live depends on how the server runs. On Compose it is a volume,
or a cloud KMS; on a managed runtime it is an upstream CA held as a secret. See
[SPIFFE and SPIRE → Protecting the keys](../spiffe.md#protecting-the-keys).

## Settings

All on **Settings → Integrations → Remote Agents**, under **Give the dashboard its own SPIFFE
identity**. The SPIRE server itself is set up in
[Attesting an agent through SPIRE](../../admin/remote-agents/spire-attestation.md#settings).

| Setting | Key | Default | What it does |
|---|---|---|---|
| Give the dashboard its own SPIFFE identity | `dashboard_spiffe_identity_enabled` | off | Keeps the token files and publishes `<issuer>/.well-known/openid-configuration` and the keys |
| Issuer | `dashboard_spiffe_issuer` | blank: the pinned agent audience plus `/spiffe` | Must equal `SPIRE_JWT_ISSUER` exactly; HTTPS and internet-reachable for AWS and Azure |
| AWS | `dashboard_spiffe_aud_aws` | off | Keeps `aws.jwt` (`aud` `sts.amazonaws.com`) |
| AWS role | `aws_federation_role_arn` | blank | The role the dashboard assumes with `AssumeRoleWithWebIdentity` |
| Azure | `dashboard_spiffe_aud_azure` | off | Keeps `azure.jwt`. Uses the Azure setup's client and tenant ids; clear its client secret to switch |
| GCP workload identity provider | `dashboard_spiffe_aud_gcp` | blank | The provider's full resource name, which is also the token's `aud`; blank keeps no `gcp.jwt` |
| GCP service account to impersonate | `gcp_federation_service_account` | blank | Optional: impersonate this service account after the STS exchange, instead of granting the federated principal directly |
| Workload Credentials | `dashboard_spiffe_aud_wlc` | off | Keeps `wlc.jwt` for the identity audience set under Settings → Workload Credentials |

The k3s side is per cluster: **Trusts dashboard identity?** on the cluster, described
[above](#on-prem-k3s).

## Troubleshooting

| Symptom | Cause |
|---|---|
| *the SPIRE server signs with issuer '(none)'* | `SPIRE_JWT_ISSUER` is blank on the server. Set it to the issuer the panel shows and restart the SPIRE container. No file is written until they match. |
| *the SPIRE server signs with issuer 'X', but this dashboard publishes discovery as 'Y'* | The two disagree. Change either one so they match exactly, trailing slash included. |
| *is not https* | The issuer resolved to `http://`. Set the issuer explicitly, or fix the pinned agent audience. |
| *container 'vmdash-spire-server' is not running* | The SPIRE overlay is not up, or its container name differs from **SPIRE server container**. The previous token file is kept until it expires. |
| *Workload Credentials is ticked but its identity audience is blank* | Set the audience under Settings → Workload Credentials first. |
| A cloud says *not federated: …* in the panel | The reason is the one thing left to do — a role ARN, the Azure tenant id, `gcp_project_id`, or a stored key that still wins. |
| *AWS refused the dashboard's SPIFFE identity … InvalidIdentityToken* | AWS could not verify the token: the issuer is not reachable over HTTPS with a publicly trusted certificate, or the IAM OIDC provider's URL differs from the issuer. |
| *AWS refused … AccessDenied* | The role's trust policy does not match: check the `:sub` and `:aud` conditions against the issuer's host and path. |
| *marked as trusting the dashboard's identity, but …* on a k8s operation | The cluster's flag is on but no token could be minted. Fix the cause it names, or untick **Trusts dashboard identity?** to fall back to the admin kubeconfig. |
| `/spiffe/keys` answers 503 | Neither the live server nor a stored bundle has a JWT key yet. Start the SPIRE server; the stored copy is written on the first sync. |
