# BeyondTrust Password Safe

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want credentials vaulted and rotated rather than stored by this dashboard.

## What is it?

**BeyondTrust Password Safe / Secrets Safe** is on-demand checkout of SSH keys and
passwords. Target credentials (AWS keys, Azure service principal secrets, SSH private
keys) are fetched at the moment the dashboard needs them and discarded after use, rather
than stored in the dashboard's encrypted database. Driven by `ps-cli`.

It also **onboards** the resources the dashboard builds — VMs, cloud databases, and
Kubernetes ServiceAccount tokens — as managed systems + accounts, so their credentials
rotate on the tenant's schedule instead of living forever as whatever the deploy minted.

Gated by `password_safe_enabled`. This is one of three independently-gated BeyondTrust
products; see [BeyondTrust Integrations](../beyondtrust.md) for the map, and
[Privileged Remote Access](privileged-remote-access.md) for the jump-item and Gateway
half of the story.

---

## Use cases

- **Vault-backed cloud credentials** — instead of entering AWS access keys,
  Azure service principal secrets, or SSH private keys into the dashboard
  (where they would be stored encrypted in the application database), the
  dashboard fetches them from Password Safe at runtime. Rotate credentials in
  one place; the dashboard always gets the current value.
- **Audit trail** — every secret checkout creates a Password Safe audit record.
  You know who (the dashboard service account) requested what credential and
  when.
- **SSH key checkout for cloud VMs** — the Ansible config-management runner and
  BeyondTrust Gateway container retrieve SSH keys from Password Safe managed
  accounts, so the private key never touches the host filesystem.
