# Entitle Integration

The Entitle integration has three independent tracks that share one Entitle
tenant + API token. You can enable any combination:

| Track | What it does | Default |
|---|---|---|
| **Resource registration** (this doc) | As the dashboard builds Linux VMs and cloud databases, it registers each as an Entitle integration so users request **just-in-time access** in Entitle. | Off |
| **Machine-identity JIT** | Short-TTL, auto-approved cloud-credential elevations for the dashboard's own privileged cloud calls. | Off |
| **Dashboard permissions** | Time-boxed permissions **inside the dashboard**, granted by Entitle. Two mechanisms: **REST** (current — any user, takes effect immediately) and **Entra groups** (legacy — Entra only, takes effect at next login). See [entitle-dashboard-permissions.md](entitle-dashboard-permissions.md). | Off |

> The previous **approval-gate** track (which blocked dashboard actions behind an
> Entitle approval + webhook) has been **removed**. Secret read/update/delete are
> now admin-only; cloud deploys run without an approval round-trip.

---

## Resource registration

When enabled, each resource the dashboard provisions is registered as its own
**`entitle_integration`** via the [`entitleio/entitle`](https://registry.terraform.io/providers/entitleio/entitle/latest)
Terraform provider:

| Resource built | Entitle integration |
|---|---|
| Linux cloud VM | **SSH ephemeral accounts** ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-ssh_ephemeral_accounts)) |
| PostgreSQL database | **PostgreSQL** ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-postgressql)) |
| MySQL database | **MySQL** ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-mysql)) |
| SQL Server database | **Microsoft SQL Server** ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-microsoft-sql-server)) — **not currently registerable on any managed flavor**, see below |
| Kubernetes cluster (EKS / AKS / GKE, and Rancher-managed) | **Kubernetes** ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-kubernetes)) — see [Kubernetes clusters](#kubernetes-clusters) |

> **Managed SQL Server is gated off.** The Entitle SQL Server connector needs
> `sysadmin` / `CONTROL SERVER`, which RDS-standard, Azure SQL Database and Cloud SQL
> for SQL Server cannot grant. `_ENTITLE_VIABLE_SQLSERVER_PROVIDERS` in
> `cloud_database_service` is empty today, so all three managed flavors are refused up
> front — the API returns an error before touching the database, and the UI hides the
> checkbox via the row's `entitle_viable` flag. Adding a provider to that set flips it
> back on; the truth table is pinned by `tests/test_entitle_viability.py`.

> **Registered databases can't be onboarded either.** A database added with *Register
> existing* (`source="registered"`) has no provisioning job and no Terraform state, so
> there is no admin credential to give the connector — its Password Safe managed account
> is checked out at run time and never stored. `_entitle_ineligible_reason` refuses those
> rows first, whatever the engine, with a message naming the missing credential rather
> than the SQL Server one above.

Registration is done by [`entitle_registration_service.py`](../../web_dashboard/services/entitle_registration_service.py),
mirroring `terraform_pra_service`: it generates HCL, runs `terraform apply`, records
the new integration id, and stashes the Terraform state on the provisioning job so
decommission can `terraform destroy` it. The teardown is wired into VM termination and
DB decommission, so removing a resource removes its Entitle integration.

### Public vs. private — the Entitle Agent

How Entitle reaches the target determines whether an agent is needed:

- **Public** resource (reachable from Entitle's cloud) → registers directly, **no agent**.
- **Private** resource (our private RDS, PRA-only VMs) → Entitle reaches it through the
  **Entitle Agent**, which runs **only in a Kubernetes cluster** (Helm-installed); there
  is no Docker/ECS deployment ([agent docs](https://docs.beyondtrust.com/entitle/docs/entitle-agent)).

The agent is **shared**: one per VPC/network serves every private integration in it,
referenced by `entitle_agent_token_name`. So you provision the agent **once per
environment** (not per build). A registration for a private target fails (non-fatally)
with a clear message if no agent is configured. The Entitle agent is the *management*
plane (it mints/revokes the ephemeral SSH account or DB role); the **PRA tunnel** the
dashboard already brokers is the separate *access* path the user connects through.

> **Network reachability — the agent must have a route to the private target.**
> To "fetch resources" for an SSH-ephemeral target, the agent pod SSHes into the host
> and enumerates accounts, so it needs an IP path to the target on port 22. The
> dashboard-managed clusters build their **own self-contained VPC**; when the sandbox
> VPC is configured (`gcp_network` / `aws_vpc_id`), the cluster module **peers back to
> the sandbox VPC** and opens the lab-VM firewall to the cluster's node+pod ranges so
> the agent can reach private VMs directly (GCP: `gcp_gke` peering + a
> `*-allow-ssh-from-k8s` firewall; AWS: `aws_eks` VPC peering + VM-SG ingress). Without
> that path, the Entitle audit log shows **"Failed to fetch the resources of &lt;target&gt;"**
> (a connection timeout). **GCP caveat:** VPC peering is non-transitive, so the agent
> reaches sandbox **VMs** but **not** Cloud SQL private-IP DBs (behind the
> sandbox↔`servicenetworking` peering) — DB JIT uses the PRA tunnel, not the agent.

> **Provisioning the agent** is a one-time, per-environment admin step, and it is
> **wired up**: `POST /api/k8s/clusters/{id}/entitle-agent` Helm-installs the agent into
> a dashboard-managed cluster as a `k8s_entitle_agent` job, mints its token via the
> provider, stashes the value in the secrets backend and records
> `entitle_agent_token_name` for later private registrations. The token's lifecycle is
> tied to the agent's: the `remove` action and the decommission of the hosting cluster
> **destroy the auto-minted token** (freeing the name for the next mint) — an
> operator-supplied `entitle_agent_token_ref` is never touched. See
> [`docs/design/entitle-resource-registration.md`](../design/entitle-resource-registration.md).

### Kubernetes clusters

Cluster registration is **not** a build-form checkbox — a cluster is registered on
demand, after it exists, from the Kubernetes page:

| Call | Job type | What it does |
|---|---|---|
| `POST /api/k8s/clusters/{id}/entitle-register` | `k8s_entitle_register` | Registers the cluster as an Entitle **Kubernetes** integration |
| `POST /api/k8s/rancher/entitle-register` | `rancher_entitle_register` | The same for the Rancher-managed cluster |
| `POST /api/k8s/clusters/{id}/entitle-agent` | `k8s_entitle_agent` | Helm-installs the Entitle agent (the one-time per-environment step above) |

A **public** cluster registers directly, with the API server host, a token and the CA
cert passed as Terraform variables. A **private** cluster registers through the agent
instead and passes no credentials — the same public/private split as every other
resource type.

The public path's token is the minted ServiceAccount's **long-lived token Secret**,
applied and read back in a single runner invocation (sentinel-wrapped, so noisy
runner log capture can't corrupt it). If the cluster never populates that Secret,
the job falls back to a TokenRequest (`kubectl create token`) and logs a warning:
TokenRequest lifetimes are capped by the cluster (e.g. ~48 h on GKE), so an
integration registered that way stops working when the token expires — re-register
before then, or install the agent and use In-Cluster access.

**The fine-grained tier is the impersonator model.** Rather than Entitle minting cluster
credentials, `POST /api/k8s/clusters/{id}/impersonator` grants the Entra group
cluster-wide `impersonate` on `users`. Entitle then JIT-binds `<prefix>:<email>` to a
role, and the operator runs `kubectl --as=<prefix>:<email>` over the
[API tunnel](../kubernetes.md). The prefix is `entitle_k8s_user_prefix` (default
`entitle`).

Note that impersonation needs the **API (TCP) tunnel**, not the Web Jump — a browser
jump can't carry impersonation headers. Full context in
[Kubernetes](../kubernetes.md).

### Per-build opt-in (VMs and databases)

For **VMs and databases** — the resources the dashboard builds — registration is
**opt-in per build**. The build forms (AWS deploy / bulk deploy) show a
**"Register in Entitle for just-in-time SSH access"** checkbox; cloud-database provisioning
takes a `register_in_entitle` flag on `POST /api/databases`. Registration runs only when
**both** the global `entitle_registration_enabled` capability **and** the per-build choice
are on. Default off everywhere — nothing is registered until an operator and a builder both
opt in. Clusters differ: they are registered on demand after the fact
([above](#kubernetes-clusters)), so there is no build-time checkbox for them.

### Setup

**Settings → Integrations → Entitle**, or the first-run wizard. Then per-build, check the
"Register in Entitle" box.

| Field | Notes |
|---|---|
| API URL / API Token | Shared Entitle tenant credentials (also used by the other two tracks). The **API URL** is pre-filled with the canonical `https://api.entitle.io/v1` (identical for every tenant) and drives both machine-identity JIT and — normalized to scheme+host — the Terraform provider endpoint. Leave it unless you're on a non-standard Entitle region. |
| Terraform Provider API Key | `entitleio/entitle` provider key (`ENTITLE_API_KEY`); falls back to the API Token. |
| Registration enabled | Master capability switch for this track. |
| `entitle_owner_id` / `entitle_workflow_id` | **Required** — Entitle user UUID that owns created integrations + the default approval workflow UUID. |
| `entitle_agent_token_name` | **Auto-minted** — installing the Entitle agent mints a token via the provider, stashes its value in the secrets backend, and records this name (used to attach **private**/PRA-only targets during registration). Shown read-only in the panel; you don't set it by hand. See [the design doc](../design/entitle-resource-registration.md#agent-token--server-side-secret--helm-reuses-the-runner-primitives). |
| `entitle_allowed_durations` | JIT durations offered on created integrations (seconds). |
| SSH sudo user | **Optional override.** Each VM deploy automatically registers with its image's cloud-default login user (`ubuntu` / `ec2-user` / `azureuser` / `gcp-user` — the `provisioners/beyondtrust/` bt-ready user cloud-init set up with passwordless sudo). Set this only to force a different sudo user for **all** SSH registrations. |

> **SSH private key — not a config field.** The key Entitle authenticates with is the
> counterpart of the keypair **cloud-init injected into the VM at build time**, resolved
> per-cloud from the dashboard's existing SSH keypair secret (Azure
> `azure_ssh_keypair_secret_name`, AWS `ec2_ssh_key_secret` / `ec2/keypairs/<name>`, GCP
> `gcp_ssh_key_secret_name`) — *not* a separately-configured Entitle key.
> `entitle_ssh_private_key_ref` exists only as an optional global fallback/override. See
> the [design doc](../design/entitle-resource-registration.md#ssh-key-sourcing--from-the-vms-own-keypair-not-config).

> **Application slugs:** `application.name` is a lowercase catalog slug — `postgresql`
> is confirmed; `mysql` / `mssql` / `ssh` are best-effort. Confirm against the
> `entitle_applications` data source for your tenant and adjust `_APP_SLUG` in the
> service if they differ.

---

## Machine identity — JIT cloud credentials via Entitle

There's a separate track that covers **what credentials get used** when the dashboard
executes a privileged action against AWS / Azure / GCP — the Cloud-Identity JIT design.

Today the dashboard's three cloud identities (AWS IAM user, Azure Service
Principal, GCP Service Account) carry broad standing privilege all the
time. The machine-identity track replaces that with **per-request,
short-TTL elevations** issued through the same Entitle tenant — so an
`EC2 deploy` triggers a fresh IAM role grant scoped to that one action, valid
for ~15 minutes, audited end-to-end in Entitle, and auto-revoked afterwards.
No long-lived keys in the dashboard.

### Setup additions (when enabling the machine-identity track)

| Field | Where | Notes |
|---|---|---|
| `cloud_identity_gate_enabled` | Settings → Integrations → Entitle → Machine identity | Master kill-switch. Off by default. |
| `cloud_identity_<cloud>_enabled` | Same panel (3 checkboxes) | Per-cloud opt-in — promote AWS → Azure → GCP one at a time |
| Operation matrix | Same panel (JSON textarea) | Maps dashboard operations (`aws:ec2:deploy`) → Entitle resource IDs + per-cloud IAM roles |
| Entitle bundle | Entitle console | A dedicated **machine-identity** bundle with auto-approve enabled; do NOT route to a human reviewer |

### Further reading

- [`docs/design/cloud-identity-jit.md`](../design/cloud-identity-jit.md) — full design, threat model, per-cloud trade-offs.
- `docs/runbooks/cloud-identity-jit-phase-1-entitle-submit.md` — first end-to-end Entitle submit-and-poll loop; **requires a configured Entitle tenant**.

---

## User JIT — Entra-group-backed dashboard permissions via Entitle

The User-JIT track governs **what permissions the user has at all** — granted
just-in-time through Entitle instead of statically assigned by an admin.

Today the dashboard's user permissions (e.g. `aws:write`, `images:delete`,
`dashboard-admin`) are static — once granted, they persist until an admin revokes
them. The Entitle User-JIT track lets users **request** those permissions through the
same Entitle workflow, and the dashboard recognises the time-bound grant on the next
login or token refresh (via Entra group membership).

### Setup additions (when enabling the User-JIT track)

| Field | Where | Notes |
|---|---|---|
| `entitle_user_jit_enabled` | Settings → Integrations → Entitle → User JIT | Master toggle. Off by default. |
| Entra tenant ID + admin SP | Same panel | Used by the bootstrap script for one-shot group provisioning. |
| OAuth group mapping | Same panel + `/api/admin/oauth-group-mappings` | Maps `dashboard-aws-write` → scope `aws:write`. |
| Resource ID map | Same panel (JSON) | Maps each scope to the Entitle resource ID for the 403-page request-access deep link. |

The Terraform module under [`terraform/entitle_user_jit/`](../../terraform/entitle_user_jit)
covers the Entitle side (one application + workflows + resources + policies). The Entra
bootstrap is a separate script: `python -m web_dashboard.scripts.bootstrap_entra_groups`.

### Further reading

- [`docs/design/entitle-user-jit.md`](../design/entitle-user-jit.md) — full design, operation matrix, OAuth resolution flow.
- `docs/runbooks/entitle-user-jit-phase-2-bootstrap-entitle.md` — Entitle virtual-application provisioner.

---

## Troubleshooting

**Nothing registers in Entitle** — confirm both the global **Registration enabled**
capability *and* the per-build "Register in Entitle" checkbox are on, then check the job
log: `docker compose logs app | grep -i entitle`. Registration is non-fatal — the VM/DB
still provisions; the job message records why registration was skipped or failed.

**"an Entitle Agent token named X already exists in the tenant"** (the raw provider
form is `Failed to create the Agent Token, status code: 400 … Resource already exists`) —
the token mint failed, so the `k8s_entitle_agent` job died **before touching the cluster**;
nothing was installed and Live Output stays empty. It means the tenant already holds a
token of that name while this dashboard holds no copy of its value. The value is returned
only at creation and cannot be read back, and `ensure_agent_token` already tries to recover
it from `entitle_agent_token_tf_state` before minting — so reaching this error means there
is no stored state either (config migration treats all three keys as runtime handles and
drops them: see `config_migrate/classify.py` `_RUNTIME_HANDLES`). Teardown now destroys the
auto-minted token (the agent `remove` action, and the decommission of the hosting cluster),
so a conflicting token was minted by an earlier dashboard version, a different
dashboard/environment, or by hand. Fix by one of: delete
that token in Entitle and retry — which breaks any agent still using it; set
`ENTITLE_AGENT_TOKEN_NAME` to an unused name so a fresh token is minted; or set
`ENTITLE_AGENT_TOKEN_REF` to the existing token value. Both keys are **env/`.env` only** —
`entitle_agent_token_name` is read-only in the Settings panel and `entitle_agent_token_ref`
is not in it at all. To check whether an agent is in fact installed, the Kubernetes page's
per-cluster flag is just `entitle_agent_cluster_id == cluster.id`; confirm against reality
with `helm list -n entitle`.

**"private target requires entitle_agent_token_name"** — the resource is private and no
Entitle agent is configured. Either provision the agent (Kubernetes) and set
`entitle_agent_token_name`, or register only public resources.

**"Failed to fetch the resources of &lt;target&gt;" (SSH ephemeral)** — the agent can't
reach the private host on port 22. Almost always a **network path**: the agent's cluster
VPC has no route to the target (e.g. a GKE cluster in its own isolated VPC, and the
private VM in the sandbox `vm-subnet`). Fix = ensure the cluster module peered back to the
sandbox VPC — set `gcp_network` (GCP) / `aws_vpc_id` (AWS) **before provisioning** the
cluster, then re-provision so the peering + lab-VM firewall are created (see *Network
reachability* above). Verify from a pod: `nc -vz <target-private-ip> 22`. (For the
*Kubernetes* connector — not SSH — the same message instead means the agent SA lacks
cluster RBAC; that's handled by the cluster-admin binding `setup_entitle_agent` applies.)

**"entitle_owner_id / entitle_workflow_id is not configured"** — both are required to
create an integration. Fill them in under Settings → Integrations → Entitle.

**`terraform init` can't find the provider** ("was not found in any of the search
locations") — the `entitleio/entitle` provider is baked into the image's read-only
provider mirror (`$TF_PROVIDER_MIRROR_DIR`, served via `/etc/terraform.tfrc`), and the
registry is deliberately not a fallback. Rebuild the image if you changed the Dockerfile
provider pre-cache step or the provider version constraint.

**Wrong application / connection** — `application.name` and the `connection_json` keys are
application-specific. Verify the slug against the `entitle_applications` data source and the
per-application connection schema in the BeyondTrust integration docs linked above.
