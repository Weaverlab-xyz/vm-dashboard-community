# UI robot: the few steps the PRA / Password Safe APIs cannot do (see README.md).
# The Playwright image ships Chromium and its system libraries; pin the tag to the
# playwright version in requirements.txt or the browser build will not match.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# The base image lags on OS and Python package fixes, and the image scan blocks a fixable HIGH
# or CRITICAL. Take the distro's security updates, and remove the base's virtualenv 20.27.1:
# the robot never creates a virtualenv, and upgrading it left a copy the scan still found.
# The distro's updated python3 ships a PEP 668 marker the base image did not have, which
# makes every system-wide pip install refuse. This is a single-purpose image, so say so once.
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN apt-get update \
 && apt-get -y --no-install-recommends upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && find / -xdev \( -name 'virtualenv' -o -name 'virtualenv-*.dist-info' \) -path '*-packages/*' \
      -prune -exec rm -rf {} +

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The base image preinstalls these five, and pip leaves an already-satisfying version
# alone, so the install above never moves them no matter what requirements.txt asks for --
# they have to be named. Each one is a fixable HIGH or CRITICAL that the image scan blocks
# on (certifi's is CRITICAL), and .trivyignore.yaml asks for a package upgrade in
# preference to an exception entry. requests reaches the network through certifi and
# urllib3; the other three are base-image tooling the robot never imports. Floors so a
# later security release is picked up on the next build, capped at the next major so a
# fresh build cannot float past what requests supports.
RUN pip install --no-cache-dir --upgrade \
      'certifi>=2026.10.18' \
      'filelock>=4.2.1,<5' \
      'msgpack>=1.2.3,<2' \
      'setuptools>=78.1.1,<80' \
      'urllib3>=2.8.0,<3'

COPY robot ./robot

# Downloads land here; mount a volume if you want to keep them.
ENV ROBOT_DOWNLOAD_DIR=/work
# 10001 is the uid of the control node's SPIFFE helper, which owns the SVID volume the agent
# mounts here read-only (mode 0700). /opt/job is where the agent extracts the sealed bundle.
RUN mkdir -p /work /opt/job && chown 10001:10001 /work /opt/job
USER 10001
ENTRYPOINT ["python", "-m", "robot"]
