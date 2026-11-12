# UI robot: the few steps the PRA / Password Safe APIs cannot do (see README.md).
# The Playwright image ships Chromium and its system libraries; pin the tag to the
# playwright version in requirements.txt or the browser build will not match.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# The base image lags on OS and Python package fixes, and the image scan blocks a fixable HIGH
# or CRITICAL. Take the distro's security updates, and remove the base's virtualenv 21.14.7:
# the robot never creates a virtualenv, and upgrading it left a copy the scan still found.
# Dropping the package is not enough on its own. The base also ran `virtualenv venv`, which
# leaves a seed cache under /root/.cache holding an UNPACKED pip wheel, and the scanner reads
# that pip's vendored-dependency inventory as if those were packages of this image. The cache,
# not this image, is where the certifi 2026.6.17, urllib3 2.7.0, msgpack 1.1.2 and setuptools
# 70.3.0 findings came from; the certifi and urllib3 actually installed here arrive with
# requests, already past their advisories. 24 MB, and nothing reads it at run time.
# The distro's updated python3 ships a PEP 668 marker the base image did not have, which
# makes every system-wide pip install refuse. This is a single-purpose image, so say so once.
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN apt-get update \
 && apt-get -y --no-install-recommends upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && find / -xdev \( -name 'virtualenv' -o -name 'virtualenv-*.dist-info' \) -path '*-packages/*' \
      -prune -exec rm -rf {} + \
 && rm -rf /root/.cache

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# filelock came in as virtualenv's dependency and outlived it -- python-discovery still
# requires it, so it is upgraded rather than removed with the rest. 4.2.0 is a fixable HIGH,
# and pip leaves an already-satisfying version alone, so the install above does not move it
# however current requirements.txt is: it has to be named. A floor so the next security
# release is taken on the following build, capped at the next major.
RUN pip install --no-cache-dir --upgrade 'filelock>=4.2.1,<5'

# Nothing is installed at run time -- the robot is a fixed set of wheels and runs as an
# unprivileged uid -- so pip comes out once the installs above are done. It is the last
# thing here carrying somebody else's dependency inventory: pip reports the libraries it
# vendored, and the scanner reads that inventory as this image's own packages even where
# the code is absent, which is how a setuptools finding reached an image whose
# `import setuptools` raises ModuleNotFoundError. Taking pip out removes the inventory and
# the vendored copies together, and leaves nothing that could install at run time.
RUN python -m pip uninstall -y pip

COPY robot ./robot

# Downloads land here; mount a volume if you want to keep them.
ENV ROBOT_DOWNLOAD_DIR=/work
# 10001 is the uid of the control node's SPIFFE helper, which owns the SVID volume the agent
# mounts here read-only (mode 0700). /opt/job is where the agent extracts the sealed bundle.
RUN mkdir -p /work /opt/job && chown 10001:10001 /work /opt/job
USER 10001
ENTRYPOINT ["python", "-m", "robot"]
