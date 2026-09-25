# Docker Hub overview — `chrweav/infra-dashboard`

<!--
SOURCE OF TRUTH for the Docker Hub repository description at
https://hub.docker.com/r/chrweav/infra-dashboard — Hub has no sync step in
.github/workflows/publish-images.yml, so the overview there is pasted by hand.

Edit THIS file, then paste everything below the divider into Hub's
"Repository overview" box. Keeping it in the repo is what stops the published
text drifting from docker-compose.hub.yml again: the compose block below is the
same stack that file ships, flattened to be self-contained (no .env, no
repo checkout).

Two rules Hub imposes that GitHub does not:
  * relative links do NOT resolve — every link below is an absolute URL.
  * there is no in-Hub link back to the compose file, so the quickstart has to
    carry a working stack inline rather than point at one.
-->

---

# Infrastructure Management Dashboard — Community Edition

A self-hosted web dashboard for managing infrastructure across **AWS, Azure, GCP and OCI**,
plus on-premises hypervisors (VMware, Hyper-V, Proxmox, Nutanix) by way of an
outbound-dialing remote agent. You bring your own cloud credentials; the dashboard deploys
into **your** accounts and stores nothing in anyone else's.

Deploy VMs, managed databases, Kubernetes clusters, containers and serverless functions;
build and promote images across clouds; run Ansible against what you deployed; and layer
privileged access on top of it (BeyondTrust PRA, Password Safe, Entitle).

* **Source, issues and docs:** https://github.com/Weaverlab-xyz/vm-dashboard-community
* **Getting started:** https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/docs/ONBOARDING.md
* **Intro video:** https://www.youtube.com/watch?v=RwMMBpfVg2o
* **Licence:** see the repository

---

## Tags and architectures

| Tag | What it is |
|---|---|
| `latest` | The most recent stable release. **Only moves on a version tag** — a manual CI dispatch publishes a `sha-…` tag and leaves `latest` alone. |
| `vX.Y.Z` (e.g. `v26.10.20`) | An exact release. Pin this for anything you care about. |
| `X.Y` | The latest patch on that minor line. |
| `sha-<commit>` | A one-off build from a manual workflow dispatch. Not a release. |

Every tag is multi-arch — **`linux/amd64` and `linux/arm64`** — so `docker pull` picks the
right build for Intel/AMD, Apple Silicon, AWS Graviton or a Raspberry Pi 5.

The container listens on **8000** and runs `gunicorn -w 2` with uvicorn workers.

---

## Quick start

> **If you have cloned the repository, don't use the compose below** — run
> `./scripts/onboard.sh --hub` (or `.\scripts\Onboard-Dashboard.ps1 -Hub` on Windows).
> It generates the secrets, writes `.env`, pulls these images and opens the setup wizard
> for you. The compose here is the no-checkout path: two files, nothing else.

**1. Generate the JWT root key.** This key encrypts every credential the dashboard stores,
so it lives in its own file rather than in an environment variable:

```bash
openssl rand -hex 32 > .jwt_secret_key
chmod 600 .jwt_secret_key
```

**2. Save this as `docker-compose.yml`:**

