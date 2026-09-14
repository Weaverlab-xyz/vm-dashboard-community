# Cloud Hosting

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the dashboard reachable from outside your LAN, or fronting remote agents.

Running **the dashboard itself** as a managed container in your own cloud
account — Azure Container Apps, GCP Cloud Run, or AWS ECS — instead of Docker
Compose on a host you maintain.

[ONBOARDING.md](ONBOARDING.md) covers the Compose path and is still the right
starting point. Come here when you want the dashboard reachable from outside
your LAN, surviving a laptop reboot, or fronting remote agents.

> **This is not the hosted SaaS edition.** What follows is the community
> edition, single-tenant, with the JWT root key supplied as a platform secret.
> The hosted edition's differences are narrower and more specific than "runs in
> a cloud": the root key is never a static credential, and one deployment serves
> many tenants. See [saas-comparison.md](saas-comparison.md). If you have read
> [saas-roadmap.md](saas-roadmap.md) and remember Container Apps being ruled
> out — that was about *per-tenant* SaaS revisions and their cost, not about
> self-hosting one instance, which is what the reference install has run all
> along.

---

## What you lose, and what you keep

A managed container runtime gives you no Docker socket and no LAN. Two features
depend on those, and the rest do not.

| | Cloud-hosted |
|---|---|
| Cloud VM / database / Kubernetes provisioning (all four clouds) | **Works.** Terraform and Packer run as ordinary subprocesses. |
| Kubernetes management — kubectl, helm, External Secrets, Rancher | **Works.** Both binaries are baked into the image and run in-process, specifically so no socket is needed. |
| Image builds and cross-cloud promotion | **Works.** |
| Config Management against **cloud** targets | **Works**, but you must set the runner to `ecs` / `aci` / `gcp` in Settings → Ansible. The `local` runner shells out to `docker run`. |
| Config Management against **on-prem** targets — VMware, Proxmox, Hyper-V, locally-registered clusters | **Broken**, and not only because of the socket: the dashboard has no route to your lab. The run is refused with a message saying so. Use [remote agents](remote-agents.md). |
| Local Filesystem / UNC storage backend | **Unavailable.** Pick a cloud bucket on `/storage`. |

---

## Four things that will bite you

These are ordered by how quietly they fail. The first three produce a dashboard
that looks like it is working.

**1. `DATABASE_URL` unset does not fail — it silently uses SQLite.** The default
is `sqlite:///./vm_cli.db`, a file inside the container. So the app and the
worker each get their *own private database*, the worker never sees a queued
job, and everything vanishes on the next revision. Compose injects this variable
for you, which is why it does not appear in `.env.example`; on a managed runtime
nothing injects it. Set it explicitly, always.

**2. `JWT_SECRET_KEY` unset does not fail either — it regenerates per process.**
The default is `secrets.token_hex(32)` evaluated at import, so every process
gets a different one. The app runs `gunicorn -w 2`, so even a single container
holds two.

That is much worse than logged-out users. The Fernet key protecting every value
in `app_config` is `sha256(JWT_SECRET_KEY)` — cloud credentials, registry
passwords, API tokens, kubeconfigs. And `config_service._decrypt` catches
`InvalidToken` and returns the raw string, so that plain-text legacy rows still
read. A changed key therefore does not raise: every stored credential quietly
resolves to a base64 blob, and you find out from an authentication error deep
inside a cloud SDK.

