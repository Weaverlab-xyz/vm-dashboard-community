# SPIRE server sidecar image for managed container runtimes — the dashboard's own trust
# domain on Azure Container Apps, Cloud Run, ECS/Fargate or a Kubernetes pod.
#
# The compose overlay (docker-compose.spire.yml) runs the upstream image directly with a
# config file mounted in. A managed runtime has no convenient file mount and no disk that
# survives a restart, so this image bakes in a config that keeps its state in PostgreSQL
# and its trust anchor in an upstream CA you supply as a secret (server.conf), and an
# entrypoint that turns environment variables into the files SPIRE reads (entrypoint.sh).
#
# The upstream image is distroless and has no shell, so the binary — statically linked —
# is copied onto busybox, which gives the entrypoint sh, setuidgid and wget (for an ECS
# health check). Both images are pinned by digest, the SPIRE one from the same table as
# the compose overlay and the SPIRE lab; tests/test_spire_overlay.py keeps them in step.
#
# Build:  docker build -t <registry>/dash-spire:1 examples/cloud-spire
# Guide:  docs/spiffe.md#on-a-managed-container-runtime
FROM ghcr.io/spiffe/spire-server:1.15.3@sha256:4082f30d3e0ddc4000a171392c4ea174345ee44d161ee917c70b97b2ecfba141 AS spire

FROM busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e

COPY --from=spire /opt/spire/bin/spire-server /usr/local/bin/spire-server
COPY server.conf /etc/spire/server.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# The dashboard image's uid/gid (10001). The admin socket is 0770, so the server and the
# app container that drives it must share one.
RUN echo "spire:x:10001:10001:spire:/nonexistent:/bin/false" >> /etc/passwd \
    && echo "spire:x:10001:" >> /etc/group \
    && mkdir -p /run/spire-server /run/spire-ca /var/lib/spire \
    && chown 10001:10001 /run/spire-server /run/spire-ca /var/lib/spire \
    && chmod 0750 /run/spire-server && chmod 0700 /run/spire-ca /var/lib/spire \
    && chmod 0755 /usr/local/bin/entrypoint.sh \
    && /usr/local/bin/spire-server --version

# 8081: agent API (mTLS gRPC — TCP passthrough only, never through an HTTP proxy).
# 8082: federation bundle endpoint (TLS with the server's own SVID — also passthrough).
# 8088: /live and /ready for the platform's probes. Not published.
EXPOSE 8081 8082 8088

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