- **In-playbook secret lookup (Ansible)** — a config-management playbook can fetch
  its own secrets/managed-account passwords from Password Safe at runtime via the
  `beyondtrust.secrets_safe` Galaxy collection. The dashboard reuses this same OAuth
  client (`pscli_*`) — auto-injecting it into the runner as `PASSWORD_SAFE_*` — so no
  separate credential is needed. See
  [Ansible secrets](../ansible/secrets.md#in-playbook-password-safe-lookup-beyondtrustsecrets_safe)
  and [examples/playbooks/password-safe/](../../../examples/playbooks/password-safe/).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| BeyondTrust Password Safe | Secrets Safe licence; hosted or on-prem |
| `ps-cli` | Ships as the `beyondtrust-bips-cli` **pip dependency** (`web_dashboard/requirements.txt`), so it is present in any image built from this repo — there is no separate binary install step |

---

## Setup

### Step 1 — Password Safe OAuth application (ps-cli)

ps-cli authenticates to Password Safe with an OAuth 2.0 client-credentials
grant.

1. In **Password Safe** → **Configuration** → **API Registration** →
   **Add API Registration**:
   - Authentication type: **Client Credentials**
   - Copy the **Client ID** and **Client Secret** displayed after creation.

2. Assign the registration the following permissions (minimum):
   - **Secrets** → Read
   - **Requests** → Create
   - **Credentials** → Read

   Add Managed System / Managed Account scope for any accounts the dashboard
   will check out SSH keys from.

### Step 2 — Enable and configure in the dashboard

**Option A — Setup wizard (first run)**

The wizard's **Feature Flags** step lists the optional integrations. Toggle **Password
Safe** on there — that step carries toggles only, so fill in the fields from **Settings →
Integrations** once you have logged in.

**Option B — Settings → Integrations (after first run)**

1. Open **Settings** → **Integrations** → **Password Safe** → toggle on
   (`password_safe_enabled`).
2. Fill in the **API connection** section:

   | Field | Example |
   |---|---|
   | Password Safe URL | `https://ps.company.com` |
   | OAuth Client ID | (from API Registration) |
   | OAuth Client Secret | (from API Registration) |
   | API run-as account | The BeyondInsight user the OAuth client runs as — required by the Password Safe Terraform provider for VM onboarding |

3. Click **Save**. No container restart is required.

> These are **Settings keys**, not environment variables. The dashboard's config store
> reads from the database only; exporting an equivalently-named env var has no effect.

---

## What it enables in the dashboard

| Feature | Description |
|---|---|
| **Vault-backed cloud credentials** | AWS, Azure, and SSH credentials resolved from Password Safe at runtime rather than stored in the application database |
| **SSH key checkout** | Ansible and BT Gateway tasks retrieve SSH private keys from Managed Accounts on demand |
| **Managed-account checkout for playbook runs** | A Config-Management run can use a Password Safe managed account as its login identity — the operator picks an account from a live list and the credential is checked out **just-in-time** at run time, never shown and scrubbed from job output. See [below](#managed-account-checkout-for-config-management-runs) |
| **Resource onboarding** | VMs and cloud databases the dashboard builds are onboarded as Password Safe managed systems + accounts, and removed again on destroy |
| **Hypervisor credentials for a remote agent** | An on-prem agent brokering vCenter/Proxmox/Hyper-V can hold no credential at all: the dashboard checks one out per job, seals it to that agent, checks it back in and rotates it on release. See [below](#hypervisor-credentials-for-a-remote-agent) |
| **Secret audit log** | Every checkout creates an immutable record in Password Safe |
| **Attributes on Inventory** | Each matched resource's Password Safe attributes (`Status = Online`, `Business Unit = Finance`) as a filterable column on `/inventory`. Admins can assign and remove them, for one resource or up to 50 at once, and re-run the Smart Rule that uses them. See [Inventory — Password Safe attributes](../../inventory.md#password-safe-attributes) |

PRA Vault accounts minted for tunnels can themselves be onboarded here for rotation —
see [Privileged Remote Access](privileged-remote-access.md).

### Managed-account checkout for Config-Management runs

Instead of referencing a *stored* secret, an operator running a playbook can pick a
Password Safe **managed account** and have its credential checked out just-in-time. Two
details are worth knowing because they are not obvious:

- **Across many hosts, the account is matched by name.** A managed account reference
  pins a system id *and* an account id, both specific to one managed system — reusing
  one across a fleet would check out a single machine's credential and connect to every
  host with it. A [bulk run](../../operations/config-management.md#bulk-runs-from-the-inventory)
  therefore sends the account **name**, and each job resolves it against the host it is
  configuring, so every host checks out its own credential.
- **On the ECS / Cloud Run runners it needs an opt-in.** Those runners *reference* a
  store secret rather than taking a value inline, which a just-in-time credential has
  no place in — so they are rejected unless **Ephemeral cloud secrets** is enabled, at
  which point the credential is briefly written to that cloud's store as a short-lived,
  RBAC-locked secret and force-deleted after the run.

Full walkthrough in
[Ansible → Managed-account checkout](../ansible/secrets.md#managed-account-checkout-beyondtrust-password-safe).

### Hypervisor credentials for a remote agent

A [remote agent](../../remote-agents.md) brokering hypervisor operations inside a private network
has two ways to use Password Safe, and they differ in which host holds the OAuth client:

| | Agent-side checkout | Dashboard-side checkout |
|---|---|---|
| Set in | `connections.yaml` → `ps_managed_account: <id>` | Connections tab → `ps_account://<id>`, plus `dashboard_secret: true` in `connections.yaml` |
| Password Safe client lives on | the agent host, in `passwordsafe.yaml` | the dashboard, which already has one |
| Hypervisor password at rest | nowhere | nowhere |
| Rotate on release | no | **yes**, by default |
| Needs | nothing extra | agent image ≥ 2.1.0 |

The second leaves the on-prem host holding nothing but the agent's own identity key. That
matters because the OAuth client is usually entitled to more than the single account it is
being used for, so it is the more valuable secret of the two — and it is the one sitting on
the least-defended machine.

The two are **mutually exclusive** per connection: declare both and the agent refuses the job
rather than picking, because there would be no way afterwards to say which credential a job
had used.

Practical notes:

- **Duration** comes from `agent_ps_checkout_duration_min` (default 45). It must outlast the
  job *plus* the reaping window — if an agent container is killed, the request is released
  once the stale-job reconcile has failed the job (~10 min) and the next sweep has run
  (~1 min). A request that expires before then is closed by Password Safe itself, which is
  safe, but the release then does not appear as the dashboard's doing.
- **Approval-required accounts do not work unattended.** A `4034` fails the job rather than
  hanging, and the request it opened is checked straight back in so it does not hold the
  account's concurrent-request slot. Use auto-approve access policies.
- **Concurrent jobs on one account share a request.** `ConflictOption=reuse` is deliberate,
  so the release is reference-counted: the last job to finish is the one that checks in and
  rotates. Otherwise the first to finish would change the password under a live sibling.
- **Rotation can be turned off** with `agent_ps_rotate_on_release=false`, for the case where
  Password Safe is not the sole owner of that account and something else is configured
  statically with the same password.

---

## Password Safe VM onboarding (managed systems)

Onboarding the Linux and Windows VMs the dashboard builds, per cloud, and syncing their keys into the PRA Vault: see [Onboarding VMs](password-safe/vm-onboarding.md).

## Kubernetes ServiceAccount token rotation

Making the ServiceAccount token a PRA Kubernetes tunnel injects a rotating Password Safe managed account: see [Kubernetes token rotation](password-safe/kubernetes-tokens.md#kubernetes-serviceaccount-token-rotation).

## Databases

Handing database credentials to Password Safe, and importing databases Password Safe already
manages. The shared model is on [Password Safe: databases](password-safe/databases.md), and
each cloud's channel has its own page: [AWS (`dbssm`)](password-safe/databases-aws.md),
[Azure (`dbazure`)](password-safe/databases-azure.md) and
[GCP Cloud SQL (`dbgcp`)](password-safe/databases-gcp.md).

---

## Advanced configuration

`bt_ps_deploy_key_title` — the title of the Password Safe secret holding the Gateway Docker
deploy key — is documented with the Gateway keys it belongs to, on
[Privileged Remote Access → Advanced configuration](privileged-remote-access.md#advanced-configuration).
Password Safe is only the storage mechanism there; starting a Gateway is a PRA concern.

---

## Troubleshooting

**"ps-cli not found"** — `ps-cli` comes from the `beyondtrust-bips-cli` pip package in
`web_dashboard/requirements.txt`, so this means the image was built without installing
requirements, or something is shadowing `PATH`. Confirm with
`docker compose exec app ps-cli --version`; rebuild the image if it is genuinely absent.

**"Authentication failed" from ps-cli** — verify the Client ID and Client Secret
in **Settings → Integrations → Password Safe** match the API Registration in
Password Safe and that the registration has not expired.

**Secrets retrieved are empty** — check that the API Registration has **Secrets →
Read** and **Credentials → Read** permissions, and that the specific secret is
in scope for the registration.

**A file secret retrieves with no contents** — expected, if you reached for
`ps-cli secrets get`. That verb projects a **per-type** field set, and the file one is
metadata only: `FileName` and `FileHash`, with no content field at all (a text secret gets
`Text`, a credential gets `Password`). `-d` / `--decrypt` cannot rescue it — the flag only
adds a `decrypt=true` query parameter, and there is no payload field in the projection for
it to fill. The body comes from a different verb, which takes **only** a GUID (there is no
`--title` and no `--path`), so fetching a file secret by name is always two calls:

```bash
ps-cli secrets get -t 'api-gateway-chain'
```

```bash
ps-cli secrets download -id <GUID-from-the-Id-column> -s ./chain.pem
```

`download -s` writes the file `0600`. One adjacent trap: `secrets get -id <GUID> -d`
**ignores** `--decrypt` — only the `--title` branch enables it — so resolving by ID hands
back a masked credential and no error.

A `raw` call reaches the same endpoint in **one** step if you already hold the GUID.
This is the route **the dashboard itself takes** — `read_bt_secrets_safe` spots
`SecretType: File` and follows up with it, so a `bt_safe://` reference to a text bundle
resolves normally rather than to an empty string:

```bash
ps-cli raw GET "Secrets-Safe/Secrets/<GUID>/file/download"
```

**Neither route is binary-safe, and that is a ps-cli limit rather than an API one.** The
endpoint itself returns `application/octet-stream` — raw bytes, faithfully — but every
path ps-cli offers decodes them to text before you see them: `download-secret-file` hands
back `response.text`, and `raw` falls through its JSON parse to print `response.text` too.
A PEM bundle is ASCII and survives; a `.pfx`, `.p12` or DER payload is **corrupted rather
than refused**. So keep certificate material in file secrets as PEM — or, if it genuinely
has to be a PKCS#12, call
[`GET .../file/download`](https://docs.beyondtrust.com/bips/reference/get-api-public-v3-secrets-safe-secrets-secretid-file-download)
directly and keep the bytes, the way the agent worker already calls `Requests` and
`Credentials`.

In a playbook none of this applies for text bundles — the `beyondtrust.secrets_safe`
lookup resolves all three types in one call by `folder/title` (it decodes as text too, so
the PEM-only caveat carries over). See [Secrets in a Remote Worker run](../ansible/secrets.md#in-playbook-password-safe-lookup-beyondtrustsecrets_safe).

**A checkout returns `4031` / 403** — usually the API identity is missing the **Requestor**
role or an access policy granting View on a Smart Rule containing the account. There is no
Smart Rule API, so this is out-of-band; see [Operator prerequisites](password-safe/kubernetes-tokens.md#operator-prerequisites).

Password Safe returns the same 4031 when the account is not **API-enabled**, and when the
`SystemID` on the request does not own the `AccountID` — `POST Requests` authorises the
*pair*. So a 4031 that survives a correctly granted Requestor role is not the grant. The
dashboard sends the account's real managed system (read from the account when the caller
does not already hold it) and quotes Password Safe's response body in the job error, which
is where the numeric code that separates these lives: `4034` is a request awaiting approval
and `4035` the account's concurrent-request cap — both also 403.

## What the public API can and cannot do

Researched against BeyondTrust's own endpoint tables — the [BeyondInsight
APIs](https://docs.beyondtrust.com/bips/v24.3/docs/beyondinsight-api) and [Password Safe
APIs](https://docs.beyondtrust.com/bips/v24.3/docs/password-safe-api) references — while
scoping the POV readiness panel. Recorded here so nobody has to establish it twice, and so
a plan that assumes "we can automate the console" gets corrected before it is written.

**Creatable:**

| Object | Call |
|---|---|
| User groups, their permissions and memberships | `POST UserGroups`, `POST UserGroups/{id}/Permissions`, `.../Users` |
| Workgroups | `POST Workgroups` |
| Assets | `POST Workgroups/{workgroupID}/Assets` |
| Address groups and their addresses | `POST AddressGroups` |
| API registrations | `POST` |
| Users | `POST Users` |
| Functional accounts | `POST FunctionalAccounts` — already used by `pov_functional_account` |
| Directory managed systems | `POST Workgroups/{id}/Directories` |
| Managed accounts | `POST ManagedSystems/{systemID}/ManagedAccounts` |
| Attributes and attribute types | `POST` |
| Smart Rules — **one shape only** | `POST SmartRules/FilterAssetAttribute` |

**Actionable:**

| Action | Call |
|---|---|
| Re-process a Smart Rule | `POST SmartRules/{id}/Process` — used by `pov_ps_config.process` |
| Rotate a managed account's credential | `POST ManagedAccounts/{id}/Credentials/Change` |
| Test an access policy | `POST AccessPolicies/Test` |

**Not creatable — read-only, or no endpoint at all:**

| Object | State |
|---|---|
| Resource zones, resource brokers | **no endpoint of any kind.** Independently confirms why `ps_application_host_id` was never the broker handle — see [`docs/profiles/pov/design/resource-broker.md`](../../profiles/pov/design/resource-broker.md) §6 |
| Discovery credentials | no endpoint |
| Discovery scans | no endpoint; run from the console |
| Directory queries | no endpoint |
| Password policies | read-only |
| Access policies | read, plus the Test above |
| Applications | read-only |
| Smart Rules, generally | read, delete and process; create is the one narrow shape above |

The practical consequence: **the engine of a Password Safe POC — an authenticated discovery
scan and the Smart Rules that act on its results — cannot be built by this dashboard.** What
it can do is verify the work and re-run a rule, which is what `pov_ps_config` does.

Absence from the documentation is not proof of absence from the product, and most of the
table above was not checked against a live tenant. The `_probe` helper in `ps_api_service`
is built for that uncertainty: a `404` is reported as "this Password Safe version does not
serve that endpoint" rather than as a failure, so a tenant that *does* serve one of these
shows up as readable instead of broken.

### Assets and attributes — verified live, 2026-09-23

*This is contributor-level API evidence. For using attributes, see
[Inventory — Password Safe attributes](../../inventory.md#password-safe-attributes).*

Building the `/inventory` attributes column settled four of these against a real tenant.
All four were guesses before, and two of them were wrong:

| Call | Result | Note |
|---|---|---|
| `GET Assets` | **404** | There is **no flat asset collection.** |
| `GET Workgroups/{id}/Assets` | 200 | Assets are **workgroup-scoped on read**, matching the POST. Reading them all means one call per workgroup. |
| `GET AttributeTypes` | 200 | The vocabulary — `Criticality`, `Business Unit`, `Geography`, `Status`, `Operating System`, `Retire Date`, `Workgroup`, `Manufacturer`. |
| `GET Attributes` | **404** | No flat attribute collection either. |
| `GET Assets/{id}/Attributes` | 200 | Per object, which is why the read is capped and only matched objects are fetched. |
| `GET ManagedSystems/{id}/Attributes` | 200 | Exists, and is commonly empty. |
| `POST`/`DELETE ManagedSystems/{id}/Attributes/{attributeID}` | **unverified** | Documented by BeyondTrust; not yet exercised against a tenant from here. See the write note below for what the dashboard does about that. The SPIRE lab's **Prepare probe** is the first caller that will. It also calls `POST AttributeTypes` and `POST AttributeTypes/{id}/Attributes` when the tenant lacks `SpiffeTrustDomain`, and its job log records every call's status, so its first live run settles all three rows. |

**An attribute's shape is easy to read backwards**, and doing so produces chips that look
broken. A row is:

```json
{"AttributeID": 10000, "AttributeTypeID": 10000, "ShortName": "Online",
 "LongName": "Online", "ValueInt": 0, "ChildAttributes": []}
```

`ShortName` is the **value**; the *type* is the category it was chosen from. So
`AttributeTypeID 10000` + `ShortName "Online"` renders as **`Status = Online`** — not
`Online = ""`. The type is also the half `SmartRules/FilterAssetAttribute` keys on, so
losing it loses the point. `ps_attribute_catalog.to_chips` takes the `AttributeTypes` map
for exactly this.

**Writing an attribute — verified live the same day.** `POST` and `DELETE` on
`Assets/{assetID}/Attributes/{attributeID}` both work; a `DELETE` of an attribute the
asset does not carry answers **404**, which the dashboard treats as success (the caller
asked for it gone and it is gone, so a retry after a partial apply does not report errors
for the targets that already succeeded).

**Managed systems take attributes too, and that is the path that matters here.**
`ManagedSystems/{managedSystemID}/Attributes/{attributeID}` is the same POST/DELETE shape
in a different collection. It is the one most of `/inventory` needs: everything this
dashboard onboards lands as a managed system, so while the write path was asset-only, most
matched rows could have their attributes *read* and never changed.

One asymmetry follows from the table above: the managed-system **read** is verified and
the **write** is not. So the 404-is-success rule cannot simply be reused — an appliance
that does not serve the endpoint at all answers 404 to exactly the same DELETE. Before
accepting a 404 as "already gone", `ps_api_service._set_object_attribute` issues one
`GET {collection}/{id}/Attributes`: a 200 proves the endpoint family exists and the
attribute is genuinely absent, anything else is reported as a failure. Reporting a removal
that never happened is the worse of the two errors by a wide margin.

A resource matched to **both** an asset and a managed system gets the change written to
both records. They are separate rows in Password Safe and can disagree; the inventory
page's chips flatten them into one, so writing to only half means a removal leaves the chip
on screen and reads as a failed write.

An attribute is **assigned, not typed**: a type owns a fixed set of values, each with its
own `AttributeID`, so `GET AttributeTypes/{id}/Attributes` is the picker and there is no
free text anywhere in the write path. Some types are `IsReadOnly: true` — `Criticality`
is, in the tenant checked — and the dashboard refuses those by name rather than letting
Password Safe refuse them once per target in a bulk apply.

**An asset's address is not guaranteed usable.** In the tenant checked, 33 of 35 assets
carried a routable `IPAddress`; the other two carried an IPv6 **link-local** (`fe80::…`),
which cannot identify a host. Separately, every managed system this dashboard onboards
through a cloud-native plugin carries the `127.0.0.1` placeholder with a packed locator in
`DnsName` — so it has **no usable address at all**, and is matched by the
`ps_managed_system_id` recorded on its deploy job instead. See `ps_attribute_catalog`.
