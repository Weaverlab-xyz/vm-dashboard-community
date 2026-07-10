# Promote-runner image — transient task that converts and uploads a VM
# disk image between cloud storage backends as part of cross-cloud
# image promotion. Launched as an ECS Fargate task / Azure ACI container
# / GCP Cloud Run job by the dashboard. Stays slim so cold-start is
# fast and the runtime footprint is bounded.
FROM python:3.12-slim

# libguestfs (virt-customize) offline-edits the disk image so an Azure-target
# promotion can inject the Azure Linux Agent (waagent) into a foreign Linux
# image (see install_linux_agent in entrypoint.py). It runs its own qemu
# appliance in userspace, so it needs a kernel package and works in an
# unprivileged container (no /dev/kvm) via the TCG "direct" backend.
# linux-image is arch-matched so the multi-arch (amd64 + arm64) build works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         qemu-utils \
         ca-certificates \
         curl \
         libguestfs-tools \
         "linux-image-$(dpkg --print-architecture)" \
    && rm -rf /var/lib/apt/lists/*

# No /dev/kvm in ECS Fargate / ACI / Cloud Run — force libguestfs to run its
# appliance directly under software-emulated qemu instead of trying to talk to
# libvirt (which isn't present).
ENV LIBGUESTFS_BACKEND=direct

# Bundle the WALinuxAgent source so the guest install is distro-agnostic and
# needs NO guest network or package repo — it installs via the guest's own
# Python. This matters because foreign images (e.g. an Amazon-Linux AMI) often
# don't carry waagent in any configured repo. Pin to a stable release tag.
# Also vendor setuptools into _vendor/: WALinuxAgent's setup.py does
# `import setuptools`, but minimal cloud images (e.g. Debian 13) ship Python
# without it. The vendored copy rides along in the --copy-in and is used only
# when the guest lacks its own (see entrypoint.install_linux_agent). Pin <80:
# setuptools 80 removed the `setup.py install` command --register-service uses.
ARG WALINUXAGENT_VERSION=2.15.0.1
RUN curl -fsSL \
      "https://github.com/Azure/WALinuxAgent/archive/refs/tags/v${WALINUXAGENT_VERSION}.tar.gz" \
      -o /tmp/waagent.tar.gz \
    && mkdir -p /opt/walinuxagent-src \
    && tar -xzf /tmp/waagent.tar.gz -C /opt/walinuxagent-src --strip-components=1 \
    && rm /tmp/waagent.tar.gz \
    && pip install --no-cache-dir --target /opt/walinuxagent-src/_vendor "setuptools<80"

# Cloud SDKs cover all three targets so a single image works for AWS / Azure / GCP.
RUN pip install --no-cache-dir \
        "boto3>=1.34" \
        "azure-identity>=1.15" \
        "azure-storage-blob>=12.19" \
        "google-cloud-storage>=2.14"

COPY entrypoint.py /entrypoint.py
RUN chmod +x /entrypoint.py

ENTRYPOINT ["python", "/entrypoint.py"]
