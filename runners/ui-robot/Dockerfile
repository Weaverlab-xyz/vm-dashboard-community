# UI robot: the few steps the PRA / Password Safe APIs cannot do (see README.md).
# The Playwright image ships Chromium and its system libraries; pin the tag to the
# playwright version in requirements.txt or the browser build will not match.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# The base image lags on OS and Python package fixes, and the image scan blocks a fixable HIGH
# or CRITICAL. Take the distro's security updates, and a virtualenv new enough to carry its
# fixes (the base ships 20.27.1). Both are re-evaluated on every rebuild.
RUN apt-get update \
 && apt-get -y --no-install-recommends upgrade \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --break-system-packages --ignore-installed "virtualenv>=21.14.9"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY robot ./robot

# Downloads land here; mount a volume if you want to keep them.
ENV ROBOT_DOWNLOAD_DIR=/work
# 10001 is the uid of the control node's SPIFFE helper, which owns the SVID volume the agent
# mounts here read-only (mode 0700). /opt/job is where the agent extracts the sealed bundle.
RUN mkdir -p /work /opt/job && chown 10001:10001 /work /opt/job
USER 10001
ENTRYPOINT ["python", "-m", "robot"]
