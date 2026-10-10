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
