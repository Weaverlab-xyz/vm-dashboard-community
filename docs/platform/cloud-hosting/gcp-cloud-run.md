# Cloud Hosting: GCP Cloud Run

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are running the dashboard on GCP Cloud Run.

Part of [Cloud Hosting](../cloud-hosting.md).

> The architecture below is the Azure one expressed in Cloud Run primitives. It
> has not been run end to end, so treat the flags as a starting point rather
> than a transcript.

Cloud Run supports sidecars, so the gateway pattern carries over: mark the
gateway as the ingress container and the app as a plain one.

Three platform-specific decisions do most of the work:

**`--no-cpu-throttling` on the worker, and `--min-instances=1`.** A Cloud Run
service is throttled to near-zero CPU outside a request. The worker never
*serves* a request — it polls a database — so with the default it would be
frozen most of the time and jobs would crawl. This is the Cloud Run equivalent
of the `minReplicas: 0` trap.

**Cloud SQL over private IP with direct VPC egress**, not the Cloud SQL Auth
Proxy sidecar — you already have a sidecar and the app speaks plain
`postgresql://`. Direct VPC egress is region-locked, so put the service in the
same region as the instance.

**Secret Manager for `DATABASE_URL` and `JWT_SECRET_KEY`**, injected with
`--set-secrets`. Grant the runtime service account `secretmanager.secretAccessor`
on those two secrets only.

```bash
gcloud run deploy dash-worker \
  --image chrweav/infra-dashboard:latest --region us-east1 \
  --command python --args=-m,web_dashboard.jobs_worker \
  --no-cpu-throttling --min-instances=1 --max-instances=1 --no-allow-unauthenticated \
  --set-secrets=DATABASE_URL=dash-db-url:latest,JWT_SECRET_KEY=dash-jwt-key:latest \
  --set-env-vars=APP_ENV=production,PUBLIC_BASE_URL=https://dash.example.com
```

## Pathfinder without a PAT

Cloud Run hands every revision a Google-signed OIDC identity token from the metadata
server, so the dashboard can reach BeyondTrust Pathfinder with **no stored Personal
Access Token**. This applies if you use [Workload Credentials](../../beyondtrust/workload-credentials.md);
the [Azure walkthrough](azure-container-apps/workload-identity.md) explains the trust
model, and this section is the Cloud Run version of it.

1. **Give the service an identity.** Both `dash` and `dash-worker` need a runtime
   service account, and **the worker is where credentials are minted**, so set it on both:

   ```bash
   gcloud run services update dash --region us-east1 --service-account dash-runtime@PROJECT.iam.gserviceaccount.com
   gcloud run services update dash-worker --region us-east1 --service-account dash-runtime@PROJECT.iam.gserviceaccount.com
   ```

   The account needs no IAM roles for this; the token proves who it is, not what it may do.

2. **Register the trust in Pathfinder** (a GUI action, by design): **Administration →
   Workload Identities → Register Workload Identity**, type **Custom IDP**.
   - **Issuer:** `https://accounts.google.com`
   - **Claim conditions** (AND-matched): `aud` equals the audience you choose in step 3,
     and `email` equals the service account's address (or `sub` equals its numeric unique
     ID). Pin the account, not just the issuer: every Google workload shares the issuer.
   - Grant the Product Scopes and site exactly as in the Azure walkthrough.

3. **Settings → Integrations → Workload Credentials:**
   - **Authentication** → *Workload identity (nothing stored)*
   - **Identity platform** → *GCP — Cloud Run, GCE, GKE*
   - **Service name** → the Service Name from step 2
   - **Token audience** → any string, as long as it matches the `aud` condition. A URL such
     as `https://pathfinder.example.com` is conventional.

4. **Save, then press Test connection.** It is an unmetered `GET /session`, and on success
   it prints the token's real **`aud`** and **`exp`**. Compare that `aud` with the
   registration: a mismatch is the usual cause of a bare `HTTP 401`.

| What you see | What it means |
|---|---|
| `could not get a workload identity token on gcp` | no service account is attached to this revision, or the metadata server is unreachable |
| `HTTP 401` from Workload Credentials | the token was fine and Pathfinder declined it: wrong site, a Service Name that does not match (a trailing space is **not** trimmed), an `email`/`sub` that is not this account, or an `aud` that differs from the token audience |

The token is fetched per process and cached until five minutes before its `exp`; a fetch
is free, so `dash` and `dash-worker` each keep their own. Once Test connection passes,
clear `wlc_pat`: switching modes does not delete it.

**Status:** unit-tested, not yet run against a live Pathfinder registration on Cloud Run.

## SPIRE sidecar

**Remote agents cannot attest through SPIRE on Cloud Run.** Cloud Run's only ingress is
HTTPS, which it terminates itself, and an agent's mTLS gRPC to tcp/8081 has to reach the
server untouched ([why](../spiffe.md#publishing-8081-to-agents)).
Agents on a Cloud Run dashboard stay on Ed25519, which is fully supported. It is one of
the few places SPIRE, the [recommended](../workload-identity.md#where-a-stored-secret-is-still-the-answer)
agent identity, is not available; host the dashboard elsewhere if you want it.

The sidecar is still worth running for [the dashboard's own SPIFFE
identity](../oidc/dashboard-identity.md), which needs nothing inbound beyond the
issuer's HTTPS discovery documents. Those are served by the app, through the gateway. In
the service YAML:

- **a third container** from `examples/cloud-spire`, with the variables from
  [The sidecar](../spiffe.md#the-sidecar) (the secrets through
  Secret Manager, as for `DATABASE_URL`) and a startup probe of `httpGet /ready` on 8088;
- **an in-memory volume**, `emptyDir: {medium: Memory}`, mounted at `/run/spire-server` in
  both the sidecar and `app`, and `SPIRE_SERVER_SOCKET=/run/spire-server/api.sock` on `app`;
- **`--no-cpu-throttling` and `--min-instances=1` on the app service.** It is the same trap
  as the worker: a throttled instance freezes the server's key rotation between requests.

The worker relays through the database, so it does not change.
