# Ansible runner image (`ansible-winrm`) — the dashboard's default

This builds the dashboard's **default** Ansible config-management runner image,
published at **`chrweav/ansible-winrm:latest`**. It is upstream
`willhallonline/ansible:latest` **plus** [`pywinrm`](https://pypi.org/project/pywinrm/)
and the NTLM auth backend the sample Windows playbooks use.

Why: upstream `willhallonline/ansible` does **not** bundle `pywinrm`, so a Windows /
WinRM target would fail with *"pywinrm is not installed"*. Shipping the default with
pywinrm makes Windows work out of the box on every runner (local + ECS / ACI / Cloud
Run), while Linux / SSH runs behave exactly as on the upstream image — it's a strict
superset. All four `*_image` settings default to `chrweav/ansible-winrm:latest`.

The Publish images GitHub Actions workflow builds + pushes this multi-arch alongside
the app and promote-runner images; build it yourself with the commands below.

## Build & push

```bash
# single-arch
docker build -t chrweav/ansible-winrm:latest runners/ansible-winrm
docker push  chrweav/ansible-winrm:latest

# multi-arch (recommended — Fargate/Cloud Run/ACI are amd64, Apple-silicon dev is arm64)
docker buildx build --platform linux/amd64,linux/arm64 \
  -t chrweav/ansible-winrm:latest --push runners/ansible-winrm
```

## Use it

It's already the **default** for all four runner image settings, so nothing to do
for the common case. These are the keys if you want to override the image (an
override without pywinrm breaks Windows targets):

| Runner | Setting |
|---|---|
| Local Docker | `ANSIBLE_LOCAL_IMAGE` / `ansible_local_image` |
| AWS ECS | `ansible_ecs_image` |
| Azure ACI | `ansible_aci_image` |
| GCP Cloud Run | `gcp_ansible_image` |

## Notes

- **NTLM vs CredSSP.** The sample Windows plays set `ansible_winrm_transport: ntlm`,
  covered by `requests-ntlm`. If you need CredSSP, add `pywinrm[credssp]` to the
  Dockerfile's `pip install`.
- **A Windows run still needs more than the image:** the target must have WinRM
  listening (5985/5986) and reachable from the runner (security-group / firewall
  rules; GCP Cloud Run needs the VPC connector), and the admin credential must be
  supplied — via the **Use a secret** panel (a cloud-store secret / managed account
  on ECS/GCP, inline on ACI). See
  [docs/integrations/ansible.md](../../docs/integrations/ansible.md).
- **Base pin.** The Dockerfile tracks `willhallonline/ansible:latest`; pin a specific
  upstream tag if you want reproducible rebuilds.