```yaml
services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: dashboardadmin
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-changeme}   # set a strong value for anything but a local trial
      POSTGRES_DB: vmclidashboard
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dashboardadmin -d vmclidashboard"]
      interval: 5s
      timeout: 5s
      retries: 5

  # The web app. Serves the UI and the API, and ENQUEUES long jobs rather than
  # running them — see the `worker` service below, which is not optional.
  app:
    image: chrweav/infra-dashboard:${INFRA_DASHBOARD_TAG:-latest}
    restart: unless-stopped
    ports:
      - "8001:8000"          # browse at http://localhost:8001
    environment:
      - APP_ENV=development
      - DATABASE_URL=postgresql://dashboardadmin:${POSTGRES_PASSWORD:-changeme}@db:5432/vmclidashboard
      - JWT_SECRET_KEY_FILE=/run/secrets/jwt_key
      # Where the transient kubectl/helm runner containers exchange files. The
      # runner is a SIBLING container launched through the host docker socket,
      # so this must be a NAMED VOLUME referenced by name — a bind of an
      # in-container temp dir resolves empty on the host and helm then talks to
      # localhost:8080.
      - RUNNER_WORK_DIR=/runner-work
      - RUNNER_WORK_VOLUME=vmdash_runner_work
    secrets:
      - jwt_key
    depends_on:
      db:
        condition: service_healthy
    volumes:
      # Only needed for the Ansible / Kubernetes integrations, which launch
      # one-shot sibling containers. Drop this mount if you don't use them.
      - /var/run/docker.sock:/var/run/docker.sock
      - runner_work:/runner-work

  # Background job runner. REQUIRED — cluster and database provisions, Packer
  # builds, image exports and promotes, VM deploys and the auto-delete sweep are
  # claimed only by this process. Without it those jobs are accepted by the UI
  # and then sit pending forever.
  #
  # Same image as `app`, different command; shares the database, the config and
  # the state backend. No HTTP port is published: its health endpoint exists for
  # platform probes, and publishing it would collide under `--scale`.
  worker:
    image: chrweav/infra-dashboard:${INFRA_DASHBOARD_TAG:-latest}
    command: ["python", "-m", "web_dashboard.jobs_worker"]
    restart: unless-stopped
    deploy:
      # ONE worker runs SEVERAL jobs at once (tiered light/medium/heavy caps), so
      # start at 1 replica and raise the caps before adding replicas: slots inside
      # one process share ONE database connection pool, while N replicas need N.
      # The queue claim is atomic, so replicas never double-execute a job.
      replicas: ${WORKER_REPLICAS:-1}
      resources:
        limits:
          cpus: "${WORKER_CPU_LIMIT:-2}"
          memory: ${WORKER_MEM_LIMIT:-2g}
    environment:
      - APP_ENV=development
      - DATABASE_URL=postgresql://dashboardadmin:${POSTGRES_PASSWORD:-changeme}@db:5432/vmclidashboard
      - JWT_SECRET_KEY_FILE=/run/secrets/jwt_key
      - RUNNER_WORK_DIR=/runner-work
      - RUNNER_WORK_VOLUME=vmdash_runner_work
    secrets:
      - jwt_key
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - runner_work:/runner-work

volumes:
  pgdata:
  # Shared between app/worker and the sibling runner containers they launch.
  # The explicit name (no compose-project prefix) is what lets a runner mount it
  # by the name in RUNNER_WORK_VOLUME.
  runner_work:
    name: vmdash_runner_work

secrets:
  jwt_key:
    file: .jwt_secret_key
```

**3. Start it:**

```bash
POSTGRES_PASSWORD='<a strong password>' docker compose up -d
```

Then open **http://localhost:8001** and complete the setup wizard: create the admin
account, then paste cloud credentials (access key / service principal / service-account
JSON) or skip a cloud and explore the UI. Credentials are encrypted with AES-256 and held
in the database — nothing sensitive is written to disk.

Pin a release instead of tracking `latest`:

```bash
INFRA_DASHBOARD_TAG=v26.10.20 docker compose up -d
```

---

## The worker (what changed)

Earlier versions of this page shipped a single `app` service. That is no longer a complete
stack. Long-running work was moved out of the gunicorn request workers into a dedicated
`worker` process so it survives worker recycling, crashes and redeploys.

**Jobs the worker owns** — cluster and database provisions and decommissions, Packer image
builds, image exports/promotes/copies, VM deploys and destroys, PRA tunnels, Kubernetes
add-ons and management-plane installs, local Ansible runs, the auto-delete sweep. The web
app writes the row; the worker claims it.

**If you run `app` without `worker`,** the UI works, pages load and the job is accepted —
and then nothing happens. There is no fallback executor for these types. (One narrow
exception: the dashboard's own tile statistics have an in-app collector, so the front page
still populates.)

**Concurrency.** One worker runs several jobs at a time, capped by kind — light 3 (start a
cloud operation and poll it), medium 1 (SDK plus a short terraform/kubectl), heavy 2
(terraform apply, Packer), with an overall total of 3. The tier caps may sum higher than
the total: they cap the *mix*, the total caps the *total*. Tune them in
**Settings → Job Worker**, which applies within ~5 seconds with no restart;
`WORKER_*_CONCURRENCY` environment variables are only the bootstrap defaults.

**The database connection pool is usually the real limit.** A running job holds more than
one connection, so the worker reduces your configured total to what the pool can serve and
reports the reduction in Settings → Job Worker. Roughly,
`usable = (DB_POOL_SIZE + DB_MAX_OVERFLOW - 4) / 2`. Per process the budget is
`DB_POOL_SIZE + DB_MAX_OVERFLOW`; the app runs two gunicorn workers and each worker one
pool, so a default deployment holds `3 × (size + overflow)`. On a small managed Postgres
(Azure Burstable B1ms allows 50 connections) that budget, not CPU, is what caps throughput.

