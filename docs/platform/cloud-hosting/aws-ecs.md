# Cloud Hosting: AWS ECS Fargate

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are running the dashboard on AWS ECS Fargate.

Part of [Cloud Hosting](../cloud-hosting.md).

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

## SPIRE sidecar

For [SPIRE attestation](../spiffe.md#on-a-managed-container-runtime)
or [the dashboard's own identity](../oidc/dashboard-identity.md). The dashboard's own
identity matters more here than anywhere else: an ECS task gets no OIDC token from its
platform, so it is the way to drop stored cloud keys.

- **A third container in the app's task definition**, from
  `docker build -t <registry>/dash-spire:1 examples/cloud-spire`. Give it portMappings
  8081 and 8082, the four variables from
  [The sidecar](../spiffe.md#the-sidecar) (three of them
  through `secrets` / `valueFrom`), and a health check of
  `["CMD", "wget", "-q", "-O-", "http://127.0.0.1:8088/ready"]`.
- **A task-level volume with no host**, `{"name": "spire-admin"}`, mounted at
  `/run/spire-server` in both the sidecar and `app`. Fargate creates it root-owned,
  which is why the sidecar starts as root to take ownership of it.
- **`SPIRE_SERVER_SOCKET=/run/spire-server/api.sock`** on `app`.
- **A Network Load Balancer, not the ALB**, with TCP listeners on 8081 and 8082 forwarding
  to an `ip` target group on the sidecar's ports. An ALB terminates TLS, and the agent's
  mTLS has to reach the server untouched. Allow those ports from the agent networks in the
  task security group.
- **SPIRE server address for agents** (`spire_server_address`) set to the NLB's DNS name,
  or a CNAME you give it. The agent gateway's hostname points at the ALB, and the ALB does
  not carry 8081.
- **`CREATE DATABASE spire;`** on the RDS instance, for `SPIRE_DATASTORE_URL`.

The worker's task definition does not change: it reaches the server through the database.
