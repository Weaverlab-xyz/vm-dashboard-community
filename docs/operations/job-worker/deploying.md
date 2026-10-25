# Deploying the job worker

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are running the worker as its own process or container, or more than one of them.

Part of [Job Worker](../job-worker.md).

The worker is a **second deployment of the same image** with a different command. It shares the
database, the config table and the state backend with the app, and it needs no ingress, no
volume and no cloud credentials of its own beyond what the app already has.

```
python -m web_dashboard.jobs_worker
```

## Docker Compose

Already wired: the `worker` service in `docker-compose.yml` (and `docker-compose.hub.yml`).
`scripts/onboard.sh` brings it up with the app. Replicas default to 1 — raise the caps before
raising `WORKER_REPLICAS`.

## Container Apps

The worker is its **own Container App**, not a sidecar in the app's. That is deliberate: a
sidecar shares the parent's replica count and scale rules, so scaling the worker would scale
gunicorn and the proxy with it, and the two have nothing in common in how they should scale.

```bash
az containerapp create -n dash-worker -g RG-CWeaver --environment cae-infradash --image chrweav/infra-dashboard:latest --command python --args "-m" "web_dashboard.jobs_worker" --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 1 --secrets db-url="postgresql://..." jwt-key="..." --env-vars DATABASE_URL=secretref:db-url JWT_SECRET_KEY=secretref:jwt-key APP_ENV=production
```

To change the pool later, without touching the caps:

```bash
az containerapp update -n dash-worker -g RG-CWeaver --set-env-vars DB_POOL_SIZE=8 DB_MAX_OVERFLOW=6
```

Four things to get right, in order of how badly they bite:

**1. Pin `--min-replicas 1 --max-replicas 1`.** The worker has no HTTP ingress, so
`minReplicas: 0` is not "scale to zero" — it is **off**. Nothing would ever generate the
traffic that scales it up, so no queued job would ever run, and the only symptom is jobs
sitting `pending` forever with nothing logged anywhere. At the other end, each replica is a
whole extra connection pool (see the budget above), so raise `max-replicas` only after you
have the connections for it.

**2. There is no CLI flag for `terminationGracePeriodSeconds`.** It is a template property, so
it needs `az containerapp update --yaml` (or the portal). Unset means the platform default of
30 seconds, which is why `WORKER_DRAIN_TIMEOUT_S` defaults to 20 — comfortably inside it. If
you raise the drain, raise the grace period first, or the platform kills the process before
the drain it was told to perform can finish.

**3. The app and the worker are separate Container Apps**, which means they can carry
**different values for the same setting names** with no code change and no role detection. This
is the cheapest concurrency you will find: moving connections from the request path to the
worker costs nothing in total.

```
dash        DB_POOL_SIZE=3  DB_MAX_OVERFLOW=5   ->  8 x 2 gunicorn workers = 16
dash-worker DB_POOL_SIZE=8  DB_MAX_OVERFLOW=6   ->  14
                                          total = 30, unchanged
worker clamp: (8 + 6 - 4) / 2 = 5  ->  the full heavy 2 + medium 1 + light 3 mix fits
```

**4. Size CPU and memory for the heavy tier, not the average.** A measured worker process
holds about **130 MB** with every cloud SDK loaded — boto3, the eight `azure-mgmt` packages,
the seven `google-cloud` ones, `oci`, `pyVmomi`. That is the floor; what varies is what a job
spawns *outside* the process. A Terraform apply is the `terraform` binary plus one provider
plugin process per provider in the module, each tens of MB, so `heavy=2` is the high-water
mark. On **0.5 vCPU / 1 GiB** — the reference deployment — that leaves roughly 890 MB for
subprocesses, which is comfortable for two applies. Raise memory before raising `heavy` past 2.

CPU matters less than it looks: these jobs are overwhelmingly waiting on cloud APIs, not
computing. 0.5 vCPU is fine for the shipped caps.

## The reference deployment

For comparison, what the live install actually runs (RG-CWeaver / eastus2):

| | `dash` | `dash-worker` |
|---|---|---|
| containers | `gateway` (Caddy) + `app` | `worker` |
| CPU / memory | 0.25 / 0.5 Gi + 1.0 / 2 Gi | 0.5 / 1 Gi |
| replicas | 1–1 | 1–1 |
| ingress | port 80 | **none** |
| health endpoint | `/api/health` on 8000 (via Caddy) | `/api/health` on 8080, pod-local |
| command | image default (gunicorn) | `python -m web_dashboard.jobs_worker` |
| database | `pg-infradash-601087`, Burstable B1ms | same |

Caddy is a sidecar *in the app's* Container App rather than its own, because that is what keeps
`TRUSTED_PROXY_HOSTS=127.0.0.1` true. The worker is the opposite case — nothing about it wants
to share a lifecycle with the request path.

The `dash` half of that table — the environment, the private database, the custom domains and
the gateway sidecar's own config — is covered in [cloud-hosting.md](../../platform/cloud-hosting.md), along
with the Cloud Run and ECS equivalents.