Full reference, including running the worker as its own Azure Container App:
https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/docs/job-worker.md

---

## Environment variables

Cloud credentials and feature flags are configured **in the browser**, not here. These are
the bootstrap settings only.

| Variable | Applies to | Notes |
|---|---|---|
| `DATABASE_URL` | app, worker | Required. Both services must point at the same database. |
| `JWT_SECRET_KEY_FILE` | app, worker | Path to the key file (Docker secret). Use `JWT_SECRET_KEY` instead if you are not using compose secrets. Both services need the **same** key — it is the root of trust for every stored credential. |
| `APP_ENV` | app, worker | `development` for a local Docker Desktop run. |
| `RUNNER_WORK_DIR` / `RUNNER_WORK_VOLUME` | app, worker | Shared work dir for sibling kubectl/helm runners. Must name a real named volume. |
| `WORKER_HEAVY_CONCURRENCY`, `WORKER_MEDIUM_CONCURRENCY`, `WORKER_LIGHT_CONCURRENCY`, `WORKER_MAX_CONCURRENCY` | worker | Bootstrap defaults only; Settings → Job Worker wins after first run. |
| `WORKER_DRAIN_TIMEOUT_S` | worker | Seconds to let running jobs finish on shutdown. Keep it **under** your platform's termination grace period (30s on Azure Container Apps). |
| `WORKER_HEALTH_PORT` | worker | Health endpoint (default 8080) for platform probes; answers 503 until the run loop is actually turning. Not published to the host by the compose above. |
| `WORKER_REPLICAS`, `WORKER_CPU_LIMIT`, `WORKER_MEM_LIMIT` | compose only | Replicas **multiply** the caps above. Prefer raising the caps. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | app, worker | Cannot be set from Settings — the engine is built before the app can read the database. |
| `PUBLIC_BASE_URL` | app | Set when something sits in front of the dashboard: OAuth callbacks and the remote-agent signing audience are built from it. |
| `NOTIFY_BASE_URL` | worker | The address operators reach the dashboard on. Without it, notifications ship with no link — the worker has no request context to infer one. |

Full annotated list:
https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/.env.example

---

## Companion images

The dashboard launches most of these for you; you do not run them by hand.

| Image | What it is |
|---|---|
| `chrweav/ansible-cloud` | The Kubernetes / cloud-database localhost runner. The dashboard's default config points at `:latest`, so it must exist alongside the release you run. |
| `chrweav/ansible-winrm` | Config-management runner for Windows targets over WinRM. |
| `chrweav/dashboard-promote-runner` | Cross-cloud image promote, run transiently in the target cloud. |
| `chrweav/dashboard-agent` | The remote on-premises agent. **You** pull this one, inside the private network — it dials out, so there are no inbound ports. The install command the Agents page hands out names this image. |
| `chrweav/hypervisor-runner` | The agent's one-shot Hyper-V (WinRM/NTLM) and bare ESXi (SOAP) runner. Pulled by the operator on the agent host, never by the agent itself. |

All six are built from the same release tag and are multi-arch.

---

## Data, upgrades and security

* **State lives in Postgres** (`pgdata`). The image is disposable; the volume is not. Back
  it up before an upgrade.
* **Upgrading:** `docker compose pull && docker compose up -d`. Both `app` and `worker` run
  the schema migration at startup; it is idempotent and advisory-locked, so they can start
  in any order or simultaneously. Keep them on the **same tag** — they share one database
  schema, and a split-version pair is untested.
* **Keep `.jwt_secret_key`.** Lose it and every stored credential becomes undecryptable;
  there is no recovery path. It is deliberately not in `.env`, so that file stays safe to
  inspect.
* **Set a real `POSTGRES_PASSWORD`** for anything beyond a local trial.
* **The docker socket mount is a privilege grant.** It exists so the app can launch
  one-shot sibling runners; remove it if you don't use the Ansible or Kubernetes
  integrations.
* **Exposure:** the quickstart binds to localhost and is meant for a local host. Put it
  behind TLS and set `PUBLIC_BASE_URL` before exposing it — see
  https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/docs/cloud-hosting.md
* Vulnerability reports: https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/SECURITY.md
