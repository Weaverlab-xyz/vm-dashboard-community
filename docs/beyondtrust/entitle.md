# Entitle Integration

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want access to expire on its own instead of being revoked by someone remembering.

The Entitle integration has four independent tracks that share one Entitle
tenant + API token. You can enable any combination:

| Track | What it does | Default |
|---|---|---|
| **Resource registration** (this doc) | As the dashboard builds Linux VMs and cloud databases, it registers each as an Entitle integration so users request **just-in-time access** in Entitle. | Off |
| **Machine-identity JIT** | Short-TTL, auto-approved cloud-credential elevations for the dashboard's own privileged cloud calls. | Off |
| **Virtual applications & persona bundles** | Friendly, tiered roles over a registered directory's groups, and one bundle per job function per cloud that combines them with the cloud's own roles. See [virtual applications](entitle/virtual-apps.md) and [persona bundles](entitle/persona-bundles.md). | Off (needs resource registration) |
| **Dashboard permissions** | Time-boxed permissions **inside the dashboard**, granted by Entitle. Two mechanisms: **REST** (current — any user, takes effect immediately) and **Entra groups** (legacy — Entra only, takes effect at next login). See [entitle-dashboard-permissions.md](entitle/dashboard-permissions.md). | Off |

> The previous **approval-gate** track (which blocked dashboard actions behind an
> Entitle approval + webhook) has been **removed**. Secret read/update/delete are
> now admin-only; cloud deploys run without an approval round-trip.

---

## Pages

Each part has a page:

| Page | Read it when |
|---|---|
| [Registering resources with Entitle](entitle/resource-registration.md) | you want what the dashboard builds (VMs, databases, clusters, Rancher, Portainer) requestable in Entitle |
| [Entitle dashboard permissions](entitle/dashboard-permissions.md) | you want dashboard access without standing admins, or you need to tell the two mechanisms apart |
| [Entitle virtual applications](entitle/virtual-apps.md) | you registered Active Directory or an identity provider and want people to request its groups in Entitle by a clear name, under an approval tier |
| [Entitle persona bundles](entitle/persona-bundles.md) | you want people to request everything one job function needs on one cloud, or on-premises through Active Directory, in a single Entitle request |
| [Troubleshooting Entitle](entitle/troubleshooting.md) | a resource will not register in Entitle, or a request is granted and nothing changes |

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
| Request portal URL (`entitle_request_portal_url`) | Same panel | Where the **Request access** nav link and the 403-page deep links point. |
| Resource ID map (`entitle_resource_ids_json`) | Same panel (JSON) | Maps each scope (`aws:write`) to the Entitle resource ID for the 403-page request-access deep link. Paste the `resource_ids` output of `terraform/entitle_user_jit/`. |
| Session length (`access_token_expire_minutes`) | Environment only: `ACCESS_TOKEN_EXPIRE_MINUTES` | The dashboard's own session token lifetime, default 480 (8 hours). Not a Settings field, and an import does not change it. A revoked Entitle grant takes effect at the user's next token, so keep it short (the design recommends 30 minutes or less) for high-sensitivity tenants. |

The Terraform module under [`terraform/entitle_user_jit/`](../../terraform/entitle_user_jit)
covers the Entitle side (one application + workflows + resources + policies). The Entra
bootstrap is a separate script: `python -m web_dashboard.scripts.bootstrap_entra_groups`.

### Further reading

- [`docs/design/entitle-user-jit.md`](../design/entitle-user-jit.md) — full design, operation matrix, OAuth resolution flow.
- `docs/runbooks/entitle-user-jit-phase-2-bootstrap-entitle.md` — Entitle virtual-application provisioner.

---

## Workload identity

Instead of the API token, the dashboard's REST calls to Entitle can present the dashboard's
own workload identity: set **Authentication → Workload identity** on this panel, after
setting up the shared identity and adding Entitle to its Pathfinder registration's
**Product Scopes**. Resource registration (the `entitleio/entitle` Terraform provider) still uses the API key or token, so keep one set. Not yet verified live — see
[BeyondTrust platform identity](platform-identity.md).

