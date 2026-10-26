# Terraform, Packer, and all other tools are downloaded as architecture-aware
# binaries (see RUN steps below), so the image builds and runs natively on
# both ARM64 (Apple Silicon, AWS Graviton) and AMD64.
#
# Every downloaded binary is pinned to a version AND to the SHA256 its vendor published
# for that version, per architecture, and the build fails on a mismatch (helm: against
# the checksum file it publishes beside the tarball, see below). TLS proves who
# served the bytes, not that they are the bytes that were released; a pinned hash does.
# Bumping a version means updating its two hashes from the vendor's checksum file in the
# same change (the URL is in the comment next to each pair). Terraform providers and
# Packer plugins need no hash here: `terraform init` and `packer plugins install` verify
# signed checksums themselves.

# The SPIRE server CLI, for driving a SPIRE SIDECAR through its admin socket on a managed
# container runtime (SPIRE_SERVER_SOCKET; services/dashboard_spire, examples/cloud-spire).
# Only the client half is used here — this image never runs a server. Statically linked,
# so it runs on the slim base unchanged. Same pinned digest as docker-compose.spire.yml
# and the sidecar image; tests/test_spire_overlay.py keeps all three in step.
FROM ghcr.io/spiffe/spire-server:1.15.5@sha256:584a9c5cc95b9457f5033e7e4488d603fffe5ec48a80c4dba14b05c5e1c2c384 AS spire

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS base

ARG PACKER_VERSION=1.16.2
# https://releases.hashicorp.com/packer/1.16.2/packer_1.16.2_SHA256SUMS
ARG PACKER_SHA256_AMD64=abbfe0cf74985084112659b53e3565f264f70a20914c3b7c4f789f54f61c2414
ARG PACKER_SHA256_ARM64=5d7d6de86a49b2d64f3c0eef5766855b580e10a7c45bd6605ba8f1dead7815ea
# The four cloud plugins, pinned so a rebuild cannot pick up a new release unannounced.
# Move them with PACKER_VERSION: newer plugin builds are published only to
# releases.hashicorp.com, which Packer < 1.14 cannot install from — under 1.11.2 every
# image quietly stayed on the last GitHub-hosted builds (amazon 1.3.9, azure 2.3.3,
# googlecompute 1.2.1), whatever the latest release was.
ARG PACKER_PLUGIN_AMAZON=1.8.3
ARG PACKER_PLUGIN_AZURE=2.6.4
ARG PACKER_PLUGIN_GOOGLECOMPUTE=1.2.8
ARG PACKER_PLUGIN_ORACLE=1.1.2

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

# Cloud Functions: database drivers vendored into the deployed function zip.
#
# The fnruntime contract is stdlib-only precisely so packages need no vendoring,
# and the db_grant workload is the one deliberate exception: it opens a real
# database connection. PyMySQL is pure Python and would be safe to vendor from
# site-packages, but SQL Server is not — python-tds needs pyOpenSSL for TLS,
# pyOpenSSL needs cryptography, and cryptography ships a COMPILED extension.
#
# Vendoring a compiled wheel out of site-packages is unsafe here: this image is
# built multi-arch, so an arm64 build would place an arm64 .so into a zip that
# runs on x86_64 and fail at import time, in the cloud, with a confusing error.
# So fetch the wheels for the FUNCTION's platform explicitly, at build time, into
# a directory the packager reads instead of site-packages — the same "resolve it
# at build, never at runtime" discipline as the Terraform provider cache above.
#
# --platform/--only-binary is also what makes this correct on an arm64 builder:
# pip resolves for the TARGET, not the host. All three clouds default to x86_64
# for Python functions; an operator deploying arm64 Lambdas needs a second dir
# here and a matching architecture in the aws_lambda module.
ARG FN_VENDOR_PLATFORM=manylinux2014_x86_64
ENV FN_VENDOR_DIR=/opt/fn-vendor/linux-x86_64
RUN pip install --no-cache-dir --no-deps --upgrade \
        --platform "${FN_VENDOR_PLATFORM}" \
        --python-version 3.12 \
        --only-binary=:all: \
        --target "${FN_VENDOR_DIR}" \
        PyMySQL==1.1.1 python-tds==1.16.0 pyOpenSSL==24.2.1 cryptography==43.0.1 \
        cffi==1.17.1 pycparser==2.22 \
    && find "${FN_VENDOR_DIR}" -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find "${FN_VENDOR_DIR}" -name '*.dist-info' -type d -prune -exec rm -rf {} +

