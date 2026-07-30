# Remote on-prem agent — a long-lived supervisor that runs inside a customer's
# private network, polls the dashboard for work, and reports back.
#
# Deliberately NOT built FROM chrweav/ansible-cloud. That is the *payload* image, and
# merging a persistent process with a code-execution runtime is exactly what
# docs/config-management.md argues against. This image carries no ansible, no kubectl,
# no helm and no container client, so it is structurally incapable of running
# operator-supplied code — a property tests/test_agent_runner_contract.py asserts
# rather than trusts. When agent-executed Ansible lands it will spawn a one-shot
# sibling container; the supervisor itself stays inert.
FROM python:3.12-slim

# Three dependencies, all auditable:
#   requests      — the HTTPS client (trust_env picks up corporate proxy + CA settings)
#   PyYAML        — reads policy.yaml and any mounted kubeconfig
#   cryptography  — Ed25519 request signing and reading a probed TLS certificate
# The database and Kubernetes probes are stdlib sockets: no driver can be tricked into
# completing a handshake that a hand-written pre-auth probe cannot.
RUN pip install --no-cache-dir \
        "requests>=2.31.0" \
        "PyYAML>=6.0" \
        "cryptography>=42.0"

COPY agent.py /app/agent.py

# A fixed high UID with no shell and no home. The state directory is created here so
# the container works with a read-only root filesystem and a mounted volume; 0700
# because it holds the agent's Ed25519 private key.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin agent \
 && mkdir -p /var/lib/dashboard-agent /etc/dashboard-agent \
 && chown -R 10001:10001 /var/lib/dashboard-agent \
 && chmod 700 /var/lib/dashboard-agent

USER 10001:10001

ENV AGENT_STATE_DIR=/var/lib/dashboard-agent \
    AGENT_POLICY_FILE=/etc/dashboard-agent/policy.yaml \
    PYTHONUNBUFFERED=1

# No EXPOSE and no HEALTHCHECK that opens a port: this container never listens on
# anything. Liveness is visible from the other end — the dashboard's Agents page shows
# the agent offline within ~90s of it stopping polling.

ENTRYPOINT ["python", "/app/agent.py"]