Supply it as a platform secret. Back it up somewhere you can find in a year —
lose it and every credential must be re-entered by hand. See
[secrets-management.md](secrets-management.md#why-the-jwt-root-key-cannot-be-migrated)
for why the community edition cannot bootstrap it from a vault.

**3. Terraform state must live in a cloud bucket.** There is no persistent
volume. With a storage backend configured, state goes to
`terraform-state/<job_id>/` in S3 / Blob / GCS and a lost working directory is
rebuilt on demand. Without one, state stays on container-local disk — and the
next revision orphans every resource it was tracking, still running and still
billing, with nothing left that knows how to destroy them. Configure `/storage`
**before** the first provision, not after.

**4. Pick your hostnames before you enrol an agent.** The first enrolment code
minted on an install permanently pins the signing audience, and every agent
signature is checked against it afterwards. A platform-assigned name — an
`*.azurecontainerapps.io` default FQDN, a Cloud Run auto-URL — will change, and
changing it strands the fleet. Bind a custom domain first. See
[remote-agents.md](remote-agents/enrolment.md#the-signing-audience-is-pinned-by-that-first-code).

---

## The shape

The same on all three platforms:

```
                    ┌──────────────────────────────────────┐
   agents.…  ─────▶ │  ingress (platform-managed TLS)       │
   dash.…    ─────▶ │            │                          │
                    │      ┌─────▼─────┐   ┌─────────┐      │
                    │      │  gateway  │──▶│   app   │      │  one task/pod
                    │      │  (Caddy)  │   │ gunicorn│      │  ingress: yes
                    │      └───────────┘   └────┬────┘      │
                    └───────────────────────────┼───────────┘
                                                │
                    ┌───────────────────────────┼───────────┐
                    │            worker         │           │  separate deploy
                    │  python -m …jobs_worker ──┘           │  ingress: none
                    └───────────────────────────┬───────────┘
                                                ▼
                                     managed Postgres (private)
```

**One image, two deployments.** The worker is the same image with a different
command. It needs no ingress and no volume, but it does need the same database,
the same JWT key, and the same cloud credentials — it is what actually runs
every provision. Without it, jobs sit `pending` forever with nothing logged.
[job-worker.md](job-worker.md#deploying-the-worker) covers it in full; do not
skip the part about `minReplicas: 0` meaning *off* rather than *scale to zero*.

**The gateway is a sidecar, not a separate service.** Two reasons, and the
second is the one people get wrong:

- It splits the vhosts. `/api/agent/*` is served on one hostname and the UI on
  another, so the internet-facing surface is one prefix with no HTML, no login
  form, no session cookies, no OAuth callbacks and no `/setup`.
- It keeps `TRUSTED_PROXY_HOSTS` correct without configuring anything. The app
  believes `X-Forwarded-*` only from peers in that list, which defaults to
  `127.0.0.1`. A sidecar reaches the app over loopback, so the default is
  already right. A *separate* proxy service gets a platform-assigned address
  that changes on redeploy, and uvicorn 0.27 compares those as plain strings —
  no CIDR, no hostnames. See [SECURITY.md](../SECURITY.md#reverse-proxies-forwarded-headers-and-the-public-url).

A ready-made gateway image is in [`examples/cloud-gateway/`](../examples/cloud-gateway/Dockerfile):

```bash
docker build -t <registry>/dash-gateway:1 examples/cloud-gateway
docker push <registry>/dash-gateway:1
```

It takes `AGENT_HOSTNAME`, `UI_HOSTNAME`, optional `UI_HOSTNAME_ALT`, and
optional `UI_ALLOWED_CIDR` to restrict the UI to your own egress addresses.

### Environment, everywhere

| Variable | Value |
|---|---|
| `DATABASE_URL` | **secret.** `postgresql://user:pass@host:5432/db` |
| `JWT_SECRET_KEY` | **secret.** 64 hex chars; generate once, never rotate casually |
| `PUBLIC_BASE_URL` | `https://dash.example.com` — the origin users reach |
| `WEBAUTHN_RP_ID` | `dash.example.com` — bare domain, no scheme, no port |
| `WEBAUTHN_ORIGIN` | `https://dash.example.com` — must match exactly |
| `APP_ENV` | `production`. Cosmetic — it only colours the nav bar |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | env-only; the engine is built at import, before any config read |

`TRUSTED_PROXY_HOSTS` is deliberately absent: with the sidecar the default is
correct, and setting it wrong is worse than leaving it alone.

For a first deploy where you cannot open a browser to the wizard in time, set
`FIRST_RUN_ADMIN_USERNAME` and `FIRST_RUN_ADMIN_PASSWORD`. They create the admin
only when the users table is empty, so they are a no-op on every later boot.

### Probes

`GET /api/health` on container port 8000. It returns `{"status": "ok"}` without
touching the database — a liveness signal, not a readiness one. Give startup a
generous window: `init_db()` runs before the app serves anything, and on a cold
database it takes a Postgres advisory lock, creates every table and applies ~45
idempotent DDL statements. Concurrent starts queue on that lock rather than
racing, which is correct but is real wall-clock time.

**Set them.** Without probes ACA has nothing to fail, so it reports a revision
`Healthy` with `restartCount: 0` no matter what the app is doing — and an app whose
`lifespan` never reaches `yield` holds the listening socket open, so the ingress
accepts the connection and returns `504` only after its full ~240s timeout, on every
path including one that does not exist. That is a total outage that looks like a
network fault and never self-heals. A startup probe turns it into a restart.

The app container takes the HTTP probe; the sidecar must not.

| Container | Probe | Type | Period | Failures |
|---|---|---|---|---|
| `app` | Startup | `httpGet /api/health:8000` | 30s | 10 |
| `app` | Liveness | `httpGet /api/health:8000` | 30s | 3 |
| `app` | Readiness | `httpGet /api/health:8000` | 10s | 3 |
| `gateway` | Liveness | `tcpSocket:80` | 30s | 3 |
| `gateway` | Readiness | `tcpSocket:80` | 10s | 3 |

`failureThreshold` caps at 10, so the startup budget is `periodSeconds x 10` — 30s x 10
gives the five minutes the DDL run above wants.

**The sidecar gets a TCP probe, not an HTTP one.** The Caddyfile answers `404` for any
`Host` it does not recognise, and a probe request matches neither `AGENT_HOSTNAME` nor
`UI_HOSTNAME`. ACA counts a `404` as a failure, so an `httpGet` probe on the gateway
fails forever and restart-loops the sidecar. Accepting a connection on `:80` is the
strongest signal available without adding a health route to the image.

Readiness matters here even at one replica: it pulls the replica out of ingress, so a
broken app returns a fast `503` instead of that 240s hang.

`az containerapp create` and `update` have **no probe flags** — probes come from YAML or
an ARM PATCH. Prefer the PATCH, and send only `properties.template`: `az containerapp
show` renders every secret with `value: null`, so round-tripping its output through
`update --yaml` can blank `database-url` and `jwt-secret-key`. Omitting
`properties.configuration` leaves secrets, ingress and registries untouched.

```bash
# probes live in properties.template.containers[].probes
az rest --method PATCH --url "https://management.azure.com/subscriptions/<sub>/resourceGroups/RG-DASH/providers/Microsoft.App/containerApps/dash?api-version=2025-01-01" --body @patch.json
```

Use `api-version=2025-01-01` or later. `2024-03-01` rejects the `cooldownPeriod` and
`pollingInterval` fields that `az containerapp show` puts in `scale`, so a patch built
from its own output fails validation.

Changing a probe changes the template, which mints a new revision — i.e. a restart. Do
it deliberately, not during an incident.

**The worker is a separate Container App and needs its own probe.** It has no ingress, so
for a long time it had no port and could not be probed at all — it now serves
`/api/health` on `WORKER_HEALTH_PORT` (8080) for exactly this purpose, and reports
unhealthy until its run loop is turning. Probe config, and the reason you must not point a
probe at it before the deployed image contains the listener, are in
[job-worker.md](job-worker.md#health-endpoint).

### Replicas

**Login no longer constrains this.** OIDC/OAuth CSRF state and FIDO2 challenges
used to live in process memory, which made SSO and security-key logins fail
intermittently even across the image's own two gunicorn workers — the second leg
of a ceremony had to land on the process that started it, and nothing makes it.
That state is now a short-TTL database table, so it crosses workers and replicas.
Password login was never affected.

**One app replica is still the default,** but now for a cost reason rather than a
correctness one: cache warmers run per process, so extra replicas multiply
billable cloud API calls — Cost Explorer among them.

Live job output is not a constraint here. The job WebSocket is driven entirely by
the database — it replays persisted log lines on connect and tails new ones by
polling — so a browser reaches an in-flight job's output from any replica, not
only the one that started it.

**Run exactly one worker replica** unless you have raised the connection budget
to match. Sizing and the budget arithmetic are in
[job-worker.md](job-worker.md#budgeting-connections).

---

## Azure Container Apps

This is what the reference install runs, so everything below is verified against
a live deployment rather than derived.

### Postgres

```bash
az postgres flexible-server create \
  --resource-group RG-DASH --name pg-dash --location eastus2 \
  --tier Burstable --sku-name Standard_B1ms --version 16 \
  --storage-size 32 --public-access Disabled \
  --vnet vnet-dash --subnet snet-pg
```

Public access disabled means the Container Apps environment must be VNet-joined
to reach it. B1ms allows only **50 connections**, which is why the shipped
`DB_POOL_SIZE`/`DB_MAX_OVERFLOW` defaults are a conservative 5 + 5 — the app is
two gunicorn processes and the worker one, so a deployment holds `3 × (size +
overflow)` = 30. B2s and every General Purpose tier allow 429 or more.

### Environment

```bash
az containerapp env create \
  --name cae-dash --resource-group RG-DASH --location eastus2 \
  --infrastructure-subnet-resource-id "$(az network vnet subnet show \
      -g RG-DASH --vnet-name vnet-dash -n snet-aca --query id -o tsv)"
```

The subnet must be delegated to `Microsoft.App/environments`. Leave the
environment external (the default) — VNet-joining is about reaching the private
database on the way *out*, not about hiding ingress.

### The app, with its gateway sidecar

Create the app first, then add the sidecar — `az containerapp create` takes one
container, and a second is a template edit.

Create it pointing ingress at **8000**, the app's own port, so you have a
working dashboard to check before adding anything. Moving ingress to the
gateway's port 80 is part of the same update that adds the gateway; do it
earlier and ingress points at a port nothing is listening on.

```bash
az containerapp create \
  --name dash --resource-group RG-DASH --environment cae-dash \
  --image chrweav/infra-dashboard:latest \
  --target-port 8000 --ingress external \
  --cpu 1 --memory 2Gi --min-replicas 1 --max-replicas 1 \
  --secrets database-url="postgresql://..." jwt-secret-key="$(openssl rand -hex 32)" \
  --env-vars \
      DATABASE_URL=secretref:database-url \
      JWT_SECRET_KEY=secretref:jwt-secret-key \
      PUBLIC_BASE_URL=https://dash.example.com \
      WEBAUTHN_RP_ID=dash.example.com \
      WEBAUTHN_ORIGIN=https://dash.example.com \
      CORS_ORIGINS=https://dash.example.com \
      APP_ENV=production
```

Then add the sidecar. There is no CLI flag for a second container, so this is a
template edit: export the current YAML, add an entry under
`properties.template.containers`, and apply it back.

```yaml
- name: gateway
  image: <registry>/dash-gateway:1
  resources: { cpu: 0.25, memory: 0.5Gi }
  env:
    - name: AGENT_HOSTNAME
      value: agents.example.com
    - name: UI_HOSTNAME
      value: dash.example.com
```

```bash
az containerapp show   -n dash -g RG-DASH -o yaml > dash.yaml
# …add the gateway container to properties.template.containers…
az containerapp update -n dash -g RG-DASH --yaml dash.yaml
az containerapp ingress update -n dash -g RG-DASH --target-port 80
```

After the port move, the app is reachable only through the gateway — which
means only on the two hostnames it knows about. The environment's default
`*.azurecontainerapps.io` name will start returning 404, and that is the design
working, not a fault.

### Custom domains

```bash
az containerapp hostname add    -n dash -g RG-DASH --hostname dash.example.com
az containerapp hostname bind   -n dash -g RG-DASH --hostname dash.example.com \
                                --validation-method CNAME
```

Repeat for the agent hostname. Two things worth knowing: bind both names
*before* enrolling any agent, and **a managed certificate often reports a bind
error and then succeeds anyway** — wait and re-check before you start
troubleshooting a problem you do not have.

### The worker

```bash
az containerapp create \
  --name dash-worker --resource-group RG-DASH --environment cae-dash \
  --image chrweav/infra-dashboard:latest \
  --command python --args "-m" "web_dashboard.jobs_worker" \
  --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 1 \
  --secrets database-url="postgresql://..." jwt-secret-key="<same key>" \
  --env-vars DATABASE_URL=secretref:database-url \
             JWT_SECRET_KEY=secretref:jwt-secret-key \
             PUBLIC_BASE_URL=https://dash.example.com APP_ENV=production
```

Full detail, including why the worker is its own Container App rather than
another sidecar: [job-worker.md](job-worker.md#container-apps).

### No PAT: authenticate to Pathfinder with an Entra workload identity

> **Applies to:** an install using [Workload Credentials](integrations/workload-credentials.md).
> Skip this if you do not. **Status:** the client path ships and is unit-tested;
> the Azure and Pathfinder steps below have not yet been run end to end on this
> install, so treat the commands as a starting point rather than a transcript.

Workload Credentials replaced three standing cloud keys with **one** standing
platform credential: a Personal Access Token, stored encrypted in `app_config`.
That token is the last secret this feature holds, and it is the one that has
actually caused an outage here — a dead PAT produces
`401 Personal access token not found` on every mint, from a dashboard that looks
entirely healthy.

On Azure that token can go away. Container Apps can hand the container a
**managed identity**; Pathfinder can be told, once, to trust that identity. The
container then presents a short-lived Entra token it fetched from the platform a
moment earlier, and there is **no BeyondTrust credential anywhere in the
deployment** — nothing in `app_config`, nothing in the Container App's secrets,
nothing to rotate.

**Registration is a GUI action and there is no API for it.** Pathfinder →
**Administration → Workload Identities → Register Workload Identity**. That is
deliberate on both sides: a trust that can be created by the thing being trusted
is not a trust. It takes a few minutes, once.

Pathfinder registers three kinds of issuer. Only the second applies to this
container:

| Identity Provider Type | What it is for |
|---|---|
| **GitHub Actions** | a workflow pulling secrets in CI — pinned to `owner/repo` and, optionally, immutable org/repo **IDs** so a renamed or recreated repo stops matching |
| **Azure Entra ID** | anything holding an Entra identity, including this Container App |
| **Custom IDP** | any other OIDC issuer, scoped by explicit claim conditions — the escape hatch, and the one to reach for below if the issuer version fights you |

#### 1. One user-assigned identity, on both container apps

Use a **user-assigned** identity rather than a system-assigned one. The app and
the worker are two separate Container Apps, so system-assigned means two
principals, two object ids, and two registrations in Pathfinder to keep in step.
And **the worker is where credentials are actually minted** — an identity on
`dash` alone gives you a panel that tests green and jobs that still fail.

```bash
az identity create -g RG-DASH -n id-dash-wlc
ID=$(az identity show -g RG-DASH -n id-dash-wlc --query id -o tsv)
az containerapp identity assign -n dash        -g RG-DASH --user-assigned "$ID"
az containerapp identity assign -n dash-worker -g RG-DASH --user-assigned "$ID"
```

Then read back the two values Pathfinder and the settings panel each need:

```bash
az identity show -g RG-DASH -n id-dash-wlc \
  --query '{principalId:principalId, clientId:clientId, tenantId:tenantId}' -o json
```

`principalId` is the **service principal object ID** — the value Pathfinder asks
for, and the one that ends up as the `sub` claim it checks. `clientId` is what
the dashboard sends when asking the platform for a token; without it the request
resolves to the *system-assigned* identity, which here is not assigned at all.

#### 2. An audience for the token, and the issuer trap behind it

A managed identity does not mint a token in the abstract — it mints one **for a
resource**, and that resource becomes the token's `aud`. Register an application
in your own tenant purely to be that audience:

```bash
APP_ID=$(az ad app create --display-name vm-dashboard-wlc \
           --identifier-uris api://vm-dashboard-wlc --query appId -o tsv)
az ad app update --id "$APP_ID" --set api.requestedAccessTokenVersion=2
az ad sp create --id "$APP_ID"      # so the tenant can resolve api://…
```

**That second command is the whole trap, so do not skip it.** Entra issues *two*
token versions with *two different issuers*:

| Token version | `iss` |
|---|---|
| v1 (the default) | `https://sts.windows.net/<tenant-id>/` |
| v2 (`requestedAccessTokenVersion: 2`) | `https://login.microsoftonline.com/<tenant-id>/v2.0` |

The **Azure Entra ID** registration type expects the v2 form — it is what the
field's own placeholder shows. Leave the resource app on the default and the
identity happily produces a perfectly valid token that Pathfinder will never
match, with nothing in either UI to say why. In the portal's manifest the same
property reads `accessTokenAcceptedVersion`; via Graph and the CLI it is
`requestedAccessTokenVersion`. They are the same knob.

If you cannot change the resource app — someone else owns it — register as
**Custom IDP** instead, with the issuer set to `https://sts.windows.net/<tenant-id>/`
and one claim condition, `sub` = the identity's `principalId`. That path is
exactly why Custom IDP exists.

#### 3. Register the trust in Pathfinder

**Administration → Workload Identities → Register Workload Identity**:

| Field | Value |
|---|---|
| Identity Provider Type | **Azure Entra ID** |
| Service Name | `vm-dashboard` — any name; the dashboard sends it back as `X-BT-Service-Name` |
| Issuer URL | `https://login.microsoftonline.com/<tenant-id>/v2.0` |
| Service Principal OID | the identity's `principalId` from step 1 |
| Site | **the site whose Workload Credentials you use** |

That last row carries the same trap a PAT does: a registration made against the
wrong site fails every call with `401 Access denied for this site`, which reads
like the site is missing the application rather than like a scoping mistake.

#### 4. Point the dashboard at it

**Settings → Integrations → Workload Credentials**:

- **Authentication** → *Azure workload identity (nothing stored)*
- **Service name** → the Service Name from step 3
- **Token resource** → `api://vm-dashboard-wlc`
- **User-assigned identity client ID** → `clientId` from step 1

**Save, then press Test connection** — it tests the *saved* values. The test is
an unmetered `GET /session`, so it is free and safe to repeat, and it is the only
thing that separates the two failures cleanly:

| What you see | What it means |
|---|---|
| `no managed identity endpoint reachable` | no identity is assigned to **this** container app |
| `could not get a managed identity token (HTTP 400/404)` | the identity exists but the tenant will not issue for that resource — check the App ID URI and that the service principal in step 2 was created |
| `Workload Credentials error (HTTP 401)` | the token was fine and Pathfinder declined it — wrong site, wrong Service Name, a `sub` that is not this identity's `principalId`, or the v1/v2 issuer mismatch above |

Once it passes, clear `wlc_pat`: switching modes does not delete it, and a stored
token nobody uses is still a credential somebody has to answer for. The Workload
Lab's Cloud tab mints through the same client, so it stops depending on a stored
token at the same moment.

**What this does not remove.** `DATABASE_URL` and `JWT_SECRET_KEY` are still
platform secrets on both container apps, and they still have to match between
`dash` and `dash-worker`. This removes the BeyondTrust credential, not every
credential.

---

## GCP Cloud Run

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

---

## AWS ECS Fargate

> As with Cloud Run: the same architecture, not yet a transcript.

**Use ECS, not App Runner.** App Runner runs a single container per service, so
there is nowhere to put the gateway — you would need CloudFront or ALB rules to
reproduce the vhost split, and `TRUSTED_PROXY_HOSTS` would have to name an ALB
address that is not stable. An ECS task takes multiple containers and keeps the
sidecar property.

- **One task definition, two containers** — `gateway` (portMapping 80) and
  `app`. In `awsvpc` mode they share a network namespace, so the gateway's
  `reverse_proxy localhost:8000` reaches the app exactly as it does elsewhere.
- **A second task definition for the worker**, no port mappings, its own service
  at `desiredCount: 1`.
- **ALB in front**, target group on the gateway's port 80, HTTPS listener with
  an ACM certificate. Both hostnames point at the same listener; the gateway
  does the splitting, not the listener rules.
- **RDS Postgres** in private subnets; security group allows 5432 only from the
  task security group.
- **Secrets Manager** via the task definition's `secrets` block with `valueFrom`,
  which needs `secretsmanager:GetSecretValue` on the *execution* role.
- **Private subnets with a NAT gateway.** The tasks need outbound reach to every
  cloud API they manage, plus your registry.

---

## Troubleshooting

**Everything looks configured but every cloud call fails to authenticate.** The
JWT key changed, so `app_config` decrypts to ciphertext. Check whether the
platform regenerated the secret, or whether it was ever set. Re-enter the
credentials after fixing the key — see
[config-migration.md](config-migration.md) if you have another instance to
copy from.

**Jobs sit `pending` and nothing is logged anywhere.** No worker is running.
On Container Apps the usual cause is `minReplicas: 0` on a service with no
ingress, which is off rather than idle.

**SSO or security-key login fails intermittently with `?error=invalid_state`.**
The ceremony landed on a different process than the one that started it. Reduce
to one replica; it will still misfire occasionally across the image's two
gunicorn workers.

**A provision succeeded but the resource cannot be destroyed.** Terraform state
was on container-local disk and the container was replaced. Configure a cloud
storage backend on `/storage` and treat the orphan as a manual cleanup.

**Agents 401 with what looks like a revoked key.** The signing audience does not
match the origin they are calling. It was pinned by the first enrolment code
ever minted and is not overwritten by later configuration.

**The UI is unreachable but the agent endpoint works.** `UI_ALLOWED_CIDR` does
not contain your current address. It gates only the UI vhost, by design.