# Copy the application.
COPY web_dashboard/ ./web_dashboard/

# Build provenance for Cloud Functions.
#
# A deployed function runs privileged code — a database admin credential, or rights
# to write Azure role assignments — from inside a customer network. These make
# "which commit is running in that function?" answerable months later, recorded on
# the function's row at deploy time (services/build_provenance.py).
#
# All optional: an image built without them still records the handler source-tree
# hash, which is computed from the files and cannot be wrong. Git metadata is the
# nice-to-have, so a missing build arg degrades to "unknown" rather than to a guess.
#
#   docker build \
#     --build-arg GIT_SHA="$(git rev-parse HEAD)" \
#     --build-arg GIT_REF="$(git rev-parse --abbrev-ref HEAD)" \
#     --build-arg GIT_ORIGIN="$(git config --get remote.origin.url)" \
#     --build-arg BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" .
#
# Declared AFTER the COPY so changing them does not bust the dependency layers.
ARG GIT_SHA=""
ARG GIT_REF=""
ARG GIT_ORIGIN=""
ARG BUILT_AT=""
ENV DASHBOARD_GIT_SHA=${GIT_SHA} \
    DASHBOARD_GIT_REF=${GIT_REF} \
    DASHBOARD_GIT_ORIGIN=${GIT_ORIGIN} \
    DASHBOARD_BUILT_AT=${BUILT_AT}
LABEL org.opencontainers.image.revision=${GIT_SHA} \
      org.opencontainers.image.created=${BUILT_AT}

# In-repo docs, rendered at /docs/<page> by api/docs_pages.py (the "guide" links
# in Settings). Markdown only; small.
COPY docs/ ./docs/

# Packer provisioner scripts. The POV standard images (services/pov_image_recipes)
# run these straight from the image (packer_build_service.PROVISIONERS_DIR); without
# this COPY every standard build fails naming the missing script. Small text files.
COPY provisioners/ ./provisioners/

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
COPY terraform/db_oci_autonomous/ ./terraform/db_oci_autonomous/
COPY terraform/db_aws_oracle/ ./terraform/db_aws_oracle/
COPY terraform/db_atlas_mongodb/ ./terraform/db_atlas_mongodb/
# Managed-Kubernetes provisioning modules (driven by k8s_service, §1.1a): EKS
# (hashicorp/aws), AKS (hashicorp/azurerm ~> 3.0), GKE (hashicorp/google ~> 5.0),
# OKE (oracle/oci ~> 5.0) — all four providers are already in the pre-cache init
# below. Without the AKS/GKE/OKE COPYs an azure/gcp/oci provision fails at
# _materialize: "No such file or directory: /app/terraform/k8s_cluster/azure_aks".
COPY terraform/k8s_cluster/aws_eks/ ./terraform/k8s_cluster/aws_eks/
COPY terraform/k8s_cluster/azure_aks/ ./terraform/k8s_cluster/azure_aks/
COPY terraform/k8s_cluster/gcp_gke/ ./terraform/k8s_cluster/gcp_gke/
COPY terraform/k8s_cluster/oci_oke/ ./terraform/k8s_cluster/oci_oke/
# Cloud Functions modules (driven by cloud_function_service) — Lambda / Linux
# Function App / Cloud Run function. Same rule as above: a missing COPY here is a
# _materialize failure at deploy time, in the published image only.
COPY terraform/cloud_function/ ./terraform/cloud_function/
# Certificate-authority provisioning modules (driven by cert_lab_service): the GCP CAS
# pool + root CA + enrollment service account behind the Certificate plugin's `gcpcas`
# backend. Same rule as above -- a missing COPY here is a _materialize failure at
# deploy time, in the published image only. The google provider is already pre-cached
# below, so no new init leg is needed.
COPY terraform/cert_ca/ ./terraform/cert_ca/
# Managed Active Directory modules (driven by directory_service): AWS Managed Microsoft AD
# (hashicorp/aws ~> 5.0) and GCP Managed AD (hashicorp/google ~> 6.0). Both providers are
# already in the pre-cache inits below.
COPY terraform/directory/ ./terraform/directory/
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
    && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install Packer (architecture-aware) and pre-cache all four cloud plugins
