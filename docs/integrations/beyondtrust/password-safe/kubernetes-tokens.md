# Password Safe: Kubernetes token rotation

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the ServiceAccount token a PRA Kubernetes tunnel injects to be a Password Safe managed account that rotates, with the PRA Vault copy kept current.

Part of [Password Safe](../password-safe.md).

## Kubernetes ServiceAccount token rotation

A cluster's PRA k8s tunnel can inject a ServiceAccount bearer token at session launch
([Kubernetes → Access & identity](../../../kubernetes.md#access--identity)). The dashboard used to
mint that token once and never touch it again. This makes it a Password Safe **managed
account** so it rotates on the tenant's schedule, and keeps the PRA Vault copy current.

> This path needs **both** products: `password_safe_enabled` for the rotation itself and
> `pra_enabled` for the tunnel identity the token belongs to. With PRA off there is no
> injected ServiceAccount to manage.

Two custom plugins, both imported by hand:

| Plugin | Platform (default name) | Role |
|---|---|---|
| Kubernetes Service Account Token | `Kubernetes Service Account Token` | Rotates the token in the cluster |
| PRA Vault Token | `PRA Vault Token` | Writes the rotated value into the PRA Vault `opaque_token` account |

Enable **Kubernetes Token Rotation** in Settings → Integrations → Password Safe, then use the
per-cluster **Token rotation** button on `/k8s` (or tick the box on the provision form).
Registration applies the rotator RBAC, creates the managed system + account, registers the
PRA Vault Token account, **syncs** it to the token account, rotates once to prove the path,
and finally deletes the dashboard-minted `<sa>-token` Secret.

**The registration rotation is not optional here.** A managed account cannot be *created*
holding a bearer token: the REST path that sets a password on create caps it at 128
characters and a ServiceAccount JWT is 800–1,200, so the account starts out holding a
throwaway placeholder. (The cap is specific to that path — a plugin's rotation write-back
carries multi-KB values, which is how the SSH-key plugins store 3.2 KB private keys.) The
first rotation is therefore what puts a real credential in the vault, and turning
`k8s_ps_token_change_on_register` off does not skip it — registration overrides the setting
and warns, because a vault serving a placeholder to the PRA tunnel looks exactly like a
working registration.

The sync is the step that matters, and it is a Password Safe feature rather than anything the
dashboard runs: the PRA Vault account becomes a *subscriber* of the token account, and a
managed account and its subscribers always share an identical credential. The link is created
before the registration rotation on purpose — a failure there has changed nothing in the
cluster, whereas rotating first and failing to link would leave PRA holding a value that was
just revoked and that nothing would refresh.

**Why that last step matters.** The plugin's rotation sweep selects Secrets by *its own*
labels, so the dashboard-minted Secret is never swept — leaving it in place would mean
rotation revokes nothing and a permanent cluster-admin bearer token stays valid forever. The
ordering is deliberate too: it is deleted only after PRA carries a Password-Safe-issued
token, because until then it is the credential live sessions are using.

### Managed system address

The address is the plugin's only per-cluster configuration surface (a `.psplugin` is
checksum-sealed, so its packaged settings cannot be edited), capped at 249 characters:

```
eks;<region>;<clusterName>[;option…]
aks;<subscriptionId>;<resourceGroup>;<clusterName>[;option…]
gke;<projectId>;<location>;<clusterName>[;option…]
k8s;<apiServerUrl>[;option…]
```

The dashboard builds it from the cluster row plus its deploy job's Terraform variables. For
a **registered** cluster those are unknown, so the modal accepts a cloud cluster name and
(for AKS) a resource group. **OKE and on-prem clusters use the generic `k8s;` form** — the
plugin has no OCI provider. For GKE the `location` must be the **zone** for a zonal cluster
and the **region** for a regional one; mixing them is the documented cause of a 404.

Options appended automatically: `;longlived` or `;bound` (+ `ttl=`), `;ns=<namespace>`, and
anything in `k8s_ps_token_address_options` (e.g. `dnsEndpoint=true`, `serverName=`,
`allowHostnameMismatch=true`, `ca=` on generic addresses only).

### Functional accounts

One per cloud, created by the operator — the dashboard references them by name and never
holds a cloud secret. The managed system inherits the functional account's platform, so a
functional account on the wrong platform is refused with the platform named.

| Cloud | Username | Password |
|---|---|---|
| GKE | service account email (or `impersonate:<target>`) | **base64 of the whole JSON key file** |
| EKS | AWS access key id (or `InstanceProfile` / `WebIdentity`) | `<secret>` or `<secret>:<sessionToken>` |
| AKS | `SP:<tenantId>:<clientId>` — tenant **first** | Entra client secret (`-` for managed identity) |
| Generic / OKE | `token`, `cert` or `kubeconfig` | bearer token, PEM/PKCS#12, or `b64kubeconfig:<base64>` |
| PRA Vault Token | PRA OAuth **Client ID** | PRA OAuth **Client Secret** |

### In-cluster rotator RBAC

Applied automatically (`k8s_ps_rotator_apply_rbac`). LongLived needs `serviceaccounts`
get/list + `secrets` create/get/list/delete; Bound needs `serviceaccounts/token` create and
**no** access to Secrets at all. The ClusterRole always applies; the binding only when a
subject is configured, because **the subject differs per cloud and is the single most common
onboarding mistake**:

| Cloud | Binding subject | Config key |
|---|---|---|
| GKE | the service account's **email** | `k8s_ps_rotator_gke_sa_email` (blank → derived from the functional account) |
| AKS | the service principal's **object id** (`oid`), *not* the client id | `k8s_ps_rotator_aks_sp_object_id` |
| EKS | the username the access entry maps the principal to | `k8s_ps_rotator_eks_username` |
| Generic / OKE | a bootstrap ServiceAccount | `k8s_ps_rotator_bootstrap_sa` / `_namespace` |

On EKS the access entry is created too when `k8s_ps_rotator_eks_principal_arn` is set —
without it the binding's `User` subject matches nothing and the API server returns 401,
which is invisible from inside the cluster. The dashboard never edits `aws-auth`; a bad edit
there can lock every principal out. If the ARN is unset the job result prints the command:

```
aws eks create-access-entry --cluster-name <cluster> \
  --principal-arn <arn> --type STANDARD --username passwordsafe-rotator
```

**The cluster's authentication mode must include `API`, or there is no access-entry API to
call.** EKS defaults new clusters to `CONFIG_MAP`, where the command above is rejected and
the only way in is the `aws-auth` ConfigMap the dashboard deliberately never edits. Clusters
provisioned here are built `API_AND_CONFIG_MAP` (`authentication_mode` in
`terraform/k8s_cluster/aws_eks`); an older or hand-built cluster is converted in place, and
EKS allows the upgrade but never the reverse:

```
aws eks update-cluster-config --name <cluster> \
  --access-config authenticationMode=API_AND_CONFIG_MAP
```

Symptom when this is missed: registration succeeds, then the **first rotation** fails with a
`400` from `Credentials/Change` whose body is the plugin's attempt log ending in an API-server
`401`/`403` — nothing in the cluster looks wrong, because the binding is fine and it is the
*identity* that was never mapped.

**AKS has the same failure for a different reason: the ClusterRoleBinding is not what
authorises a Microsoft Entra principal there.** Every cluster provisioned here sets
`azure_rbac_enabled` (`terraform/k8s_cluster/azure_aks`), which delegates Kubernetes API
*authorisation* to Azure role assignments. The role a rotator service principal is usually
given — **Azure Kubernetes Service Cluster User Role** — is control-plane only: it grants
`listClusterUserCredential`, so the identity fetches a kubeconfig and authenticates
perfectly, and holds no Kubernetes verb at all.

So the dashboard also creates the role assignment (`k8s_ps_rotator_aks_assign_role`, on by
default). **It no longer needs to be told the object id.** Three routes, tried in order:

1. `k8s_ps_rotator_aks_sp_object_id`, if you set it.
2. **The dashboard's own token.** An Azure functional account's username is the
   *application (client) id*, and an object id cannot be derived from one — but a token
   states the oid of the principal it was issued to. When the functional account's appid
   is the dashboard's own appid (one app registration doing both jobs, the usual case),
   the oid is already a claim in a token the dashboard mints anyway: no directory
   permission, no extra call. It is only used when the two appids match, because granting
   our own oid for a functional account that is a *different* app registration would
   succeed at ARM and change nothing about the 403.
3. **Microsoft Graph**, when the functional account is a different principal. Needs
   `Application.Read.All` (or `Directory.Read.All`) consent, which most tenants have not
   granted, so a 403 here is skipped rather than fatal.

If all three come up empty the first rotation still fails — and **that failure is itself
the fourth route.** The AKS API server authenticated the functional account, resolved it
to an object id, and named that object id when it refused; so the dashboard reads the
principal out of the 403, makes the namespace-scoped assignment, waits for Azure to apply
it (`k8s_ps_rotator_aks_propagation_seconds`, default 240) and rotates again. A whole
registration therefore completes with no object id configured anywhere. Whatever the
resolved value was, it is written back to `k8s_ps_rotator_aks_sp_object_id` — announced in
the job result, and never over an operator's own value — so later registrations grant
before the first rotation rather than after it.

That recovery is the reason nothing in `ps_k8s_token_service` may call
`ManagedAccounts/{id}/Credentials/Change` directly; every rotation goes through
`_rotate_token_once`, and a static test enforces it. It backs off in three cases, all
deliberate: a non-Azure cluster, a failure that names no principal (a Password Safe
outage must not spend the propagation budget), and
`k8s_ps_rotator_aks_assign_role=false`. `k8s_ps_rotator_aks_role` picks which
one: `writer` (the default) and `reader` are the two Azure documents as assignable at
namespace scope, so those land on `<cluster>/namespaces/<pra namespace>`; `admin`,
`clusteradmin` or a custom role definition GUID go on the cluster. Writer is the right
default — it carries `secrets/*` and `serviceaccounts/*`, which covers LongLived's Secret
lifecycle and Bound's TokenRequest both. This needs `Microsoft.Authorization/roleAssignments/write`
(User Access Administrator or RBAC Administrator) on the cluster for the dashboard's own
Azure credential; when it is missing, or the object id is unset, the job result prints the
command:

```
az role assignment create --role "Azure Kubernetes Service RBAC Writer" \
  --assignee-object-id <sp-object-id> --assignee-principal-type ServicePrincipal \
  --scope <cluster-resource-id>/namespaces/pra-access
```

Use `--assignee-object-id`, not `--assignee`: the latter resolves the principal through
Microsoft Graph and fails outright (*"Cannot find user or service principal in graph
database"*) for a service principal the operator cannot read. A **new role assignment takes
up to five minutes** to reach the authorisation server, and registration rotates immediately,
so a 403 in the minute after a first registration is worth one retry before it means
anything. Symptom when the assignment is missing — again at the *first rotation*, and it
names its own cause:

```
HTTP 403 (Forbidden): serviceaccounts "pra-access" is forbidden: User "<oid>" cannot get
resource "serviceaccounts" in API group "" in the namespace "pra-access": User does not
have access to the resource in Azure. Update role assignment to allow access.
```

That 403 is also where the object id comes from when it was never configured. A failed
`k8s_ps_token` job reads the principal out of the rotation failure and fills it into the
`az role assignment create` line it had already collected, so the remedy on the job page is
runnable as printed instead of carrying a `<sp-object-id>` placeholder that the operator has
to resolve through Microsoft Graph. Set `k8s_ps_rotator_aks_sp_object_id` to that value and
the next register makes the assignment itself. The id is matched off the surrounding prose,
never scanned for as a bare GUID: the same error body carries the subscription id (the
address is `aks|<sub>|<rg>|<cluster>`) and the plugin's own id.

Run **Verify Functional Account** in Password Safe after registering: it names every missing
verb, prints the ClusterRole to apply, and logs the correct AKS object id on every run.

### Keeping the PRA Vault copy in sync

Password Safe does it. Registration calls
`POST ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}` — `{id}` is the **parent** (the
Kubernetes Service Account Token account), `{syncedAccountID}` the **subscriber** (the PRA
Vault Token account). `ps-cli synced-accounts create -ma-id <parent> -sa-id <subscriber>` is
the same operation and is the quickest way to check it by hand. From then on every rotation of
the token account is applied to the subscriber too, which runs the PRA Vault Token plugin's
PATCH into PRA. Both accounts stay Password Safe-managed and audited, and no credential passes
through the dashboard at all.

`GET …/ps-token/status` re-reads the link from Password Safe on every open rather than caching
it, because an admin unlinking in the Password Safe console is exactly the condition an
operator opens that panel to diagnose. `DELETE …/ps-token` unlinks before off-boarding either
account.

**Order of operations, and the repair when it was the other way round.** The subscriber is
created only when the cluster already has a PRA Vault account to write into, so registering
the token *before* the PRA k8s tunnel leaves a cluster whose token rotates while nothing
carries the value into PRA — Synced Accounts on the parent reads `0 items` and there is no
"PRA Vault Token" managed account in the tenant. **Re-running the registration repairs it**:
`Repair registration` in the cluster's token panel (or a re-POST of `…/ps-token`) creates the
subscriber, links the pair, and touches nothing in the cluster. The subscriber is created on a
placeholder rather than a checkout of the live token — the same 128-character cap drops any
real token — and Password Safe replaces it through the link, so **PRA's copy is refreshed by
the next rotation**. The pair is already in step at that point (the tunnel vaulted the value
Password Safe holds), so nothing is rotated to prove it; use **Rotate now** if you want the
path proven immediately, remembering that LongLived revokes the token a live session is using.

> **Removing the PRA tunnel does not unlink the pair.** The plugin resolves its PRA Vault
> account by *name* (`k8s-<cluster>-sa`), so re-provisioning the tunnel re-creates the account
> the link already points at and syncing resumes with no operator action. The cost is that a
> rotation landing while no tunnel exists fails the PRA half — visibly, in Password Safe's
> change log.

The **OT demo cell** uses the same primitive for its `adminuser` credential — parent on the
GCP VM SSH Rotation platform, subscriber on the **PRA Vault Username Password** plugin, PRA
Vault account associated to the cell's Jump Group for checkout/injection. See
[OT demo cell](../../../profiles/demo/ot-demo-cell.md#pra-checkout-of-the-cells-admin-credential).

**The LongLived break window.** Rotation revokes the old token immediately. Password Safe
applies the new value to the subscriber as part of the same change, but change operations are
queued, so there is a short window where PRA still holds the revoked token. Set the cluster's
token mode to **Bound** on tunnels that must not break: Bound never revokes, and the old token
stays valid until its TTL.

**Why this works when the parent mints a JWT rather than accepting a password.** Password
Safe's published shared-credential behaviour describes ordinary password accounts, where it
generates a password from the policy and pushes it outward. The sync actually copies whatever
is *stored* as the parent's password, and the parent's plugin decides what that is: the
"Kubernetes Service Account Token" plugin ignores the password policy — it is minting a JWT,
not a password — and reports the token it got from the cluster, so the token is the stored
credential and the token is what the subscriber receives.

### Operator prerequisites

1. Import both `.psplugin` packages and confirm the platform names.
2. Create the functional accounts above.
3. Grant the API identity **Requestor** plus an access policy granting **View** on a Smart
   Rule containing both managed accounts. There is no Smart Rule API, so this is out-of-band —
   and it is the failure every Password Safe consumption path here hits first (`POST /Requests`
   → `4031` / 403).
4. Grant the API identity **Password Safe Account Management (Full control)** — what the
   sync link needs, per the REST reference. (`ps-cli synced-accounts -h` claims *Role
   Management (Read/Write)* instead; that looks like an error in the CLI help, since the
   operation acts on managed accounts rather than roles. If the link 403s with Account
   Management already granted, try Role Management before assuming a different cause.)
5. **Leave "Change Password After Release" OFF** on *both* accounts. A credential change on
   either member of a synced pair re-rotates the pair, so with it on, every release of the PRA
   copy would rotate the real cluster token — an endless loop with a dead-credential window
   each time. There is no circuit breaker for this any more; the access policy is the fix.
6. Give the Password Safe host or Resource Broker network reachability to the cluster's API
   server. For private clusters this is a real constraint — Password Safe does not route
   through the PRA Gateway.

### Configuration keys — token rotation

| Key | Default | Notes |
|---|---|---|
| `k8s_ps_token_rotation_enabled` | `false` | Master gate (row action, provision checkbox) |
| `k8s_ps_token_platform` | `Kubernetes Service Account Token` | Plugin platform (name or id) |
| `k8s_ps_pravault_token_platform` | `PRA Vault Token` | Subscriber plugin platform |
| `k8s_ps_functional_account_aws` / `k8s_ps_functional_account_azure` / `k8s_ps_functional_account_gcp` / `k8s_ps_functional_account_local` | — | Per cloud; `_local` also covers OKE and on-prem |
| `k8s_ps_pravault_functional_account` | — | PRA Config-API OAuth client for the PRA Vault account |
| `k8s_ps_workgroup` | — | Blank → `passwordsafe_workgroup` |
| `k8s_ps_token_mode` | `longlived` | `longlived` (revokes) or `bound` (TTL expiry, no revoke) |
| `k8s_ps_token_ttl_seconds` | `3600` | Bound mode; clamped up to the API server's 600s floor |
| `k8s_ps_token_change_on_register` | `true` | Rotate once on register — proves the whole path immediately |
| `k8s_ps_token_delete_legacy_secret` | `true` | Retire the dashboard-minted Secret the plugin's sweep never touches |
| `k8s_ps_token_register_on_provision` | `false` | Pre-tick the provision-form checkbox |
| `k8s_ps_pravault_mirror_enabled` | `true` | Register and sync the PRA Vault Token account when a PRA vault account exists |
| `k8s_ps_token_checkout_duration_min` | `15` | Password Safe request duration for token reads |
| `k8s_ps_token_address_options` | — | Extra `;key=value` appended to every address |
| `k8s_ps_rotator_apply_rbac` | `true` | Apply the rotator ClusterRole + binding on register |
| `k8s_ps_rotator_gke_sa_email` | — | Blank → derived from the GCP functional account's name |
| `k8s_ps_rotator_aks_sp_object_id` | — | The `oid` claim. Resolved automatically when blank (own token → Graph → the first 403) and written back |
| `k8s_ps_rotator_aks_assign_role` | `true` | Grant that oid an AKS data-plane role — with Azure RBAC the binding alone authorises nothing |
| `k8s_ps_rotator_aks_role` | `writer` | `reader`/`writer` → namespace-scoped; `admin`/`clusteradmin`/GUID → cluster-scoped |
| `k8s_ps_rotator_aks_propagation_seconds` | `240` | How long a refused rotation waits for a NEW role assignment to apply before giving up |
| `k8s_ps_rotator_eks_username` | `passwordsafe-rotator` | Access-entry username = the RBAC `User` subject |
| `k8s_ps_rotator_eks_principal_arn` | — | IAM principal behind the functional account's key |
| `k8s_ps_rotator_eks_create_access_entry` | `true` | Create the access entry when the ARN is set |
| `k8s_ps_rotator_bootstrap_namespace` / `_sa` | `beyondtrust` / `password-safe-rotator` | Generic-path bootstrap ServiceAccount |

The managed account name is `<namespace>/<serviceaccount>`, taken from `pra_k8s_namespace` /
`pra_k8s_sa_name` on the
[Privileged Remote Access](../privileged-remote-access.md#kubernetes-tunnel-identity) panel —
PRA owns that identity, Password Safe rotates its token.

Off-boarding removes both managed systems and the rotator RBAC; it runs automatically when the
cluster is decommissioned or deregistered. Design rationale, including why each of these
choices is what it is: [k8s-sa-token-rotation](../../../design/k8s-sa-token-rotation.md).
