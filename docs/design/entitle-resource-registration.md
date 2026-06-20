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

## Agent cluster bootstrap (deferred — reuses the EKS flow)

A one-time **admin prerequisite**, off the per-build critical path. Decisions:

1. **Dashboard-provisioned** dedicated agent cluster via the in-progress EKS flow
   (later AKS/GKE). The cluster also becomes an Entitle **Kubernetes integration**
   (the future phase) — same artifact, double duty.
2. **Explicit admin action** ("Provision Entitle agent"), decoupled long-running
   job. The ~10-min spin-up happens once per environment; registration just checks
   the agent exists.
3. Per-build registration stays **opt-in (default off)** and is greyed out until
   the capability + agent are ready.

### Agent token — pass it as a secret (via External Secrets Operator)

The agent token is a sensitive credential, returned **only at creation**. The
provider exposes it as a first-class resource:

```hcl
resource "entitle_agent_token" "agent" { name = var.agent_name }   # .token is sensitive
```

**Recommended (ESO):** the cluster already runs External Secrets Operator. Keep the
token out of Terraform state and Helm values entirely:

1. Mint the token (`entitle_agent_token` or the API) and write it to the external
   store the dashboard already integrates (`aws_sm://` / `azure_kv://` / `gcp_sm://`),
   recorded as `entitle_agent_token_ref`.
2. An `ExternalSecret` (via the existing `ClusterSecretStore`) materializes the
   in-cluster `entitle-agent-token` Secret with key `ENTITLE_TOKEN` — the key the
   provider's own Kubernetes example uses.
3. The `entitle-agent` chart consumes that Secret.

Net: the token lives only in the external store + the ESO-managed in-cluster Secret
— never in TF state, Helm `--set`, or `helm get values`.

**Fallback (no ESO / chart only takes plaintext):** drive the Helm release from
Terraform with `set_sensitive { name = "agent.token", value = entitle_agent_token.agent.token }`
so it stays out of plan output (lives in sensitive state + the Helm-managed secret).

> **Verification gate:** confirm the `entitle-agent` chart can read the token from an
> existing Secret (`secretKeyRef`/`ENTITLE_TOKEN`) vs. only the documented
> `--set agent.token=...`. The provider's `kubernetes_secret { ENTITLE_TOKEN }`
> example implies the former.

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
