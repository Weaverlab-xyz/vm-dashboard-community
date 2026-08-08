# Gateway sidecar image — Caddy plus the Caddyfile next to this file.
#
# Baking the config in rather than mounting it is deliberate: the managed
# runtimes this is built for (Azure Container Apps, Cloud Run, ECS/Fargate) have
# no convenient per-task file mount, and a config that ships with the image is a
# config that cannot drift between revisions.
#
# Everything that varies between deployments is an environment variable, so one
# image serves every environment:
#
#   AGENT_HOSTNAME    hostname serving /api/agent/* only            (required)
#   UI_HOSTNAME       hostname serving the dashboard UI            (required)
#   UI_HOSTNAME_ALT   a second UI hostname, e.g. during a cutover  (optional)
#   UI_ALLOWED_CIDR   space-separated CIDRs allowed to reach the UI
#                     (optional; defaults to allowing every address)
#
# Build:  docker build -t <registry>/dash-gateway:1 examples/cloud-gateway
FROM caddy:2-alpine

COPY Caddyfile /etc/caddy/Caddyfile

# The platform ingress terminates TLS and forwards plain HTTP here, so there is
# no 443 to publish and no ACME challenge to answer on 80.
EXPOSE 80

# Fail the build rather than the deploy if the config does not parse. A bad
# Caddyfile in a sidecar surfaces as the whole app failing to start with the
# error buried in a container that is not the one you are looking at.
RUN caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
