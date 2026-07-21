# `ansible-cloud` runner image

The Ansible runner image for **Kubernetes-cluster** and **cloud-database**
Configuration-Management targets. Unlike the VM runner
([`runners/ansible-winrm/`](../ansible-winrm/), which SSHes/WinRMs *to* a host),
these targets run a **`hosts: localhost` play** that reaches *out* to the cluster
API server (via a kubeconfig) or the database endpoint (via login vars). This
image ships what those plays import:

| Collection | Client lib / binary | Sample modules |
|---|---|---|
| `kubernetes.core` | `kubernetes` + `helm` CLI | `k8s_info`, `k8s`, `helm` |
| `community.postgresql` | `psycopg2` | `postgresql_db`, `postgresql_user` |
| `community.mysql` | `PyMySQL` | `mysql_db`, `mysql_user` |
| `community.general` | `pymssql` | `mssql_db` |

`kubectl` is also included for ad-hoc `command:`/`shell:` tasks.

## Why a separate image (not `chrweav/ansible-winrm`)
The winrm image is Alpine-based and carries only the SSH/WinRM collections.
`pymssql` needs FreeTDS, which is painless on Debian/glibc but fights musl on
Alpine, and there is no reason to bloat the proven VM runner with the k8s/DB
stack. Keeping them separate lets the winrm image stay lean and untouched.

## How the dashboard uses it
The dashboard runs k8s/DB config-management jobs **only** on a remote in-cloud
runner (AWS ECS / Azure ACI / GCP Cloud Run) placed in-subnet with line-of-sight
to the private endpoint — never the local sibling-Docker path (see
[docs/config-management.md](../../docs/config-management.md)). The runner shell
decodes the playbook + connection material from env vars and runs:

```
ansible-playbook -i 'localhost,' -c local /tmp/playbook.yml [-e @/tmp/conn_vars.json]
```

Select this image with the `ansible_cloud_image` config key
(`ANSIBLE_CLOUD_IMAGE`); it defaults to `chrweav/ansible-cloud:latest` and is
used for all three cloud runners for k8s/DB targets.

## Build + push
```
docker build -t chrweav/ansible-cloud:latest runners/ansible-cloud
docker push  chrweav/ansible-cloud:latest
```
Multi-arch (amd64 + arm64), so it runs on Fargate/Cloud Run/ACI and Apple-silicon
dev hosts alike:
```
docker buildx build --platform linux/amd64,linux/arm64 \
  -t chrweav/ansible-cloud:latest --push runners/ansible-cloud
```
The runner pulls this image **in-cloud** (public Docker Hub, or mirror it to
ECR/ACR/Artifact Registry), so the corporate TLS-inspecting proxy never touches
the pull or the Ansible→endpoint traffic.

## Smoke-test locally (no dashboard)
```
# Kubernetes (against a kind/k3d cluster)
docker run --rm -v "$PWD:/pb" -e K8S_AUTH_KUBECONFIG=/pb/kubeconfig \
  chrweav/ansible-cloud ansible-playbook -i 'localhost,' -c local \
  /pb/examples/playbooks/k8s/list-nodes.yml

# Database (against a throwaway postgres container)
docker run --rm -v "$PWD:/pb" chrweav/ansible-cloud ansible-playbook \
  -i 'localhost,' -c local /pb/examples/playbooks/database/postgres-create-database.yml \
  -e @/pb/conn_vars.json -e target_db_name=demo
```
