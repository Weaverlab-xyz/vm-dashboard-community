# Terraform, Packer, and all other tools are downloaded as architecture-aware
# binaries (see RUN steps below), so the image builds and runs natively on
# both ARM64 (Apple Silicon, AWS Graviton) and AMD64.
FROM python:3.12-slim AS base

ARG PACKER_VERSION=1.11.2

# Escape hatch for networks whose proxy still mangles trixie-updates /
# trixie-security Packages files even with the corp CA installed and apt
# switched to HTTPS. Set to 1 via `--build-arg BUILD_SKIP_DEBIAN_UPDATES=1`
# (or the ONBOARD_SKIP_DEBIAN_UPDATES=1 env var picked up by scripts/onboard.sh)
# to drop those two repos for the build. The image loses point-in-time
# security patches; rebuild after the proxy issue clears.
ARG BUILD_SKIP_DEBIAN_UPDATES=0

WORKDIR /app

# Optional corporate proxy root CA(s). Drop .crt/.pem files into corp-ca/ at
# the repo root if your network uses a TLS-inspecting proxy (Cloudflare
# Gateway, Zscaler, etc.). Without this, apt/curl/pip inside the build fail
# with "x509: certificate signed by unknown authority".
COPY corp-ca/ /usr/local/share/ca-certificates/corp-ca/
RUN set -e; \
    found=0; \
    for f in /usr/local/share/ca-certificates/corp-ca/*.pem; do \
        [ -e "$f" ] || continue; \
        mv "$f" "${f%.pem}.crt"; \
    done; \
    for f in /usr/local/share/ca-certificates/corp-ca/*.crt; do \
        [ -e "$f" ] && found=1 && break; \
    done; \
    if [ "$found" = "1" ]; then \
        update-ca-certificates; \
        # Switch apt to HTTPS. TLS-inspecting proxies MITM cleanly with the \
        # corp CA we just installed, but can mangle plaintext apt bodies \
        # (truncated responses, HTML block pages substituted for Packages). \
        find /etc/apt/sources.list /etc/apt/sources.list.d -type f \
            \( -name '*.sources' -o -name '*.list' \) 2>/dev/null \
            | xargs -r sed -i 's|http://deb.debian.org|https://deb.debian.org|g'; \
        # Apt config tuned for TLS-inspecting proxies: \
        #   - gzip only: some proxies decompress .xz/.zst for inspection and \
        #     corrupt the response; .gz round-trips more reliably. \
        #   - Pipeline-Depth 0: serialize requests so an intercepting proxy \
        #     can't interleave/truncate parallel streams. \
        #   - Retries 3: tolerate transient proxy hiccups (early TLS EOFs). \
        printf '%s\n' \
            'Acquire::CompressionTypes::Order:: "gz";' \
            'Acquire::http::Pipeline-Depth "0";' \
            'Acquire::https::Pipeline-Depth "0";' \
            'Acquire::Retries "3";' \
            > /etc/apt/apt.conf.d/99-corp-proxy; \
    fi

# Optional: drop -updates and -security mirrors for networks where the corp
# proxy still blocks those specific mirror paths. Triggered by
# --build-arg BUILD_SKIP_DEBIAN_UPDATES=1. Handles both layouts:
#   - deb822 (.sources): a separate stanza per URI — drop the security stanza,
#     strip the -updates token from the main stanza's Suites.
#   - legacy (.list): one line per source — drop any line referencing
#     -updates or -security.
# Suite-name-agnostic (works for trixie, bookworm, etc.).
RUN if [ "$BUILD_SKIP_DEBIAN_UPDATES" = "1" ]; then \
        for f in /etc/apt/sources.list.d/debian.sources; do \
            [ -e "$f" ] || continue; \
            awk 'BEGIN { RS=""; ORS="\n\n" } \
                 { if ($0 ~ /URIs:[^\n]*-security/) next; \
                   gsub(/[[:space:]]+[a-z]+-updates/, ""); \
                   gsub(/[[:space:]]+[a-z]+-security/, ""); \
                   print }' "$f" > "$f.new" && mv "$f.new" "$f"; \
        done; \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do \
            [ -e "$f" ] || continue; \
            sed -i '/-updates/d; /-security/d' "$f"; \
        done; \
        echo "BUILD_SKIP_DEBIAN_UPDATES=1: -updates and -security dropped from apt sources"; \
        echo "--- resulting apt sources ---"; \
        cat /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        echo "--- end ---"; \
    fi

# Point Python TLS clients (pip, requests, etc.) at the system trust store
# so they pick up any corp CA installed above.
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Install Python dependencies first so this layer caches when only app
# code changes.
COPY web_dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY web_dashboard/ ./web_dashboard/

# In-repo docs, rendered at /docs/<page> by api/docs_pages.py (the "guide" links
# in Settings). Markdown only; small.
COPY docs/ ./docs/

# Cloud-database Terraform modules (driven by cloud_database_service). The rest
# of terraform/ is generated at runtime / cached at build, so only the static
# DB modules are copied in. One COPY per cloud — adding a cloud means adding its
# module here AND its provider to the pre-cache init below, or the published
# image is missing it at runtime.
COPY terraform/db_postgres/ ./terraform/db_postgres/
COPY terraform/db_azure_postgres/ ./terraform/db_azure_postgres/
COPY terraform/db_gcp_postgres/ ./terraform/db_gcp_postgres/
COPY terraform/db_mysql/ ./terraform/db_mysql/
COPY terraform/db_azure_mysql/ ./terraform/db_azure_mysql/
COPY terraform/db_gcp_mysql/ ./terraform/db_gcp_mysql/
COPY terraform/db_sqlserver/ ./terraform/db_sqlserver/
COPY terraform/db_gcp_sqlserver/ ./terraform/db_gcp_sqlserver/
COPY terraform/db_azure_sqlserver/ ./terraform/db_azure_sqlserver/
# Managed-Kubernetes provisioning modules (driven by k8s_service, §1.1a): EKS
# (hashicorp/aws), AKS (hashicorp/azurerm ~> 3.0), GKE (hashicorp/google ~> 5.0) —
# all three providers are already in the pre-cache init below. Without the AKS/GKE
# COPYs an azure/gcp provision fails at _materialize: "No such file or directory:
# /app/terraform/k8s_cluster/azure_aks".
COPY terraform/k8s_cluster/aws_eks/ ./terraform/k8s_cluster/aws_eks/
COPY terraform/k8s_cluster/azure_aks/ ./terraform/k8s_cluster/azure_aks/
COPY terraform/k8s_cluster/gcp_gke/ ./terraform/k8s_cluster/gcp_gke/
# Action-level admission-control policies (Rego), evaluated by admission_service
# via the bundled OPA binary (installed below). Ship the tree so operators can
# add/edit rules; admission_service reads terraform/policy/admission/ pre-action.
COPY terraform/policy/ ./terraform/policy/

# Container-sane defaults; .env overrides these at runtime.
ENV LOG_DIR=/tmp/logs \
    WEBAUTHN_RP_ID=localhost \
    WEBAUTHN_ORIGIN=http://localhost:8001

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
        qemu-utils \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install Packer (architecture-aware) and pre-cache all three cloud plugins
# so packer init does not require internet access at build time.
# 1.10+ required for S3-native state locking (use_lockfile) — no DynamoDB needed.
# See services/terraform.py + docs/terraform-state-backend-plan.md.
ARG TERRAFORM_VERSION=1.10.5
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_${ARCH}.zip" \
        -o /tmp/packer.zip \
    && unzip -q /tmp/packer.zip -d /usr/local/bin/ \
    && rm /tmp/packer.zip \
    && packer plugins install github.com/hashicorp/amazon \
    && packer plugins install github.com/hashicorp/azure \
    && packer plugins install github.com/hashicorp/googlecompute

# Install OPA (Open Policy Agent) — the bundled binary admission_service shells
# for pre-action policy guardrails (services/_opa.py). Static build, arch-aware
# (multi-arch image: amd64 + arm64). See docs/policy-guardrails.md.
ARG OPA_VERSION=0.70.0
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
         amd64) OPA_ARCH=amd64 ;; \
         arm64) OPA_ARCH=arm64 ;; \
         *) echo "unsupported arch $ARCH for OPA" && exit 1 ;; \
       esac \
    && curl -fsSL "https://openpolicyagent.org/downloads/v${OPA_VERSION}/opa_linux_${OPA_ARCH}_static" \
        -o /usr/local/bin/opa \
    && chmod +x /usr/local/bin/opa \
    && /usr/local/bin/opa version

# Install Terraform (architecture-aware) and pre-cache every provider the
# dashboard uses at run time — the BeyondTrust SRA provider (PRA tunnels/shell
# jumps) AND the cloud-database providers (aws/azurerm/google). Baking them in
# at build (on CI's clean network) means a pulled image has NO outbound provider
# download at run time — so cloud-DB provisioning works behind a TLS-inspecting
# proxy without the corp-CA dance, and isn't subject to flaky registry pulls.
# Keep these in sync with the version constraints in terraform/db_*/main.tf.
# The plugin cache directory is set via TF_PLUGIN_CACHE_DIR in terraform_pra_service.py.
#
# `terraform init` here talks to registry.terraform.io, whose client enforces a
# 10s default timeout (TF_REGISTRY_CLIENT_TIMEOUT). When the registry is briefly
# slow — observed on BOTH the native amd64 leg and the emulated (QEMU) arm64 leg
# of the multi-arch build — that 10s is exceeded ("request canceled
# (Client.Timeout exceeded while awaiting headers)") and init fails even though
# the provider exists. Resolving four providers (sra/aws/azurerm/google) in one
# init multiplies the registry round-trips, so a single slow response is enough.
# Fix: raise the registry client timeout to 30s AND keep a retry loop (fresh
# attempt each time), hard-failing after 5 tries so a genuinely unreachable
# registry never ships an image missing a cached provider.
ENV TF_PLUGIN_CACHE_DIR=/root/.terraform.d/plugin-cache
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip" \
        -o /tmp/terraform.zip \
    && unzip -qo /tmp/terraform.zip -d /usr/local/bin/ \
    && rm /tmp/terraform.zip \
    && mkdir -p "${TF_PLUGIN_CACHE_DIR}" \
    && mkdir -p /tmp/tf_provider_init \
    && printf 'terraform {\n  required_providers {\n    sra = { source = "beyondtrust/sra", version = "~> 1.0" }\n    passwordsafe = { source = "BeyondTrust/passwordsafe", version = "~> 1.0" }\n    entitle = { source = "entitleio/entitle", version = "~> 3.0" }\n    aws = { source = "hashicorp/aws", version = "~> 5.0" }\n    azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" }\n    google = { source = "hashicorp/google", version = "~> 5.0" }\n    oci = { source = "oracle/oci", version = "~> 5.0" }\n  }\n}\n' \
       > /tmp/tf_provider_init/main.tf \
    && for attempt in 1 2 3 4 5; do \
           TF_REGISTRY_CLIENT_TIMEOUT=30 terraform -chdir=/tmp/tf_provider_init init && break; \
           if [ "$attempt" = 5 ]; then \
               echo "terraform init failed to cache providers (sra/passwordsafe/entitle/aws/azurerm/google/oci) after 5 attempts" >&2; \
               exit 1; \
           fi; \
           echo "terraform init attempt $attempt failed (transient registry error); retrying in $((attempt * 5))s..." >&2; \
           sleep $((attempt * 5)); \
       done \
    && mkdir -p /tmp/tf_provider_init_az4 \
    && printf 'terraform {\n  required_providers {\n    azurerm = { source = "hashicorp/azurerm", version = ">= 4.55.0, < 5.0" }\n  }\n}\n' \
       > /tmp/tf_provider_init_az4/main.tf \
    && for attempt in 1 2 3 4 5; do \
           TF_REGISTRY_CLIENT_TIMEOUT=30 terraform -chdir=/tmp/tf_provider_init_az4 init && break; \
           if [ "$attempt" = 5 ]; then \
               echo "failed to cache azurerm 4.x (db_azure_mysql needs >= 4.55 for MySQL 8.4) after 5 attempts" >&2; \
               exit 1; \
           fi; \
           echo "azurerm 4.x init attempt $attempt failed (transient registry error); retrying in $((attempt * 5))s..." >&2; \
           sleep $((attempt * 5)); \
       done \
    && mkdir -p /tmp/tf_provider_init_g6 \
    && printf 'terraform {\n  required_providers {\n    google = { source = "hashicorp/google", version = "~> 6.0" }\n  }\n}\n' \
       > /tmp/tf_provider_init_g6/main.tf \
    && for attempt in 1 2 3 4 5; do \
           TF_REGISTRY_CLIENT_TIMEOUT=30 terraform -chdir=/tmp/tf_provider_init_g6 init && break; \
           if [ "$attempt" = 5 ]; then \
               echo "failed to cache google 6.x (db_gcp_mysql needs >= 6.x for MYSQL_8_4) after 5 attempts" >&2; \
               exit 1; \
           fi; \
           echo "google 6.x init attempt $attempt failed (transient registry error); retrying in $((attempt * 5))s..." >&2; \
           sleep $((attempt * 5)); \
       done \
    && rm -rf /tmp/tf_provider_init /tmp/tf_provider_init_az4 /tmp/tf_provider_init_g6