# (amazon/azure/googlecompute/oracle) so packer init does not require internet
# access at build time.
# 1.10+ required for S3-native state locking (use_lockfile) — no DynamoDB needed.
# See services/terraform.py + docs/terraform-state-backend-plan.md.
#
# `packer plugins install` resolves each plugin through the GitHub *API*
# (GET /repos/hashicorp/packer-plugin-<name>/git/matching-refs/tags), which is
# rate limited to 60 requests/hour per source IP when unauthenticated. On a
# GitHub-hosted runner that IP is shared NAT, so the hour's budget is often
# already spent by unrelated tenants before this step runs — release v26.7.3
# died here with "403 API rate limit exceeded for 20.29.188.144 [rate reset in
# 8m05s]": amazon installed, then azure was refused. Nothing in the build had
# changed; v26.7.2 built the identical layer 2.5h earlier.
#
# Two defences, in the order they apply:
#   1. PACKER_GITHUB_API_TOKEN from a build *secret* — never a --build-arg,
#      which would leave the token in the image history. CI passes the
#      workflow's GITHUB_TOKEN (see .github/workflows/publish-images.yml),
#      lifting the ceiling to 1000 req/hour/repo so a shared IP stops
#      mattering. required=false keeps tokenless local builds
#      (`docker compose build`, scripts/onboard.sh) working unchanged.
#   2. A retry loop for tokenless builds. A rate-limit reset is minutes away,
#      not seconds, so the backoff escalates (60/120/180/240s, ~10min total)
#      instead of burning its attempts inside a window that is still closed.
#      That covers a reset like the one above but cannot outlast a full hourly
#      window — hence the token is the real fix, this is the safety net. It
#      hard-fails rather than ship an image whose plugins aren't cached.
# The packer.zip curl also picks up the --retry flags the other release-artifact
# fetches in this file already carry (see the helm-install curl below).
ARG TERRAFORM_VERSION=1.17.0
# Packer plugins go to a fixed path, not root's home (~/.config/packer/plugins), so the
# unprivileged user the app runs as finds them; `packer init` at build time then sees them
# installed and downloads nothing.
ENV PACKER_PLUGIN_PATH=/opt/packer-plugins
# https://releases.hashicorp.com/terraform/1.17.0/terraform_1.17.0_SHA256SUMS
ARG TERRAFORM_SHA256_AMD64=c168cab7c637a8769fdb90089ddd19de701bc6ff8aca64dc2e2185fe46f59f67
ARG TERRAFORM_SHA256_ARM64=f32478134ffc6d710eec474bbd316652cdddc0f4e336973a3745e8d3a3309cdf
RUN --mount=type=secret,id=github_token,required=false \
    ARCH=$(dpkg --print-architecture) \
    && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_${ARCH}.zip" \
        -o /tmp/packer.zip \
    && SHA=$([ "$ARCH" = arm64 ] && echo "$PACKER_SHA256_ARM64" || echo "$PACKER_SHA256_AMD64") \
    && echo "$SHA  /tmp/packer.zip" | sha256sum -c - \
    && unzip -q /tmp/packer.zip -d /usr/local/bin/ \
    && rm /tmp/packer.zip \
    && if [ -s /run/secrets/github_token ]; then \
           PACKER_GITHUB_API_TOKEN="$(cat /run/secrets/github_token)"; \
           export PACKER_GITHUB_API_TOKEN; \
           echo "packer plugin getter: authenticating to the GitHub API"; \
       else \
           echo "packer plugin getter: no GITHUB token, using the 60/hour anonymous quota"; \
       fi \
    && mkdir -p "${PACKER_PLUGIN_PATH}" \
    && for plugin in amazon azure googlecompute oracle; do \
           case "$plugin" in \
               amazon) pin="$PACKER_PLUGIN_AMAZON" ;; \
               azure) pin="$PACKER_PLUGIN_AZURE" ;; \
               googlecompute) pin="$PACKER_PLUGIN_GOOGLECOMPUTE" ;; \
               oracle) pin="$PACKER_PLUGIN_ORACLE" ;; \
           esac; \
           for attempt in 1 2 3 4 5; do \
               packer plugins install "github.com/hashicorp/${plugin}" "v${pin}" && break; \
               if [ "$attempt" = 5 ]; then \
                   echo "packer plugins install ${plugin} failed after 5 attempts; if this is a 403 rate limit, pass a GitHub token as the github_token build secret" >&2; \
                   exit 1; \
               fi; \
               echo "packer plugins install ${plugin} attempt $attempt failed (GitHub API rate limit?); retrying in $((attempt * 60))s..." >&2; \
               sleep $((attempt * 60)); \
           done; \
       done

