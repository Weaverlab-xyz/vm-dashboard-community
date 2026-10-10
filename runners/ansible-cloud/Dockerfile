# The dashboard's Ansible runner image for KUBERNETES-CLUSTER and DATABASE
# config-management targets. Those run `hosts: localhost` plays that reach out to
# the cluster API (kubeconfig) or the DB endpoint (login vars) over the network —
# the opposite of the SSH/WinRM VM runner (`chrweav/ansible-winrm`), which this
# image deliberately does NOT replace.
#
# It carries the collections + client libraries those localhost plays need:
#   kubernetes.core            -> kubernetes  (k8s_info / k8s / helm) + the helm CLI
#   community.postgresql       -> psycopg2    (postgresql_db / postgresql_user)
#   community.mysql            -> PyMySQL     (mysql_db / mysql_user)
#   community.general (mssql)  -> pymssql     (mssql_db)
#   community.general (ldap_*) -> python-ldap (ldap_entry / ldap_attrs / ldap_search)
#   microsoft.ad               -> pywinrm + requests-ntlm (AD objects over WinRM)
#
# The directory plays (examples/playbooks/directory/) run here too, through a remote
# agent's `ansible.directory_image` (falling back to `db_image`).
#
# Used by every remote runner the dashboard drives for k8s/DB targets (AWS ECS,
# Azure ACI, GCP Cloud Run). Selected via the `ansible_cloud_image` config key
# (ANSIBLE_CLOUD_IMAGE); defaults to chrweav/ansible-cloud:latest.
#
# Debian base (not the Alpine `willhallonline/ansible` the winrm image uses):
# pymssql needs FreeTDS, which builds/installs painlessly on Debian/glibc but
# fights musl on Alpine.
#
# Published (public Docker Hub): chrweav/ansible-cloud:latest
# Build + push:
#   docker build -t chrweav/ansible-cloud:latest runners/ansible-cloud
#   docker push  chrweav/ansible-cloud:latest
# Multi-arch (amd64 + arm64), recommended so it runs on Fargate/Cloud Run/ACI and
# Apple-silicon dev hosts alike:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t chrweav/ansible-cloud:latest --push runners/ansible-cloud
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG HELM_VERSION=v3.16.3
ARG KUBECTL_VERSION=v1.31.3
# Pinned to the vendor's published SHA256 per architecture; see the root Dockerfile header.
# https://dl.k8s.io/release/v1.31.3/bin/linux/<arch>/kubectl.sha256
ARG KUBECTL_SHA256_AMD64=981f6b49577068bc174275184d8ee7105d8e54f40733792c519cd85023984c0f
ARG KUBECTL_SHA256_ARM64=a3953ad2b32eca0b429249a5fbdf4f8ef7d57223c45cc0401fd80fd12c7b9071

# FreeTDS for pymssql (the PyPI wheels bundle it, but keep the runtime libs +
# build headers so a source build also succeeds on any arch); curl/tar to fetch
# the helm + kubectl binaries; ca-certificates for TLS to the cluster/DB.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        gcc \
        freetds-dev \
        freetds-bin \
        libldap2-dev \
        libsasl2-dev \
        curl \
        tar \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# helm CLI — required by the kubernetes.core.helm module (it shells out to helm).
# --retry/--retry-all-errors: these release fetches are the same transient-blip
# failure class that killed the main image on a dropped TLS stream (curl exit 56)
# — see the OPA/terraform fetches in the root Dockerfile. The tarball lands in a
# file before it is unpacked rather than being piped into tar, because a retry
# restarts the download and tar cannot un-consume the bytes it already read.
RUN ARCH=$(dpkg --print-architecture) \
 && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
      "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" -o /tmp/helm.tar.gz \
 && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors \
      "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz.sha256sum" -o /tmp/helm.sha256 \
 && echo "$(cut -d' ' -f1 /tmp/helm.sha256)  /tmp/helm.tar.gz" | sha256sum -c - \
 && tar xzf /tmp/helm.tar.gz -C /tmp \
 && mv "/tmp/linux-${ARCH}/helm" /usr/local/bin/helm \
 && rm -rf /tmp/helm.tar.gz /tmp/helm.sha256 /tmp/linux-* \
 && helm version

# kubectl — handy for ad-hoc `command:`/`shell:` tasks; not required by k8s_info/k8s.
RUN ARCH=$(dpkg --print-architecture) \
 && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && SHA=$([ "$ARCH" = arm64 ] && echo "$KUBECTL_SHA256_ARM64" || echo "$KUBECTL_SHA256_AMD64") \
 && echo "$SHA  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod +x /usr/local/bin/kubectl \
 && kubectl version --client

# ansible-core + the DB/k8s client libraries. psycopg2-binary / PyMySQL /
# kubernetes ship pure-python or manylinux wheels; pymssql builds against the
# FreeTDS headers above when a wheel isn't available for the target arch.
# beyondtrust-bips-library backs the beyondtrust.secrets_safe lookup plugin (below)
# — it pulls requests/urllib3/cryptography/Cerberus, all pure-python or manylinux wheels.
# oracledb runs in THIN mode (pure Python, no Instant Client); it is what the Oracle
# sample drives directly, since there is no official Oracle Database collection.
# pymongo backs community.mongodb.
RUN pip install --no-cache-dir \
        "ansible-core>=2.16" \
        "kubernetes>=29.0.0" \
        "psycopg2-binary>=2.9" \
        "PyMySQL>=1.1" \
        "pymssql>=2.2" \
        "oracledb>=2.0" \
        "pymongo>=4.6" \
        "python-ldap>=3.4" \
        "pywinrm>=0.4.3" \
        "requests-ntlm>=1.2" \
        "beyondtrust-bips-library>=2.0.0,<3.0.0"

# Ansible collections for the localhost k8s/DB plays. community.general carries
# the mssql_* modules. beyondtrust.secrets_safe / beyondtrust.password_safe back the
# in-playbook Password Safe lookup + management examples (examples/playbooks/password-safe/).
# Install to a system path so any container user finds them.
RUN ansible-galaxy collection install \
        kubernetes.core \
        community.postgresql \
        community.mysql \
        community.general \
        "community.mongodb:>=1.7.0" \
        microsoft.ad \
        beyondtrust.secrets_safe \
        beyondtrust.password_safe \
        -p /usr/share/ansible/collections \
 && ansible-galaxy collection list -p /usr/share/ansible/collections

# Fail the build early if a client lib can't import or a module can't be resolved
# on the collections path — a broken image should never reach a runner.
RUN python -c "import kubernetes, psycopg2, pymysql, pymssql, oracledb, pymongo, ldap, winrm; print('client libs OK')" \
 && ansible-doc kubernetes.core.k8s_info      >/dev/null \
 && ansible-doc community.postgresql.postgresql_db >/dev/null \
 && ansible-doc community.mysql.mysql_db      >/dev/null \
 && ansible-doc community.general.mssql_db    >/dev/null \
 && ansible-doc community.mongodb.mongodb_user >/dev/null \
 && ansible-doc community.mongodb.mongodb_index >/dev/null \
 && ansible-doc community.general.ldap_entry  >/dev/null \
 && ansible-doc microsoft.ad.user             >/dev/null \
 && ansible-doc beyondtrust.secrets_safe.secrets_safe_lookup >/dev/null \
 && echo "ansible-cloud collections + client libs OK"
