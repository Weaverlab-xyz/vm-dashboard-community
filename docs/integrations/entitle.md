# Entitle Integration

The Entitle integration has three independent tracks that share one Entitle
tenant + API token. You can enable any combination:

| Track | What it does | Default |
|---|---|---|
| **Resource registration** (this doc) | As the dashboard builds Linux VMs and cloud databases, it registers each as an Entitle integration so users request **just-in-time access** in Entitle. | Off |
| **Machine-identity JIT** | Short-TTL, auto-approved cloud-credential elevations for the dashboard's own privileged cloud calls. | Off |
| **User-JIT** | Entra-group-backed dashboard permissions granted just-in-time through Entitle. | Off |

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
| SQL Server database | **Microsoft SQL Server** ([docs](https://docs.beyondtrust.com/entitle/docs/entitle-integration-microsoft-sql-server)) |
| _(future)_ EKS / AKS / GKE cluster | **Kubernetes** — wired in as those build flows ship |

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

> **Provisioning the agent** (dashboard-managed EKS/AKS/GKE cluster + Helm) is a
> one-time **admin prerequisite** — a designed, deferred phase that lands alongside the
> EKS build flow. See [`docs/design/entitle-resource-registration.md`](../design/entitle-resource-registration.md).

### Per-build opt-in

Registration is **opt-in per build**. The build forms (AWS deploy / bulk deploy) show a
**"Register in Entitle for just-in-time SSH access"** checkbox; cloud-database provisioning
takes a `register_in_entitle` flag on `POST /api/databases`. Registration runs only when
**both** the global `entitle_registration_enabled` capability **and** the per-build choice
are on. Default off everywhere — nothing is registered until an operator and a builder both
opt in.

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

**"private target requires entitle_agent_token_name"** — the resource is private and no
Entitle agent is configured. Either provision the agent (Kubernetes) and set
`entitle_agent_token_name`, or register only public resources.

**"entitle_owner_id / entitle_workflow_id is not configured"** — both are required to
create an integration. Fill them in under Settings → Integrations → Entitle.

**`terraform init` can't find the provider** — the `entitleio/entitle` provider is
pre-cached in the image at `$TF_PLUGIN_CACHE_DIR`; rebuild the image if you changed the
Dockerfile provider-cache step.

**Wrong application / connection** — `application.name` and the `connection_json` keys are
application-specific. Verify the slug against the `entitle_applications` data source and the
per-application connection schema in the BeyondTrust integration docs linked above.
