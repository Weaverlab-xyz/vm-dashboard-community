# The SPIRE sidecar on Azure Container Apps

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are running the dashboard's SPIRE server as a sidecar on Azure Container Apps.

Part of [Cloud Hosting: Azure Container Apps](../azure-container-apps.md).

Only if agents are to [attest through SPIRE](../../spiffe.md#on-a-managed-container-runtime),
or the dashboard is to have [its own SPIFFE identity](../../oidc/dashboard-identity.md).
The SPIRE server becomes a third container in `dash`, beside `gateway` and `app`. **The
worker changes in no way except its image:** it reaches the server through the database.

Before you start:

- **Both apps on an image from this release or later.** The app needs the `spire-server`
  CLI it carries, and the worker needs the relay.
- **The sidecar image in your registry**:
  `az acr build -r <acr> -t dash-spire:1 examples/cloud-spire`.
- **A `spire` database** on the Postgres server: `CREATE DATABASE spire;` as the admin
  user.
- **The upstream CA**, generated as in
  [The upstream CA](../../spiffe.md#the-upstream-ca).

**1. Secrets.** `secret set` adds or updates only the names it is given:

```bash
az containerapp secret set -n dash -g RG-DASH --secrets "spire-datastore-url=postgresql://<user>:<password>@<server>.postgres.database.azure.com:5432/spire?sslmode=require" "spire-ca-cert=$(cat spire-ca.crt)" "spire-ca-key=$(cat spire-ca.key)"
```

**2. The template.** Export it, change it, apply it. The same trap as
[Probes](../../cloud-hosting.md#probes): `az containerapp show` returns the `secrets` block as
names with no values, so **delete `properties.configuration.secrets` from the file before
you apply it**, or the update blanks `database-url` and `jwt-secret-key`. Keep the full
container list: it is a list, so a partial one drops the containers you left out.

```bash
az containerapp show -n dash -g RG-DASH -o yaml > dash.yaml
```

Add the volume, the sidecar, and two lines on `app`:

```yaml
properties:
  configuration:
    ingress:
      # Agents' mTLS gRPC and the federation endpoint: TCP passthrough, never through
      # the gateway. TCP ports need a VNet-integrated environment, and are per
      # environment: each exposed port is usable by one app only.
      additionalPortMappings:
        - { external: true, targetPort: 8081, exposedPort: 8081 }
        - { external: true, targetPort: 8082, exposedPort: 8082 }
  template:
    volumes:
      - name: spire-admin
        storageType: EmptyDir
    containers:
      - name: app
        # …everything it already has, plus:
        env:
          - name: SPIRE_SERVER_SOCKET
            value: /run/spire-server/api.sock
        volumeMounts:
          - volumeName: spire-admin
            mountPath: /run/spire-server
      - name: spire
        image: <acr>.azurecr.io/dash-spire:1
        resources: { cpu: 0.25, memory: 0.5Gi }
        env:
          - { name: SPIRE_TRUST_DOMAIN, value: dashboard.example.com }
          - { name: SPIRE_DATASTORE_URL, secretRef: spire-datastore-url }
          - { name: SPIRE_UPSTREAM_CA_CERT, secretRef: spire-ca-cert }
          - { name: SPIRE_UPSTREAM_CA_KEY, secretRef: spire-ca-key }
        volumeMounts:
          - volumeName: spire-admin
            mountPath: /run/spire-server
        probes:
          - { type: Startup, httpGet: { path: /ready, port: 8088 }, periodSeconds: 10, failureThreshold: 10 }
          - { type: Liveness, httpGet: { path: /live, port: 8088 }, periodSeconds: 30 }
          - { type: Readiness, httpGet: { path: /ready, port: 8088 }, periodSeconds: 10 }
```

```bash
az containerapp update -n dash -g RG-DASH --yaml dash.yaml
az containerapp secret list -n dash -g RG-DASH --query "[].name"
```

The second command checks that every secret name is still there. An authenticated page in
the UI is what proves the JWT key survived. The sidecar's probes are HTTP, unlike the
gateway's, because 8088 does no host matching. Add `SPIRE_JWT_ISSUER` to the sidecar's
`env` too if you will use the dashboard's own identity.

**3. Check it.** The sidecar's log should end with the server serving, and the app's log
should show `SPIRE relay: serving relayed calls over socket` once a SPIRE setting is on:

```bash
az containerapp logs show -n dash -g RG-DASH --container spire --tail 50
```

**4. The dashboard.** Under **Settings → Integrations → Remote Agents**, tick
**Let agents attest through SPIRE**. Under **Settings → Integrations → Platform → SPIFFE and SPIRE**, leave
**SPIRE server address for agents** blank: the
agent hostname's custom domain resolves to the environment's address, and that address
carries the TCP ports too. From an agent host, `nc -vz agents.example.com 8081` should
connect. Then migrate agents as on [the guide](../../spiffe/remote-agents/spire-attestation.md#setting-it-up).

`UI_ALLOWED_CIDR` does not cover these ports: it is the gateway's, and only for the UI
hostname. Restrict 8081 and 8082 at the network level if agents come from known addresses.
