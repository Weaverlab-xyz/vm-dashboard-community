# The remote agent's updater (see updater.py). Beside the agent, holding the Docker socket
# so the agent does not have to: it replaces the agent container with a newer SIGNED image,
# or restarts it, and rolls back if the new one does not come up.
#
# Same base as the agent, pinned by digest. cosign is a release binary pinned by sha256 —
# the tool that decides whether an image may run is not something to fetch by tag.
FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e

# zlib1g: Debian security update DSA-6613-1 (CVE-2026-76844, CVE-2026-85091, both
# HIGH), fixed in trixie-security as 1:1.3.dfsg+really1.3.1-1+deb13u1. The pinned base
# still ships the vulnerable 1:1.3.dfsg+really1.3.1-1+b1 and so fails the Trivy gate in
# scripts/ci/trivy_image.sh. Bumping the digest does not help — the tag's current image
# is built from the same Debian snapshot; see the root Dockerfile for the full note.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH=amd64
ARG COSIGN_VERSION=v2.4.1
ARG COSIGN_SHA256_amd64=8b24b946dd5809c6bd93de08033bcf6bc0ed7d336b7785787c080f574b89249b
ARG COSIGN_SHA256_arm64=3b2e2e3854d0356c45fe6607047526ccd04742d20bd44afb5be91fa2a6e7cb4a

RUN pip install --no-cache-dir "PyYAML>=6.0" \
 && python -c "import hashlib,os,urllib.request; \
a='${TARGETARCH}'; want={'amd64':'${COSIGN_SHA256_amd64}','arm64':'${COSIGN_SHA256_arm64}'}[a]; \
d=urllib.request.urlopen('https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-'+a, timeout=120).read(); \
got=hashlib.sha256(d).hexdigest(); \
assert got==want, 'cosign checksum mismatch: '+got; \
open('/usr/local/bin/cosign','wb').write(d); os.chmod('/usr/local/bin/cosign',0o755)"

COPY updater.py /app/updater.py

# The agent's uid, so both write the shared control volume. The Docker socket's group is
# added at run time (group_add), never baked in: it differs per host.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin updater \
 && mkdir -p /var/lib/dashboard-agent-control && chown 10001:10001 /var/lib/dashboard-agent-control

USER 10001:10001
ENV AGENT_CONTROL_DIR=/var/lib/dashboard-agent-control \
    AGENT_POLICY_FILE=/etc/dashboard-agent/policy.yaml \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# No EXPOSE: this container never listens on anything and never calls the dashboard.
ENTRYPOINT ["python", "/app/updater.py"]
