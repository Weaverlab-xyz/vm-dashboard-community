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

## SPIRE sidecar

**Remote agents cannot attest through SPIRE on Cloud Run.** Cloud Run's only ingress is
HTTPS, which it terminates itself, and an agent's mTLS gRPC to tcp/8081 has to reach the
server untouched ([why](../../spiffe.md#publishing-8081-to-agents)).
Agents on a Cloud Run dashboard stay on Ed25519, which is fully supported. It is one of
the few places SPIRE, the [recommended](../../access/workload-identity.md#where-a-stored-secret-is-still-the-answer)
agent identity, is not available; host the dashboard elsewhere if you want it.

The sidecar is still worth running for [the dashboard's own SPIFFE
identity](../../oidc/dashboard-identity.md), which needs nothing inbound beyond the
issuer's HTTPS discovery documents. Those are served by the app, through the gateway. In
the service YAML:

- **a third container** from `examples/cloud-spire`, with the variables from
  [The sidecar](../../spiffe.md#the-sidecar) (the secrets through
  Secret Manager, as for `DATABASE_URL`) and a startup probe of `httpGet /ready` on 8088;
- **an in-memory volume**, `emptyDir: {medium: Memory}`, mounted at `/run/spire-server` in
  both the sidecar and `app`, and `SPIRE_SERVER_SOCKET=/run/spire-server/api.sock` on `app`;
- **`--no-cpu-throttling` and `--min-instances=1` on the app service.** It is the same trap
  as the worker: a throttled instance freezes the server's key rotation between requests.

The worker relays through the database, so it does not change.
