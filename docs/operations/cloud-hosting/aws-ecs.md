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
