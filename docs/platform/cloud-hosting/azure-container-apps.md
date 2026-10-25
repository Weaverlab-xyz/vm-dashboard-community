# Cloud Hosting: Azure Container Apps

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are running the dashboard on Azure Container Apps: Postgres, the app with its gateway sidecar, the worker, custom domains, outbound addresses, and workload identity instead of a PAT.

Part of [Cloud Hosting](../cloud-hosting.md).

This is what the reference install runs, so everything below is verified against
a live deployment rather than derived.

## Postgres

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

## Environment

```bash
az containerapp env create \
  --name cae-dash --resource-group RG-DASH --location eastus2 \
  --infrastructure-subnet-resource-id "$(az network vnet subnet show \
      -g RG-DASH --vnet-name vnet-dash -n snet-aca --query id -o tsv)"
```

The subnet must be delegated to `Microsoft.App/environments`. Leave the
environment external (the default) — VNet-joining is about reaching the private
database on the way *out*, not about hiding ingress.

## The app, with its gateway sidecar

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

**`PUBLIC_BASE_URL` with two hostnames.** It is the remote-agent signing audience,
and enrolment codes embed it — so once agents enrol on `agents.example.com`, set it
to that host. It used to double as the SSO callback origin, which then pointed the
IdP at a hostname the gateway 404s for everything but `/api/agent/*`. The SSO
callbacks now come from `UI_BASE_URL`, else `WEBAUTHN_ORIGIN` (already
`https://dash.example.com` above), so nothing more is needed as long as
`WEBAUTHN_ORIGIN` is the UI host. Set `UI_BASE_URL=https://dash.example.com`
explicitly if it is not. Register `https://dash.example.com/api/auth/oauth/{azure,oidc}/callback`
with the IdP — see [OIDC Step 1](../oidc.md#step-1--register-an-application-with-your-idp).

## Custom domains

```bash
az containerapp hostname add    -n dash -g RG-DASH --hostname dash.example.com
az containerapp hostname bind   -n dash -g RG-DASH --hostname dash.example.com \
                                --validation-method CNAME
```

Repeat for the agent hostname. Two things worth knowing: bind both names
*before* enrolling any agent, and **a managed certificate often reports a bind
error and then succeeds anyway** — wait and re-check before you start
troubleshooting a problem you do not have.

## The worker

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
another sidecar: [job-worker.md](../../operations/job-worker.md#container-apps).

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [The SPIRE sidecar on Azure Container Apps](azure-container-apps/spire-sidecar.md) | you are running the dashboard's SPIRE server as a sidecar on Azure Container Apps |
| [Outbound addresses on Azure Container Apps](azure-container-apps/outbound-addresses.md) | managed nodes' firewalls need the dashboard's outbound addresses, and you are choosing how to give them |
| [Pathfinder without a PAT on Azure Container Apps](azure-container-apps/workload-identity.md) | you want the dashboard on Azure Container Apps to reach BeyondTrust Pathfinder with a managed identity instead of a stored token |

