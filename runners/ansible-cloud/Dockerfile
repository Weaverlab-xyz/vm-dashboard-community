# The dashboard's Ansible runner image for KUBERNETES-CLUSTER and CLOUD-DATABASE
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
FROM python:3.12-slim

ARG HELM_VERSION=v3.16.3
ARG KUBECTL_VERSION=v1.31.3

# FreeTDS for pymssql (the PyPI wheels bundle it, but keep the runtime libs +
# build headers so a source build also succeeds on any arch); curl/tar to fetch
# the helm + kubectl binaries; ca-certificates for TLS to the cluster/DB.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        gcc \
        freetds-dev \
        freetds-bin \
        curl \
        tar \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# helm CLI — required by the kubernetes.core.helm module (it shells out to helm).
RUN curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-$(dpkg --print-architecture).tar.gz" \
      | tar xz -C /tmp \
 && mv "/tmp/linux-$(dpkg --print-architecture)/helm" /usr/local/bin/helm \
 && rm -rf /tmp/linux-* \
 && helm version

# kubectl — handy for ad-hoc `command:`/`shell:` tasks; not required by k8s_info/k8s.
RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(dpkg --print-architecture)/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && kubectl version --client

# ansible-core + the DB/k8s client libraries. psycopg2-binary / PyMySQL /
# kubernetes ship pure-python or manylinux wheels; pymssql builds against the
# FreeTDS headers above when a wheel isn't available for the target arch.
RUN pip install --no-cache-dir \
        "ansible-core>=2.16" \
        "kubernetes>=29.0.0" \
        "psycopg2-binary>=2.9" \
        "PyMySQL>=1.1" \
        "pymssql>=2.2"

# Ansible collections for the localhost k8s/DB plays. community.general carries
# the mssql_* modules. Install to a system path so any container user finds them.
RUN ansible-galaxy collection install \
        kubernetes.core \
        community.postgresql \
        community.mysql \
        community.general \
        -p /usr/share/ansible/collections \
 && ansible-galaxy collection list -p /usr/share/ansible/collections

# Fail the build early if a client lib can't import or a module can't be resolved
# on the collections path — a broken image should never reach a runner.
RUN python -c "import kubernetes, psycopg2, pymysql, pymssql; print('client libs OK')" \
 && ansible-doc kubernetes.core.k8s_info      >/dev/null \
 && ansible-doc community.postgresql.postgresql_db >/dev/null \
 && ansible-doc community.mysql.mysql_db      >/dev/null \
 && ansible-doc community.general.mssql_db    >/dev/null \
 && echo "ansible-cloud collections + client libs OK"
