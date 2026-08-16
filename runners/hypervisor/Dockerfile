# One-shot hypervisor runner for the remote agent.
#
# The agent image installs `requests`, `PyYAML` and `cryptography` and nothing else,
# and two tests enforce that — the restraint IS the security argument, so it is not
# traded away for a capability. Four of the five hypervisors need nothing more, because
# Prism v3, Proxmox's /api2/json and the vSphere Automation REST API are plain JSON and
# XAPI is stdlib xmlrpc.
#
# Two are not reachable that way:
#   * Hyper-V   — WinRM is SOAP with NTLM/Negotiate, and hand-rolling NTLM is a bad idea
#   * bare ESXi — serves only the SOAP API; the Automation REST API is vCenter-only
#
# So they run HERE instead: a one-shot container the agent launches per job, which exits
# when the work is done. That keeps the long-lived supervisor inert — it still imports no
# execution machinery — and confines the heavy dependencies to a process that lives for
# seconds.
#
# The cost is real and is documented rather than hidden: launching this needs the Docker
# socket, which is root on the host. See examples/remote-agent/docker-compose.sibling.yml
# and docs/remote-agents.md#the-sibling-runner. The agent never mounts it by default.
#
# Published (public Docker Hub): chrweav/hypervisor-runner:latest
# Build + push:
#   docker build -t chrweav/hypervisor-runner:latest runners/hypervisor
#   docker push  chrweav/hypervisor-runner:latest
FROM python:3.12-slim

# pywinrm[kerberos] is deliberately NOT installed: Kerberos needs a system krb5 stack
# and the dashboard's own Hyper-V integration defaults to NTLM.
# pyvmomi is the only way to speak to a bare ESXi host at all.
RUN pip install --no-cache-dir \
        "pywinrm>=0.4.3" \
        "requests-ntlm>=1.2.0" \
        "pyvmomi>=8.0.2.0" \
 && python -c "import winrm, requests_ntlm, pyVim.connect; print('runner deps OK')"

WORKDIR /app
COPY run.py /app/run.py

# Same unprivileged uid as the agent. The agent additionally sets --read-only, drops
# every capability and disables privilege escalation on the container it creates; those
# are set at create time, not baked in, so they cannot be dropped by rebuilding this
# image with a different USER line.
RUN useradd -u 10001 -m runner
USER 10001

# No ENTRYPOINT shell wrapper: the agent passes argv directly, so there is no place for
# a shell to expand anything it was given.
ENTRYPOINT ["python", "/app/run.py"]
