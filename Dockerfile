# btapi (BeyondTrust PRA CLI) is distributed as an x86_64-only ELF binary.
# Packer is also downloaded as linux_amd64 below. Pin to amd64 so both
# binaries work under Rosetta on Apple Silicon and run natively on x86_64.
FROM --platform=linux/amd64 python:3.12-slim AS base

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

# Install Packer (architecture-aware) and pre-cache all three cloud plugins
# so packer init does not require internet access at build time.
ARG TERRAFORM_VERSION=1.9.5
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_${ARCH}.zip" \
        -o /tmp/packer.zip \
    && unzip -q /tmp/packer.zip -d /usr/local/bin/ \
    && rm /tmp/packer.zip \
    && packer plugins install github.com/hashicorp/amazon \
    && packer plugins install github.com/hashicorp/azure \
    && packer plugins install github.com/hashicorp/googlecompute

# Install Terraform (architecture-aware) and pre-cache the BeyondTrust SRA
# provider so containers have no outbound dependency at run time.
# The plugin cache directory is set via TF_PLUGIN_CACHE_DIR in terraform_pra_service.py.
ENV TF_PLUGIN_CACHE_DIR=/root/.terraform.d/plugin-cache
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip" \
        -o /tmp/terraform.zip \
    && unzip -q /tmp/terraform.zip -d /usr/local/bin/ \
    && rm /tmp/terraform.zip \
    && mkdir -p "${TF_PLUGIN_CACHE_DIR}" \
    && mkdir -p /tmp/tf_provider_init \
    && printf 'terraform {\n  required_providers {\n    sra = { source = "beyondtrust/sra", version = "~> 1.0" }\n  }\n}\n' \
       > /tmp/tf_provider_init/main.tf \
    && terraform -chdir=/tmp/tf_provider_init init \
    && rm -rf /tmp/tf_provider_init

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