# Install OPA (Open Policy Agent) — the bundled binary admission_service shells
# for pre-action policy guardrails (services/_opa.py). Static build, arch-aware
# (multi-arch image: amd64 + arm64). See docs/operations/scheduling/policy-guardrails.md.
#
# --retry/--retry-all-errors: the openpolicyagent.org download endpoint
# intermittently drops the connection mid-transfer — observed as
# `curl: (56) OpenSSL SSL_read: ... unexpected eof while reading` on the amd64
# leg of release v26.9.7, which failed the whole multi-arch build. curl's
# built-in retry only covers timeouts and a few HTTP codes, so a dropped
# stream (exit 56) needs --retry-all-errors to be retried at all. The
# `opa version` below stays the proof the binary arrived intact.
# OPA 1.x parses Rego v1 by default: a policy without `import rego.v1` (or the `if` /
# `contains` keywords) no longer loads, and the admission gate then fails closed on
# every gated action. The shipped policies already import rego.v1.
ARG OPA_VERSION=1.21.3
# https://github.com/open-policy-agent/opa/releases/download/v1.21.3/opa_linux_<arch>_static.sha256
ARG OPA_SHA256_AMD64=add7c9dcf56e1ce1538426a6ee595ea1cc7ee830bbedd4c23219825b02b19a79
ARG OPA_SHA256_ARM64=45c8734a41436bf21d82453c2aa7b0ad47c150e8237310b8665e8b5a01305fc0
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
         amd64) OPA_ARCH=amd64 ;; \
         arm64) OPA_ARCH=arm64 ;; \
         *) echo "unsupported arch $ARCH for OPA" && exit 1 ;; \
       esac \
    && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        "https://openpolicyagent.org/downloads/v${OPA_VERSION}/opa_linux_${OPA_ARCH}_static" \
        -o /usr/local/bin/opa \
    && SHA=$([ "$OPA_ARCH" = arm64 ] && echo "$OPA_SHA256_ARM64" || echo "$OPA_SHA256_AMD64") \
    && echo "$SHA  /usr/local/bin/opa" | sha256sum -c - \
    && chmod +x /usr/local/bin/opa \
    && /usr/local/bin/opa version

