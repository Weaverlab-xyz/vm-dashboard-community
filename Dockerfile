FROM python:3.12-slim AS base

ARG PACKER_VERSION=1.11.2

WORKDIR /app

# Install Python dependencies first so this layer caches when only app
# code changes.
COPY web_dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install btapi (BeyondTrust PRA CLI) Linux binary — required for the
# optional PRA Jump Group / Shell Jump integration in btapi_service.py.
COPY btapi/btapi /usr/local/bin/btapi
RUN chmod +x /usr/local/bin/btapi

# Copy the application.
COPY web_dashboard/ ./web_dashboard/

# Container-sane defaults; .env overrides these at runtime.
ENV LOG_DIR=/tmp/logs \
    WEBAUTHN_RP_ID=localhost \
    WEBAUTHN_ORIGIN=http://localhost:8000

EXPOSE 8000

# openssh-client: optional VMware-Workstation integration (see
#   docker-compose.override.windows.yml.example) SSHes from the container
#   to the Windows host to run the PowerShell wrapper.
# docker-ce-cli: optional Ansible integration runs config-mgmt jobs in
#   sibling containers via the mounted Docker socket.
# unzip: needed to extract the Packer binary archive.
# All are included by default so the same image works whether the user
# opts in to VMware / Ansible / Packer after first boot.
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        ca-certificates \
        curl \
        unzip \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install Packer and pre-cache all three cloud plugins so packer init
# does not require internet access at build time inside the container.
RUN curl -fsSL "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_amd64.zip" \
        -o /tmp/packer.zip \
    && unzip -q /tmp/packer.zip -d /usr/local/bin/ \
    && rm /tmp/packer.zip \
    && packer plugins install github.com/hashicorp/amazon \
    && packer plugins install github.com/hashicorp/azure \
    && packer plugins install github.com/hashicorp/googlecompute

# Entrypoint fixes SSH key permissions when the Windows override
# bind-mounts a key from %USERPROFILE%. Docker Desktop surfaces Windows
# files as mode 0777 and sshd-client refuses keys that world-readable,
# so copy to a private path before invoking gunicorn.
RUN printf '#!/bin/sh\nif [ -f /root/.ssh/dev_dashboard_key ]; then\n    install -m 600 /root/.ssh/dev_dashboard_key /root/.ssh/dev_key\nfi\nexec "$@"\n' \
    > /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

CMD ["gunicorn", \
     "-w", "2", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "web_dashboard.main:app"]
