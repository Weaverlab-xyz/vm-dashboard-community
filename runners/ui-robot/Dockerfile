# UI robot: the few steps the PRA / Password Safe APIs cannot do (see README.md).
# The Playwright image ships Chromium and its system libraries; pin the tag to the
# playwright version in requirements.txt or the browser build will not match.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# The base image lags on OS and Python package fixes, and the image scan blocks a fixable HIGH
# or CRITICAL. Take the distro's security updates, and remove the base's virtualenv 21.14.7:
# the robot never creates a virtualenv, and upgrading it left a copy the scan still found.
# Dropping the package is not enough on its own. The base also ran `virtualenv venv`, which
# leaves a seed cache under /root/.cache holding an UNPACKED pip wheel, and the scanner reads
# that pip's _vendor/vendor.txt as if those were installed packages. That file, not this image,
# is where the certifi, urllib3, msgpack and setuptools findings come from: the image has no
# setuptools or msgpack at all, and the certifi and urllib3 it does have arrive with requests,
# already past their advisories. 24 MB, and nothing reads it at run time.
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

# Two leftovers the scan still blocks on once the seed cache is gone, neither of them
# reachable by upgrading requirements.txt, because pip leaves an already-satisfying version
# alone -- both have to be named:
#   * filelock came in as virtualenv's dependency and outlived it (python-discovery still
#     requires it, so it is upgraded rather than removed with the rest); 4.2.0 is a fixable HIGH.
#   * pip's OWN _vendor/vendor.txt is read by the scanner as if those were installed packages.
#     The base's pip 26.2.2 vendors msgpack 1.1.2 and urllib3 2.7.0, both fixable; 26.3 moved
#     them to 1.2.4 and 2.8.0. The setuptools line it still carries is accepted in
#     .trivyignore.yaml -- no setuptools is vendored or installed here, only pkg_resources.
# Floors so the next security release is taken on the following build, filelock capped at the
# next major. Do not add setuptools: every version in range vendors its own flagged
# jaraco.context and wheel copies, which is a trade of one finding for two.
RUN pip install --no-cache-dir --upgrade 'pip>=26.3' 'filelock>=4.2.1,<5'

COPY robot ./robot

# Downloads land here; mount a volume if you want to keep them.
ENV ROBOT_DOWNLOAD_DIR=/work
# 10001 is the uid of the control node's SPIFFE helper, which owns the SVID volume the agent
# mounts here read-only (mode 0700). /opt/job is where the agent extracts the sealed bundle.
RUN mkdir -p /work /opt/job && chown 10001:10001 /work /opt/job
USER 10001
ENTRYPOINT ["python", "-m", "robot"]
