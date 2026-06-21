# Entitle resource registration — design

## Context

The dashboard registers each resource it builds as an Entitle **integration** so
end-users request just-in-time access in Entitle, instead of Entitle gating the
dashboard's own actions (the former approval gate, now removed). Implemented by
[`entitle_registration_service.py`](../../web_dashboard/services/entitle_registration_service.py)
via the `entitleio/entitle` (v3) Terraform provider, hooked into the AWS/Azure/GCP
VM deploy paths (shared [`entitle_vm_hook.py`](../../web_dashboard/services/entitle_vm_hook.py))
and the cloud-database provisioning flow. See [`../integrations/entitle.md`](../integrations/entitle.md)
for the operator view; this doc covers the architecture + the two open build-outs
(agent cluster, K8s integrations).

## Resource → integration mapping

| Built resource | Entitle `application` | `connection_json` keys |
|---|---|---|
| Linux VM (AWS/Azure/GCP) | `ssh` (ephemeral accounts) | host, port, user, privateKey |
| PostgreSQL DB | `postgresql` | host, port, database, username, password |
| MySQL DB | `mysql` | host, port, username, password |
| SQL Server DB | `mssql` | host, port, username, password |
| _(future)_ EKS/AKS/GKE | Kubernetes | per provider |

Each integration also sets the required `owner = {id}`, `workflow = {id}`,
`allowed_durations`, and — for **private** targets — `agent_token = {name}`.
Application slugs are best-effort (`postgresql` confirmed); verify against the
`entitle_applications` data source per tenant.

## Public vs. private connectivity

- **Public** target (reachable from Entitle's cloud) → no `agent_token`, no agent.
- **Private** target (private RDS, PRA-only VMs) → reached through the **Entitle
  Agent**, which is **Kubernetes-only / Helm-installed** (no Docker/ECS path). One
  agent per VPC/network is shared by all private integrations, referenced by
  `entitle_agent_token_name`.

The VM hook derives public/private from the deploy (`public_ip` / `create_public_ip`
/ `create_external_ip`); RDS is private-only. Private registration raises a clear
error (non-fatal) if no agent is configured.

## Agent cluster bootstrap (validated against the K8s feature)

A one-time **admin prerequisite**, off the per-build critical path, built as
**composition of existing `k8s_service` primitives** — not new infrastructure.
Decisions (validated 2026-06-21):

1. **Dedicated dashboard-provisioned EKS cluster** whose job is hosting the agent,
   stood up via the existing `k8s_service.create_cluster` (`terraform/k8s_cluster/aws_eks`;
   kubeconfig stored as a secrets-backend ref). One agent per VPC/network. The cluster
   also becomes an Entitle **Kubernetes integration** later — coordinate with the
   existing Entitle/K8s wiring (`_entitle_rancher_grant` Rancher-RBAC JIT + the PRA
   `tunnel_type=k8s` jump), don't duplicate.
2. **Install surface = a new management-plane kind**: `setup_entitle_agent(cluster_id)`
   mirroring `setup_secret_delivery`, dispatched as `mgmt_kind="entitle_agent"` via the
   existing management endpoint + `run_management_plane` worker job. The "Provision
   Entitle agent" admin action is just that POST (decoupled; the ~10-min cluster
   spin-up happens once).
3. Per-build registration stays **opt-in (default off)** and is greyed out until the
   capability + agent are ready.

### Agent token — server-side Secret + Helm (reuses the runner primitives)

The agent token is sensitive and returned **only at creation**. Mint it with the
`entitle_agent_token` Terraform resource (or the API) and stash it in the dashboard's
secrets backend, recorded as `entitle_agent_token_ref`:

```hcl
resource "entitle_agent_token" "agent" { name = var.agent_name }   # .token is sensitive
```

`setup_entitle_agent` then, using the **same primitives** the ESO/management installs
already use:

1. **Resolve** the token value server-side from `entitle_agent_token_ref`
   (`config_service`), never persisting it on a row.
2. **Apply** a K8s `Secret` (`ENTITLE_TOKEN`) via `_apply_manifest_via_runner` — the
   token rides in a tmpdir manifest mounted into the one-shot kubectl container, gone
   when it exits.
3. **Install** the agent via `_helm_via_runner(["upgrade","--install","entitle-agent",
   <chart>, …])` referencing that Secret.

Net: the token lives only in the secrets backend + the in-cluster Secret — never in
Terraform state, Helm `--set`, or `helm get values`.

> **Verification gate (load-bearing for step 2/3):** confirm the `entitle-agent` chart
> reads the token from an **existing Secret** (`secretKeyRef`/`ENTITLE_TOKEN`) rather
> than only `--set agent.token=...`. The provider's `kubernetes_secret { ENTITLE_TOKEN }`
> example implies it does. If it only accepts `--set`, resolve the token and pass it as
> a `--set` arg from `_helm_via_runner` (still server-side-resolved, not persisted).

**Alternative (GitOps/rotation):** a cloud-native ESO `SecretStore` (`aws_sm` /
`azure_kv` / `gcp_sm`) + `ExternalSecret` syncing the token in. Note the feature's
existing ESO `ClusterSecretStore` is **BeyondTrust/Password-Safe-specific**, so this is
a *new* store manifest, not the existing one — hence not the default here.

Distinguish three "token" concepts: **token value** (secret — above);
`entitle_agent_token_name` (just the **identifier**, consumed by
`entitle_registration_service` as `agent_token = {name}`); and the chart's `kmsType`
(where the running agent vaults *integration* creds — default `kubernetes_secret_manager`,
in-cluster).

## SSH key sourcing — from the VM's own keypair, not config

The SSH private key Entitle uses for the **SSH ephemeral-accounts** integration is
the counterpart of the key cloud-init injected into the VM at build time. It is
**not** a separately-configured Entitle key. Source it per-cloud from the dashboard's
existing SSH keypair material:

| Cloud | Public key (cloud-init) | Private key resolver | Status |
|---|---|---|---|
| Azure | `azure_ssh_keypair_secret_name` (JSON `{public_key, private_key}`) | `azure_service.resolve_azure_ssh_private_key` | clean today |
| AWS | `ec2_ssh_key_secret` | `aws_service.get_keypair_private_key` → `ec2/keypairs/<name>` convention | needs the `.pem` stored at the convention path |
| GCP | `gcp_ssh_key_secret_name` | `gcp_service.get_ssh_private_key` | clean when the secret is a JSON `{public_key, private_key}` keypair |

`entitle_vm_hook.register` accepts a caller-resolved `private_key` + `sudo_user`; each
deploy path passes the key resolved the same way its "get VM SSH key" endpoint does.
`entitle_ssh_private_key_ref` is demoted to an **optional global fallback/override**
(default empty), not the primary source. `sudo_user` is the cloud-default user
cloud-init set up with the injected key + passwordless sudo (the
`provisioners/beyondtrust/` bt-ready user); `entitle_ssh_sudo_user` overrides it.

## Open items
- Confirm `entitle-agent` chart secret-based token support (ESO gate above).
- AWS: track the per-deploy keypair name so the private key resolves from `ec2/keypairs/<name>` instead of the optional override.
- Wire EKS/AKS/GKE clusters as Entitle Kubernetes integrations (the agent cluster qualifies first).
