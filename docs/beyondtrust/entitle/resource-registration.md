# Registering resources with Entitle

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want what the dashboard builds (VMs, databases, clusters, Rancher, Portainer) requestable in Entitle.

Part of [Entitle Integration](../entitle.md).

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
| Portainer (managed node, or one you pointed the dashboard at) | **REST API** in Ephemeral Accounts mode, served by the `portainer_access` Cloud Function. Portainer has no Entitle connector at all, so an adapter is the only route; deploy it from the Portainer page's **Just-in-time access (Entitle)** card. Assets are Portainer **teams**. See [portainer.md](../../cloud-services/portainer/entitle.md) |

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

Registration is done by [`entitle_registration_service.py`](../../../web_dashboard/services/entitle_registration_service.py),
mirroring `terraform_pra_service`: it generates HCL, runs `terraform apply`, records
the new integration id, and stashes the Terraform state on the provisioning job so
decommission can `terraform destroy` it. The teardown is wired into VM termination and
DB decommission, so removing a resource removes its Entitle integration.

## Public vs. private — the Entitle Agent

How Entitle reaches the target determines whether an agent is needed:

- **Public** resource (reachable from Entitle's cloud) → registers directly, **no agent**.
- **Private** resource (our private RDS, PRA-only VMs) → Entitle reaches it through the
  **Entitle Agent**, which runs **only in a Kubernetes cluster** (Helm-installed); there
  is no Docker/ECS deployment ([agent docs](https://docs.beyondtrust.com/entitle/docs/entitle-agent)).

The agent is **shared**: one per VPC/network serves every private integration in it,
referenced by `entitle_agent_token_name`. So you provision the agent **once per
environment** (not per build).

> **Except an OT demo cell,** which mints its own. A cell registers against an agent
> running *in its own plant* — on a DMZ broker deployed beside it — because an agent
> outside the plant both misrepresents the architecture and, with the cell's Purdue
> zoning on, cannot reach it at all: the registration succeeds, the grant approves, and
> the login fails. See
> [Who brokers identity in the plant](../../profiles/demo/ot-demo-cell/identity.md#who-brokers-identity-in-the-plant).

"Only in a Kubernetes cluster" does not have to mean a real one. For an edge or
plant-floor host that will not carry a cluster, [KubeSolo](../../cloud-services/kubernetes/kubesolo.md) is a
single-node, etcd-free distribution that takes the same chart unmodified — Config
Management playbooks install it and the agent on an on-prem host through a remote
agent. That page also covers what the chart's defaults get wrong on one node, and why
the agent needs port 8080 as well as 443.

> **On a POV instance the agent is per POV, not per install.** A POV's VMs are on their own
> private network, so the shared one above cannot reach them. The POV page installs a
> single-node k3s on a Linux guest and Helm-installs the agent there, minting the token in
> that POV's own Entitle tenant — see
> [the POV instance guide](../../profiles/pov/wiring.md#the-entitle-agent). The keys on this page
> describe the *instance-wide* agent used by managed clusters and stay untouched by it. A registration for a private target fails (non-fatally)
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
> [`docs/design/entitle-resource-registration.md`](../../design/entitle-resource-registration.md).

## Kubernetes clusters

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
[API tunnel](../../cloud-services/kubernetes.md). The prefix is `entitle_k8s_user_prefix` (default
`entitle`).

Note that impersonation needs the **API (TCP) tunnel**, not the Web Jump — a browser
jump can't carry impersonation headers. Full context in
[Kubernetes](../../cloud-services/kubernetes.md).

## Per-build opt-in (VMs and databases)

For **VMs and databases** — the resources the dashboard builds — registration is
**opt-in per build**. The build forms (AWS deploy / bulk deploy) show a
**"Register in Entitle for just-in-time SSH access"** checkbox; cloud-database provisioning
takes a `register_in_entitle` flag on `POST /api/databases`. Registration runs only when
**both** the global `entitle_registration_enabled` capability **and** the per-build choice
are on. Default off everywhere — nothing is registered until an operator and a builder both
opt in. Clusters differ: they are registered on demand after the fact
([above](#kubernetes-clusters)), so there is no build-time checkbox for them.

## Setup

**Settings → Integrations → Entitle**, or the first-run wizard. Then per-build, check the
"Register in Entitle" box.

| Field | Notes |
|---|---|
| API URL / API Token | Shared Entitle tenant credentials (also used by the other two tracks). The **API URL** is **regional** — `https://api.us.entitle.io/v1` (the shipped default), `https://api.entitle.io/v1` and `https://api.ca.entitle.io/v1` are separate deployments, not aliases — and drives both machine-identity JIT and, normalized to scheme+host, the Terraform provider endpoint. Set the region your tenant is in. Every region answers an unauthenticated request, so a wrong one is not rejected here: it surfaces later as a tenant that appears to hold none of your resources. |
| Terraform Provider API Key | `entitleio/entitle` provider key (`ENTITLE_API_KEY`); falls back to the API Token. |
| Provider endpoint (`entitle_endpoint`) | **Optional.** The Terraform provider's API base. Blank derives it from the regional API URL above, which is almost always right; set it only to point the provider somewhere else. Blank does *not* mean the provider's own default, which is the unprefixed `https://api.entitle.io`. |
| Registration enabled | Master capability switch for this track. |
| `entitle_owner_id` / `entitle_workflow_id` | **Required** — Entitle user UUID that owns created integrations + the default approval workflow UUID. |
| `entitle_agent_token_name` | **Auto-minted** — installing the Entitle agent mints a token via the provider, stashes its value in the secrets backend, and records this name (used to attach **private**/PRA-only targets during registration). Shown read-only in the panel; you don't set it by hand. See [the design doc](../../design/entitle-resource-registration.md#agent-token--server-side-secret--helm-reuses-the-runner-primitives). |
| `entitle_allowed_durations` | JIT durations offered on created integrations (seconds). |
| SSH sudo user | **Optional override.** Each VM deploy automatically registers with its image's cloud-default login user (`ubuntu` / `ec2-user` / `azureuser` / `gcp-user` — the `provisioners/beyondtrust/` bt-ready user cloud-init set up with passwordless sudo). Set this only to force a different sudo user for **all** SSH registrations. |

> **SSH private key — not a config field.** The key Entitle authenticates with is the
> counterpart of the keypair **cloud-init injected into the VM at build time**, resolved
> per-cloud from the dashboard's existing SSH keypair secret (Azure
> `azure_ssh_keypair_secret_name`, AWS `ec2_ssh_key_secret` / `ec2/keypairs/<name>`, GCP
> `gcp_ssh_key_secret_name`) — *not* a separately-configured Entitle key.
> `entitle_ssh_private_key_ref` exists only as an optional global fallback/override. See
> the [design doc](../../design/entitle-resource-registration.md#ssh-key-sourcing--from-the-vms-own-keypair-not-config).

> **Application slugs:** `application.name` is a lowercase catalog slug — `postgresql`
> is confirmed; `mysql` / `mssql` / `ssh` are best-effort. Confirm against the
> `entitle_applications` data source for your tenant and adjust `_APP_SLUG` in the
> service if they differ.

## Connecting to an SSH ephemeral-account grant

When a grant on a registered VM is approved, Entitle delivers it (in Teams, for example)
as a single bash one-liner:

```bash
echo -e '<PEM with literal \n>' > user_NNNN.pem && chmod 600 user_NNNN.pem && ssh -i user_NNNN.pem user_NNNN@<private-ip>
```

Two things about it are easy to miss:

- **Every grant mints a new account.** The username (`karenwalker_8762`, then
  `karenwalker_8274` on the next grant) and the key are both new. When a grant ends,
  Entitle deletes that account (`userdel`). A key from an earlier message, or an old
  username, fails with the same `Permission denied (publickey)` a wrong key gives. Take
  the key, username **and** host from the **same** grant message.
- **The host is a private address.** Lab VMs sit on private subnets (`10.99.2.0/24` for
  the Azure `vm-subnet`). Entitle gets to them through its agent (see
  [above](#public-vs-private--the-entitle-agent)), but that is the *management* path
  only. **Your** machine needs its own route, so start a PRA **Network Tunnel** through
  the gateway (`clouddb-jumpoint` on Azure) before you run `ssh`. Without it, `ssh`
  reports `Connection timed out`. See
  [Network tunnels need an address pool](../gateways/address-pool.md)
  if the tunnel itself won't come up.

On Linux or macOS, paste the one-liner as it is.

### From Windows

The one-liner assumes bash and a POSIX filesystem. Neither is true on Windows, and each
of the obvious workarounds breaks in its own way:

- **WSL, in a `/mnt/c/...` directory:** `chmod` does nothing on a Windows drive, so the
  file stays `0777`. `ssh` then prints `Permissions 0777 for 'user_NNNN.pem' are too open`
  and ignores the key. Run the one-liner from your WSL home directory (`cd ~` first)
  instead, or use PowerShell.
- **PowerShell ISE:** ISE cannot run `ssh` interactively. The host-key confirmation
  prompt never shows up, so the session appears to hang, and ssh's normal stderr is
  displayed as a red `NativeCommandError`. Use a regular PowerShell window or Windows
  Terminal.

In a regular PowerShell window, paste all of this at once. Put everything between the
quotes of `echo -e` into `$key`, and the grant's username and host into the `ssh` line:

```powershell
$key = '<everything between the quotes of echo -e>'
$path = "$HOME\.ssh\entitle-$(Get-Date -Format yyyyMMdd-HHmmss).pem"
[IO.File]:../integrations/beyondtrust/:WriteAllText($path, ($key -replace '\\n', "`n"))
icacls $path /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh-keygen -lf $path
ssh -i $path <user>@<host>
```

What each part is for:

- **`[IO.File]::WriteAllText`, not `Out-File` or `>`.** It writes the key with no byte-order
  mark and with LF line endings. A BOM or CRLF endings can stop `ssh` from reading the key.
- **A new, timestamped filename on every run.** After the `icacls` line removes
  inheritance and leaves you read-only access, you can't overwrite the file, and even
  `Remove-Item -Force` fails on it. Writing each grant's key to a new file avoids this.
  Paste the whole block every time, too: a `$key` left over from an earlier paste still
  holds the previous grant's key.
- **`ssh-keygen -lf`** prints the key's fingerprint. Compare it with the fingerprint of
  the key the server actually installed (see [Troubleshooting](troubleshooting.md)).