# Install Terraform (architecture-aware) and pre-cache every provider the
# dashboard uses at run time — the BeyondTrust SRA provider (PRA tunnels/shell
# jumps) AND the cloud-database / k8s-cluster providers (aws/azurerm/google/oci —
# oracle/oci covers both db_oci_autonomous and k8s_cluster/oci_oke). Baking them in
# at build (on CI's clean network) means a pulled image has NO outbound provider
# download at run time — so cloud-DB provisioning works behind a TLS-inspecting
# proxy without the corp-CA dance, and isn't subject to flaky registry pulls.
# Keep these in sync with the version constraints in terraform/db_*/main.tf.
#
# The providers land in a READ-ONLY FILESYSTEM MIRROR (TF_PROVIDER_MIRROR_DIR, wired up
# via /etc/terraform.tfrc below), not in a runtime TF_PLUGIN_CACHE_DIR. TF_PLUGIN_CACHE_DIR
# is used HERE, at build time, only because `terraform init` is how the providers get
# downloaded and unpacked in the first place — it must NOT be set at run time. Two reasons:
#
#   * The plugin cache is shared MUTABLE state. `terraform init` symlinks
#     .terraform/providers/<...> straight at the cache entry, so a running apply/destroy
#     EXECUTES the cached binary, while any init that has to (re)install that same
#     provider rewrites it in place -> ETXTBSY "text file busy", failing an unrelated
#     job (job cc8743c3, a clouddb_decommission killed by a concurrent k8s destroy).
#     A mirror is only ever read, so that race cannot exist.
#   * The plugin cache is NOT an offline store: Terraform still queries
#     registry.terraform.io to resolve versions even when the cache holds the answer,
#     which defeats the whole point of baking providers in and breaks init behind a
#     TLS-inspecting proxy. A filesystem_mirror + `direct { exclude }` resolves entirely
#     from disk, so a pulled image really does provision with no outbound provider fetch.
#
# `terraform init` here talks to registry.terraform.io, whose client enforces a
# 10s default timeout (TF_REGISTRY_CLIENT_TIMEOUT). When the registry is briefly
# slow — observed on BOTH the native amd64 leg and the emulated (QEMU) arm64 leg
# of the multi-arch build — that 10s is exceeded ("request canceled
# (Client.Timeout exceeded while awaiting headers)") and init fails even though
# the provider exists. Resolving seven providers (sra/passwordsafe/entitle/aws/
# azurerm/google/oci) in one init multiplies the registry round-trips, so a single
# slow response is enough.
# The registry also proxies provider signatures/checksums from github.com, so a
# GitHub blip surfaces here as "504 Gateway Timeout returned from github.com"
# (this failed release v26.6.5). Such blips can outlast a short retry window, so
# the loop runs 8 attempts with escalating backoff (~2.5min total).
# Fix: raise the registry client timeout to 30s AND keep a retry loop (fresh
# attempt each time), hard-failing after 8 tries so a genuinely unreachable
# registry never ships an image missing a cached provider.
ENV TF_PROVIDER_MIRROR_DIR=/opt/terraform-provider-mirror
RUN export TF_PLUGIN_CACHE_DIR="${TF_PROVIDER_MIRROR_DIR}" \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip" \
        -o /tmp/terraform.zip \
    && SHA=$([ "$ARCH" = arm64 ] && echo "$TERRAFORM_SHA256_ARM64" || echo "$TERRAFORM_SHA256_AMD64") \
    && echo "$SHA  /tmp/terraform.zip" | sha256sum -c - \
    && unzip -qo /tmp/terraform.zip -d /usr/local/bin/ \
    && rm /tmp/terraform.zip \
    && mkdir -p "${TF_PLUGIN_CACHE_DIR}" \
    && mkdir -p /tmp/tf_provider_init \
    && printf 'terraform {\n  required_providers {\n    sra = { source = "beyondtrust/sra", version = "~> 1.0" }\n    passwordsafe = { source = "BeyondTrust/passwordsafe", version = "~> 1.0" }\n    entitle = { source = "entitleio/entitle", version = "~> 3.0" }\n    aws = { source = "hashicorp/aws", version = "~> 5.0" }\n    azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" }\n    google = { source = "hashicorp/google", version = "~> 5.0" }\n    oci = { source = "oracle/oci", version = "~> 5.0" }\n    mongodbatlas = { source = "mongodb/mongodbatlas", version = "~> 2.19" }\n  }\n}\n' \
       > /tmp/tf_provider_init/main.tf \
    && for attempt in 1 2 3 4 5 6 7 8; do \
           TF_REGISTRY_CLIENT_TIMEOUT=30 terraform -chdir=/tmp/tf_provider_init init && break; \
           if [ "$attempt" = 8 ]; then \
               echo "terraform init failed to cache providers (sra/passwordsafe/entitle/aws/azurerm/google/oci/mongodbatlas) after 8 attempts" >&2; \
               exit 1; \
           fi; \
           echo "terraform init attempt $attempt failed (transient registry error); retrying in $((attempt * 5))s..." >&2; \
           sleep $((attempt * 5)); \
       done \
    && mkdir -p /tmp/tf_provider_init_az4 \
    && printf 'terraform {\n  required_providers {\n    azurerm = { source = "hashicorp/azurerm", version = ">= 4.55.0, < 5.0" }\n  }\n}\n' \
       > /tmp/tf_provider_init_az4/main.tf \
    && for attempt in 1 2 3 4 5 6 7 8; do \
           TF_REGISTRY_CLIENT_TIMEOUT=30 terraform -chdir=/tmp/tf_provider_init_az4 init && break; \
           if [ "$attempt" = 8 ]; then \
               echo "failed to cache azurerm 4.x (db_azure_mysql needs >= 4.55 for MySQL 8.4) after 8 attempts" >&2; \
               exit 1; \
           fi; \
           echo "azurerm 4.x init attempt $attempt failed (transient registry error); retrying in $((attempt * 5))s..." >&2; \
           sleep $((attempt * 5)); \
       done \
    && mkdir -p /tmp/tf_provider_init_g6 \
    && printf 'terraform {\n  required_providers {\n    google = { source = "hashicorp/google", version = "~> 6.0" }\n  }\n}\n' \
       > /tmp/tf_provider_init_g6/main.tf \
    && for attempt in 1 2 3 4 5 6 7 8; do \
           TF_REGISTRY_CLIENT_TIMEOUT=30 terraform -chdir=/tmp/tf_provider_init_g6 init && break; \
           if [ "$attempt" = 8 ]; then \
               echo "failed to cache google 6.x (db_gcp_mysql needs >= 6.x for MYSQL_8_4) after 8 attempts" >&2; \
               exit 1; \
           fi; \
           echo "google 6.x init attempt $attempt failed (transient registry error); retrying in $((attempt * 5))s..." >&2; \
           sleep $((attempt * 5)); \
       done \
    && mkdir -p /tmp/tf_provider_init_g7 \
    && printf 'terraform {\n  required_providers {\n    google = { source = "hashicorp/google", version = "~> 7.21" }\n  }\n}\n' \
       > /tmp/tf_provider_init_g7/main.tf \
    && for attempt in 1 2 3 4 5 6 7 8; do \
           TF_REGISTRY_CLIENT_TIMEOUT=30 terraform -chdir=/tmp/tf_provider_init_g7 init && break; \
           if [ "$attempt" = 8 ]; then \
               echo "failed to cache google 7.x (cloud_function/gcp_cloudrun needs >= 7.21 for Direct VPC egress) after 8 attempts" >&2; \
               exit 1; \
           fi; \
           echo "google 7.x init attempt $attempt failed (transient registry error); retrying in $((attempt * 5))s..." >&2; \
           sleep $((attempt * 5)); \
       done \
    && rm -rf /tmp/tf_provider_init /tmp/tf_provider_init_az4 /tmp/tf_provider_init_g6 /tmp/tf_provider_init_g7

