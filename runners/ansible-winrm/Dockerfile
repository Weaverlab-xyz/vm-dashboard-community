# The dashboard's DEFAULT Ansible config-management runner image.
#
# It is upstream `willhallonline/ansible:latest` + `pywinrm` and the NTLM auth
# backend the sample Windows playbooks use — so Windows / WinRM targets work out of
# the box while Linux / SSH runs behave exactly as on the upstream image (it's a
# strict superset). Used by every runner the dashboard drives (local Docker sibling,
# AWS ECS, Azure ACI, GCP Cloud Run); each defaults to `chrweav/ansible-winrm:latest`
# and can be overridden per runner (ANSIBLE_LOCAL_IMAGE / ansible_ecs_image /
# ansible_aci_image / gcp_ansible_image) — but an override that lacks pywinrm breaks
# Windows targets.
#
# Published (public Docker Hub): chrweav/ansible-winrm:latest
# Build + push:
#   docker build -t chrweav/ansible-winrm:latest runners/ansible-winrm
#   docker push  chrweav/ansible-winrm:latest
# Multi-arch (amd64 + arm64), recommended so it runs on Fargate/Cloud Run/ACI and
# Apple-silicon dev hosts alike:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t chrweav/ansible-winrm:latest --push runners/ansible-winrm
FROM willhallonline/ansible:latest

# pywinrm + requests-ntlm cover the NTLM transport the sample Windows plays default
# to (ansible_winrm_transport: ntlm). Add pywinrm[credssp] if you use CredSSP.
# cryptography/pyspnego publish musllinux wheels, so no build toolchain is needed on
# the Alpine base; the final line fails the build early if the import doesn't work.
RUN pip install --no-cache-dir \
        "pywinrm>=0.4.3" \
        "requests-ntlm>=1.2.0" \
 && python -c "import winrm, requests_ntlm; print('pywinrm', winrm.__version__, 'OK')"

# BeyondTrust Password Safe Ansible collections — back the in-playbook secret/managed-account
# lookup + management examples (examples/playbooks/password-safe/). The lookup plugin runs on
# the Ansible controller (this runner), so it lives here even for SSH/WinRM VM plays. The
# dashboard auto-injects PASSWORD_SAFE_* env at run time (see docs/integrations/ansible.md).
# beyondtrust-bips-library pulls requests/urllib3/cryptography/Cerberus — all publish
# musllinux wheels, so no build toolchain is needed on Alpine. Install collections to a
# system path so any container user resolves them. The final line fails the build early if
# the lookup can't be resolved.
RUN pip install --no-cache-dir "beyondtrust-bips-library>=2.0.0,<3.0.0" \
 && ansible-galaxy collection install \
        beyondtrust.secrets_safe \
        beyondtrust.password_safe \
        -p /usr/share/ansible/collections \
 && ansible-doc beyondtrust.secrets_safe.secrets_safe_lookup >/dev/null \
 && echo "beyondtrust collections OK"