# kubectl + helm -- baked in so the k8s management-plane / ESO / Entitle-agent ops
# run them as in-process subprocesses (services/k8s_service), NOT as sibling
# containers over a Docker socket. That removes the docker.sock dependency, so the
# dashboard runs on managed/serverless runtimes (Azure Container Instances, Cloud
# Run, ECS/Fargate) that don't expose one -- and drops the whole sibling-runner
# failure class (entrypoint, CA trust, shared volume, file perms). Fetched on CI's
# clean network; both are architecture-aware (linux/amd64 + linux/arm64).
RUN ARCH=$(dpkg --print-architecture) \
    && KVER="$(curl -fsSL https://dl.k8s.io/release/stable.txt)" \
    && curl -fsSL "https://dl.k8s.io/release/${KVER}/bin/linux/${ARCH}/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client \
    && curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash \
    && helm version

# Entrypoint fixes SSH key permissions when the Windows override
# bind-mounts a key from %USERPROFILE%. Docker Desktop surfaces Windows
# files as mode 0777 and sshd-client refuses keys that world-readable,
# so copy to a private path before invoking gunicorn.
RUN printf '#!/bin/sh\nif [ -f /root/.ssh/dev_dashboard_key ]; then\n    install -m 600 /root/.ssh/dev_dashboard_key /root/.ssh/dev_key\nfi\nexec "$@"\n' \
    > /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Long-lived job-progress WebSockets must outlive a provision; the durable
# Terraform jobs now run in the separate `worker` service (web_dashboard.jobs_worker),
# so this generous request timeout only affects HTTP/WS, not job durability — it
# just stops the WS-handling worker being recycled out from under an open job page.
CMD ["gunicorn", \
     "-w", "2", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "1800", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "web_dashboard.main:app"]