# Serve those providers to every run-time `terraform init` as a read-only filesystem
# mirror. `direct { exclude = ["*/*/*"] }` bans the registry outright, so init resolves
# from disk alone: no version query, no download, and nothing ever writes the shared
# directory (the ETXTBSY race above is structurally impossible, not merely serialized).
# The path comes from TF_PROVIDER_MIRROR_DIR so the mirror the config names and the
# directory the pre-cache populated cannot drift apart.
#
# Consequence, deliberately loud: a provider that is NOT baked in above fails init with
# "was not found in any of the search locations" instead of silently downloading. Adding
# a provider (or widening a version constraint past what is cached) therefore REQUIRES a
# matching leg in the pre-cache block — which is what the verification below proves.
#
# NOTHING may set TF_PLUGIN_CACHE_DIR at run time: with the cache pointed at the mirror,
# terraform tries to install the mirror's own directory over itself and every init dies
# with "cannot install existing provider directory ... to itself".
RUN printf 'provider_installation {\n  filesystem_mirror {\n    path    = "%s"\n    include = ["*/*/*"]\n  }\n  direct {\n    exclude = ["*/*/*"]\n  }\n}\n' \
        "${TF_PROVIDER_MIRROR_DIR}" > /etc/terraform.tfrc \
    && cat /etc/terraform.tfrc
ENV TF_CLI_CONFIG_FILE=/etc/terraform.tfrc

