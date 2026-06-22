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
2. **Install surface = a secret-delivery-shaped action** (implemented):
   `POST /api/k8s/clusters/{id}/entitle-agent` enqueues a `k8s_entitle_agent` Job that
   `jobs_worker` dispatches to `k8s_service.run_entitle_agent` → `setup_entitle_agent`.
   This mirrors `setup_secret_delivery` rather than the management plane — the
   management-plane runner is coupled to Portainer endpoint registration (`status=managed`),
   whereas installing the agent is a tracked in-cluster install. The "Provision Entitle
   agent" admin action is just that POST (decoupled; the ~10-min cluster spin-up happens
   once, via the existing `clusters/provision` flow).
3. Per-build registration stays **opt-in (default off)** and is greyed out until the
   capability + agent are ready.

### Agent token — server-side Secret + Helm (reuses the runner primitives)

The agent token is sensitive and returned **only at creation**. `setup_entitle_agent`
**auto-mints** it on first install via `entitle_registration_service.ensure_agent_token`
(the `entitle_agent_token` Terraform resource below, using the provider key), stashes the
value in the dashboard's secrets backend, and records `entitle_agent_token_ref`
(→ `config://entitle/agent-token`) + `entitle_agent_token_name`. Pre-set
`entitle_agent_token_ref` yourself only to use an externally-minted token:

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
Terraform state, Helm `--set`, or `helm get values`. (Both paths land a native K8s
Secret in-cluster — the agent authenticates from it; the difference is how it gets
there and who keeps it in sync.)

**Rotation:** re-run `setup_entitle_agent` to push a fresh token — it re-resolves from
the backend, re-applies the `Secret`, and the agent picks up the new value. This is the
manual/imperative path; the ESO alternative below rotates automatically (update the
value in the external store → ESO reconciles the in-cluster Secret on its refresh
interval). **Both are supported** — pick per environment; the server-side path is
simpler, ESO is hands-off for rotation.

> **Chart token mechanism (confirmed):** the published `entitle/entitle-agent` chart
> (`helm repo add entitle https://anycred.github.io/entitle-charts/`) takes the token
> only as a plaintext Helm value (`agent.token`) — there is **no** `existingSecret`/secretKeyRef
> option. So the default is the plaintext-value path (`entitle_agent_token_plaintext_helm_key=agent.token`):
> still resolved server-side. The token is supplied as a Helm **values doc streamed over
> stdin** (`helm … -f -`), **not** `--set-string`, so it never appears in the runner's
> process args; it does still land in the in-cluster **Helm release Secret** (unavoidable
> with this chart). The apply-Secret path is retained behind config for a future chart
> version. ESO doesn't help here (nothing external to sync *from* — we hold the token).

**ESO does NOT fit the token, and not the agent's integration secrets either.** ESO is a
*consumer-side pull* (external store → K8s Secret for a workload to mount). But the agent
**produces and owns** its integration secrets (per-integration connection creds), writing
them to whatever **`kmsType`** points at and reading them back directly — there's no
external store for ESO to pull from. The lever for those secrets is `kmsType`
(`entitle_agent_kms_type`): default `kubernetes_secret_manager` (native K8s Secrets in
etcd), or `aws_secret_manager` / `azure_secret_manager` / `gcp_secret_manager` /
`hashicorp_vault` to keep them outside the cluster. To harden etcd instead, use EKS
envelope/KMS encryption of secrets-at-rest.

Distinguish three concepts: **agent token value** (auth to Entitle's control plane —
plaintext `--set`, above); `entitle_agent_token_name` (just the **identifier**, consumed by
`entitle_registration_service` as `agent_token = {name}`); and **`kmsType`** (where the
running agent vaults its **integration** creds — governed by `entitle_agent_kms_type`, not ESO).

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

## Status / open items
- **Agent bootstrap implemented** — `setup_entitle_agent`/`run_entitle_agent` (k8s_service),
  `POST /api/k8s/clusters/{id}/entitle-agent`, `k8s_entitle_agent` worker dispatch, config keys.
  The token is **auto-minted** on first install (`ensure_agent_token` → `mint_agent_token`) — no manual token step.
- **Verification gate (before first real install):** confirm `entitle_agent_chart_repo`
  (the chart's Helm repo URL) and `entitle_agent_existing_secret_helm_key` (the Helm value
  that points at the token Secret) against the published chart; set
  `entitle_agent_token_plaintext_helm_key` if the chart only takes a plaintext token.
- UI: an "Install Entitle agent" action on the cluster page (mirrors secret-delivery) — TODO.
- AWS private-key sourcing is **secret-based by design** — registration resolves it from
  the chosen `ec2_ssh_key_secret` (a JSON `{public_key, private_key}` keypair) or the
  optional override. The `ec2/keypairs/<name>` convention is a *separate manual path*
  used by `get_instance_ssh_key` for EC2-KeyPair instances, not the userdata-injected
  flow — so there's no per-deploy keypair name to track here (earlier open item retired).
- **K8s cluster registration implemented** — `register_kubernetes` (entitle_registration_service) +
  `register_cluster_in_entitle`/`run_entitle_register` (k8s_service), `POST /api/k8s/clusters/{id}/entitle-register`,
  `k8s_entitle_register` worker, "Register in Entitle" cluster button. Generic **Kubernetes** app:
  In-Cluster via the agent for private API clusters, else a minted least-priv ServiceAccount (External
  Access). Open: scope the ServiceAccount ClusterRole down from cluster-admin once the required perms are
  confirmed; the GKE-specific GCP-IAM integration is a separate optional path.
- **Sample workloads** for managed clusters live in [`examples/k8s/`](../../examples/k8s/)
  (the community counterpart to `examples/compose/` + `examples/playbooks/`): namespaced,
  `restricted`-PSS-compliant Deployment/Service/Ingress/HPA/ConfigMap/Secret/StatefulSet/
  CronJob/quota/NetworkPolicy starters, validated by `tests/test_k8s_samples.py`.