# Prove the mirror can actually serve every module the image ships plus the three
# providers the PRA / Password-Safe / Entitle services generate HCL for. This is the
# real run-time code path (same tfrc, same modules, registry banned), so a pre-cache leg
# that no longer satisfies a module's constraint fails the BUILD instead of failing a
# customer's provision months later. Each module is init'd in a COPY: an init in place
# would leave a .terraform symlink farm that `terraform._materialize` (shutil.copytree,
# which dereferences) would then duplicate ~100 MB of provider into every deploy dir.
RUN set -eu; \
    fail=""; \
    check() { \
        if terraform -chdir=/tmp/tfverify init -input=false -no-color >/tmp/tfverify.log 2>&1; then \
            echo "provider mirror OK   : $1"; \
        else \
            echo "provider mirror MISS : $1" >&2; cat /tmp/tfverify.log >&2; fail="yes"; \
        fi; \
    }; \
    for dir in $(find /app/terraform -name '*.tf' -exec dirname {} \; | sort -u); do \
        grep -qs required_providers "$dir"/*.tf || continue; \
        rm -rf /tmp/tfverify; mkdir -p /tmp/tfverify; cp "$dir"/*.tf /tmp/tfverify/; \
        check "$dir"; \
    done; \
    for spec in 'beyondtrust/sra|~> 1.0' 'BeyondTrust/passwordsafe|~> 1.0' 'entitleio/entitle|~> 3.0'; do \
        rm -rf /tmp/tfverify; mkdir -p /tmp/tfverify; \
        printf 'terraform {\n  required_providers {\n    p = { source = "%s", version = "%s" }\n  }\n}\n' \
            "${spec%%|*}" "${spec#*|}" > /tmp/tfverify/main.tf; \
        check "${spec%%|*} (generated HCL)"; \
    done; \
    rm -rf /tmp/tfverify /tmp/tfverify.log; \
    if [ -n "$fail" ]; then \
        echo "the provider mirror cannot satisfy every shipped module — add/adjust a leg in the pre-cache block above" >&2; \
        exit 1; \
    fi

# kubectl + helm -- baked in so the k8s management-plane / ESO / Entitle-agent ops
# run them as in-process subprocesses (services/k8s_service), NOT as sibling
# containers over a Docker socket. That removes the docker.sock dependency, so the
# dashboard runs on managed/serverless runtimes (Azure Container Instances, Cloud
# Run, ECS/Fargate) that don't expose one -- and drops the whole sibling-runner
# failure class (entrypoint, CA trust, shared volume, file perms). Fetched on CI's
# clean network; both are architecture-aware (linux/amd64 + linux/arm64).
#
# Both are pinned. kubectl used to follow dl.k8s.io/release/stable.txt, so two builds of
# the same commit could ship different kubectls; helm came from `curl get-helm-3 | bash`
# off helm's main branch, which runs whatever that script says on the day. 1.37.1 and
# 3.22.0 are what that produced in the last release, so behaviour is unchanged.
# helm is checked against the .sha256sum file helm publishes next to each tarball rather
# than a hash pinned here: get.helm.sh is the only place helm publishes its checksums.
ARG KUBECTL_VERSION=v1.37.1
# https://dl.k8s.io/release/v1.37.1/bin/linux/<arch>/kubectl.sha256
ARG KUBECTL_SHA256_AMD64=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8
ARG KUBECTL_SHA256_ARM64=ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7
ARG HELM_VERSION=v3.22.0
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
        -o /usr/local/bin/kubectl \
    && SHA=$([ "$ARCH" = arm64 ] && echo "$KUBECTL_SHA256_ARM64" || echo "$KUBECTL_SHA256_AMD64") \
    && echo "$SHA  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client \
    && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" -o /tmp/helm.tar.gz \
    && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz.sha256sum" -o /tmp/helm.sha256 \
    && echo "$(cut -d' ' -f1 /tmp/helm.sha256)  /tmp/helm.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/helm.tar.gz -C /tmp \
    && mv "/tmp/linux-${ARCH}/helm" /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz /tmp/helm.sha256 "/tmp/linux-${ARCH}" \
    && helm version

# The app and the worker run as `dashboard` (uid/gid 10001, the remote agent's ids), not
# root. The image does not set USER: the entrypoint starts as root only to give that user
# the host's Docker-socket group and to hand over volumes earlier (root) releases wrote,
# then drops to it with setpriv before the command runs. docker/entrypoint.sh explains.
# The directories the app writes are created here, owned by it. /app itself, and
# everything else in it, stays root-owned: an owner of /app could rename or replace
# /app/web_dashboard, which would undo the point of dropping root.
RUN groupadd -g 10001 dashboard \
    && useradd -u 10001 -g dashboard -m -d /home/dashboard -s /usr/sbin/nologin dashboard \
    && install -d -o dashboard -g dashboard /app/terraform/deployments /app/packer/builds /app/data \
    && install -d -m 0700 -o dashboard -g dashboard /run/spiffe-tokens
# The SPIRE CLI (stage above). /run/spiffe-tokens is the dashboard's own JWT-SVID
# directory (services/dashboard_identity): compose mounts a tmpfs volume over it; on a
# managed runtime nothing does, and the app and the worker each keep their own copy here.
COPY --from=spire /opt/spire/bin/spire-server /usr/local/bin/spire-server
# The SQLite fallback lives in /app/data, which the app owns, not beside the code in /app
# (SQLite needs to write its journal in the database's directory). Compose and every
# cloud-hosting guide set DATABASE_URL to PostgreSQL, which overrides this; it only
# applies to a bare `docker run`. config.py keeps ./vm_cli.db for local development.
ENV DATABASE_URL=sqlite:////app/data/vm_cli.db
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
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
