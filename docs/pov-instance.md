# A POV Instance

A **second** dashboard, next to the one managing your demo estate. Same image, same code,
its own database, its own users, its own agents — and a different set of features.

It exists because of one thing: **tenancy.** A demo instance resolves its BeyondTrust
tenant from the global singletons (`bt_api_host`, `pscli_api_url`, `entitle_api_key`). A POV
instance holds a *registry* of many named tenants, because several POVs run at once and each
has its own PRA appliance and Password Safe Cloud tenant. An instance claiming both roles
would have two answers to "which tenant?" at every call site — and the wrong answer is
silent, not loud: a demo VM deploy onboarding into a customer's Password Safe, or a POV
onboarding into your demo tenant. Nothing errors, both paths "work".

So the two profiles are **mutually exclusive**, and the exclusion is enforced in code rather
than left to discipline.

---

## The gate

`install_profile` is `demo` (the default, and what every existing install already is) or
`pov`. It is set on the setup wizard's **Purpose** step, and it is a gate, not a preference.

Everything resolves through one function — `services/feature_flags.enabled()`:

```
enabled(flag) = False                         if this profile masks the flag
              = config_service.get_bool(flag) otherwise
```

That single reader is the point. `main._feature_gate` decides whether a router 404s, and
`feature_flags.flags()` decides whether the nav link and Settings toggle render. A mask
applied in only one of them gives you a nav link to a router that 404s, or a page with no
way to reach it. Both come through `enabled()`.

Two properties worth knowing:

- **The mask only ever subtracts.** A profile can refuse a feature. It can never turn on one
  that config left off.
- **An unrecognised profile resolves to `demo`.** The profile is read on the request path, so
  a typo in one config row must not take the app down — and falling back to `demo` means the
  worst case is *today's* behaviour.

### What each profile gets

| | `demo` | `pov` |
|---|---|---|
| Cloud VMs, images, Packer, promote | yes | — |
| On-prem hypervisors (Proxmox, vSphere, Hyper-V, Nutanix, XCP-ng) | yes | — |
| Cloud databases, Kubernetes, containers, cloud functions | yes | — |
| Virtual desktops, EPM-L, cost reporting | yes | — |
| POV environments | — | yes |
| Lab platforms (Skytap) | — | yes |
| PRA, Password Safe, Entitle | yes | yes |
| Remote agents, Config Management | yes | yes |
| Auto-delete timer, notifications, secret scanning, auth/SSO | yes | yes |

The third block is deliberate: a POV instance needs PRA, Password Safe and the agent more
than a demo instance does, so those are profile-neutral rather than owned by either.

A demo-only integration on a POV instance is not merely toggled off — **Settings refuses to
enable it**, with a 409 naming the profile. Accepting the write would store a flag that
reads back as on while `enabled()` keeps returning `False`: a toggle that saves cleanly,
shows on, and does nothing. Turning something *off* is always allowed.

A POV instance also needs a **lab platform** — the thing a POV environment actually runs
on. Skytap is the first one; see [integrations/skytap.md](integrations/skytap.md) for its
prerequisites, the API token it needs, and the
[template contract](integrations/skytap.md#the-template-contract) its broker VM has to
satisfy.

And it needs somewhere to put the many tenants the first paragraph of this page is about.
That is [the tenant registry](#the-tenant-registry) below. The platform registry lives in
`services/lab_platforms.py`, and `GET /api/pov/platforms` reports what each one can do so
the UI can degrade visibly rather than offering a button that fails.

---

## Standing one up

```bash
# Its own JWT key. Not a copy of the demo stack's — see below.
openssl rand -hex 32 > .jwt_secret_key.pov

# Its own env file. Start from the example and set only what a POV instance uses.
cp .env.example .env.pov

docker compose -p vmdash-pov -f docker-compose.pov.yml up -d
```

Then browse `http://localhost:8002` (the demo stack keeps 8001) and walk the wizard: create
the admin account, choose **Customer POV / POC environments** on the Purpose step, and the
four cloud credential steps disappear.

`-p vmdash-pov` matters. Without an explicit project name Compose derives one from the
directory, and both stacks would try to share networks and volume names.

### Four things that will bite otherwise

**Its `JWT_SECRET_KEY` must be its own value.** That key is the Fernet root for the
`app_config` table. Two instances sharing it is the only way their encrypted configuration
could be interchanged — and two instances with *different* keys is exactly why a `pg_dump`
restore from one into the other yields **unreadable ciphertext with no error at all**. If you
need to copy settings across, use the config-migration tool
([config-migration.md](config-migration.md)); never a database dump.

**Bring it up alone the first time.** First boot runs `init_db`, which takes a Postgres
advisory lock. Two cold instances started together can wedge on it. Start this stack, let it
finish, then start the other.

**Its agent endpoint must be reachable from inside your POV environments.** POV wiring works
by running an agent on a broker VM *inside* the customer environment, which polls outward —
see [the broker VM](integrations/skytap.md#the-broker-vm) for what that VM must carry.
That means `/api/agent` on this instance has to be reachable over public HTTPS from there.
The provision refuses the broker step outright when this instance does not know its own
public URL, or when that URL is plaintext: the agent will not sign over `http://`, so a
broker installed against one would never enrol.
When you host it beyond your LAN, use the gateway-sidecar split in
[cloud-hosting.md](cloud-hosting.md) so the agent endpoint is separate from the UI, and keep
the UI behind its own allowlist. A UI 404 that returns in microseconds is the allowlist
doing its job, not a broken deploy.

**Auto-delete is off, observe-only is on, and two one-hour arming clocks have not started.**
A POV is a 30-day resource, so this is the one piece of configuration a POV instance is not
useful without — and enabling it is deliberately not something the compose file can do.
Follow the rollout in [auto-delete-timer.md](auto-delete-timer.md) *before* you create a POV
you expect to be cleaned up. Note also that `resource_expiry_max_total_hours` defaults to
`720` — exactly 30 days — so a 30-day POV starts at the ceiling and cannot be extended until
you raise it.

---

---

## The tenant registry

The reason this profile exists, made concrete. **POV page → BeyondTrust tenants.**

A demo instance has one PRA appliance, one Password Safe tenant and one Entitle tenant,
configured in Settings as `bt_api_host`, `pscli_api_url` and `entitle_api_url`. That is the
right shape when there is exactly one of each. A POV instance runs several POVs at once,
each for a different customer, each with its own appliance — so "which tenant?" stops
having one answer and the singletons stop being able to express the question.

The registry holds **one row per product, not per customer**, and a POV carries three
independent references. That is not an accident of modelling: PRA and Password Safe are
genuinely per-customer, while Entitle is multi-tenant behind one canonical API URL and is
usually the same tenant for every POV. One row holding all three would force a duplicate
Entitle credential per customer, and duplicated credentials rotate apart.

| | |
|---|---|
| **Name** | A slug — lowercase letters, digits, hyphens. See [below](#about-customer-data) |
| **URL / hostname** | `tenant.beyondtrustcloud.com` for PRA, the Password Safe URL, or the Entitle API base |
| **OAuth client id** | PRA and Password Safe. Entitle authenticates with a bearer token and has no paired id |
| **Client secret** | Encrypted with the same Fernet key as every other secret in this dashboard |
| **…or a vault reference** | `aws_sm://`, `azure_kv://`, `gcp_sm://`, `bt_safe://` — for operators who want no credential in this database at all. One or the other, never both |
| **Jump Group / Gateway** | PRA only. Names *inside that appliance*, which is why they belong on the tenant and not in global settings. The stored option key is still `jumpoint_name`, matching the `bt_jumpoint_name` setting it seeds from — see [why the key looks wrong](#why-the-gateway-option-key-looks-wrong) |
| **Password Safe run-as user** | Required by the `passwordsafe` Terraform provider block |

### How a POV picks one

Choose them on the New POV form, or press **edit** in the Tenants column of an existing
POV — a POV is often stood up while the customer's appliance is still being provisioned,
so "choose later" is a real answer and not a placeholder.

What the resolver does, in order:

1. the **id the POV carries**. A missing, disabled or wrong-product id is an **error**,
   never a fallback;
2. the row marked **default** for that product;
3. the only active row for that product, if there is exactly one;
4. **the Settings singletons**, if the registry holds no row for that product at all;
5. otherwise a refusal naming the fix.

Step 1 not falling back is the whole point. Falling back would mean a POV whose tenant was
deleted or disabled quietly resolving to *the default instead* — which is a POV onboarding
into another customer's Password Safe with nothing going wrong on the way. Step 5 is the
same rule seen from the other side: guessing between two customers' appliances is much
worse than refusing.

Step 4 is the compatibility contract. Every existing install, and every demo instance,
keeps working without knowing this table exists.

### Verify

**Verify** performs the same authentication the real work does — PRA's OAuth token
request, Password Safe's token plus `SignAppIn` — and stores the result on the row. Both
halves of the Password Safe check matter: the token proves the OAuth client, while
`SignAppIn` proves the BeyondInsight user it is linked to. An OAuth client with no linked
user gets a perfectly good token and fails at the second step, so checking only the first
would report green for a tenant that cannot do anything.

**Entitle has no Verify, deliberately.** Everything this dashboard does with Entitle goes
through the Terraform provider or POSTs an access request, and an access request is a side
effect rather than a check. There is no read here that would prove a bearer token, and an
invented one is how a Verify starts reporting green for a token that does not work. The
row says "not verifiable" rather than hiding the distinction, because "we did not check"
and "we checked and it was fine" must never look the same on a page you use to decide a
POV is ready.

A verify result is **cleared** when the URL, the client id or the secret changes. The
tenant that passed is not the tenant you now have, and stale-green is worse than unknown.

### Seeding, and the one-way door

On first boot the dashboard copies whatever singletons the install already has into rows,
once. After that **the rows are the truth and editing the Settings keys does nothing** —
the same one-way promise the Connections page makes for hypervisors, and for the same
reason: a second copy that keeps re-reading the singletons is how an operator edits a
field and watches it have no effect, or worse, have an effect a week later.

The seed is marked done rather than inferred from an empty table, so deleting a seeded row
on purpose does not bring it back on the next restart.

### Deleting one

Refused while a live POV still references it. The database constraint is `SET NULL`, so
deleting anyway would not error — it would blank that POV's tenant, and the next wire-up
would resolve the *default* instead. **Disable** it if you want it out of the pickers
without touching the POVs already wired into it.

### Why the Gateway option key looks wrong

BeyondTrust renamed this component to **Gateway**, and this dashboard's prose follows
that everywhere. Identifiers deliberately do not: `bt_jumpoint_name` is a row in
`app_config` and a line in operators' `.env` files, and `/api/config/v1/jumpoint` is the
vendor's own Config API path. Renaming either reads a key that was never written, or
calls a path that 404s — and the first failure is a blank setting rather than an error.

So a tenant's option key is `jumpoint_name` while the field is labelled **Gateway name**.
That asymmetry is the rule, not an oversight; `tests/test_gateway_terminology.py` pins
both halves of it.

### About customer data

This dashboard deliberately holds no customer field: `PovEnvironment` has none and adding
one should be treated as a schema regression. This table is the one place that rule bends,
and only exactly as far as it must. A PRA appliance is reached at a hostname like
`acme.beyondtrustcloud.com`, so the connection target inherently identifies whose it is
and there is no way to hold one without that.

A connection target you cannot avoid. A free-text "customer" box you can — so the **name**
is a constrained slug and not somewhere to type a company. Use a reference: `poc-014-pra`.

---

## What this stack deliberately does not mount

Unlike the demo stack, neither service gets `/var/run/docker.sock` or the `runner_work`
volume. The demo stack needs the socket to launch sibling Ansible and kubectl/helm
containers. A POV instance runs Ansible **on the remote agent inside the customer
environment**, and Kubernetes is masked off entirely, so handing this stack the host's Docker
daemon would grant a great deal and buy nothing.

If you later want local Config Management runs here, the socket has to come back — and that
is a deliberate decision to make, not an omission to fix.

---

## Failure modes

**A nav link is missing and I know the flag is on.** Check the profile. `GET /api/features`
returns `install_profile`, and a masked feature reports `false` there whatever the stored
flag says. That is the mask, working.

**Settings gives me a 409 when I enable an integration.** Also the mask. The message names
the profile. That integration belongs on the other instance.

**I switched the profile and a feature did not come back.** The mask only subtracts, so
switching to `demo` stops masking — but the feature's own `*_enabled` flag still has to be
on. Check both.

**A POV is refusing with "no … tenant is configured".** Nothing is registered for that
product and the Settings singleton for it is blank or half-filled — a URL with no secret
is not a tenant, because it fails at the first call. Register one, or fill in the
singleton.

**A POV refuses with "N tenants are configured and none is the default".** Step 5 of the
resolver, working. Pick one on the POV itself, or mark a default.

**I edited `bt_api_host` in Settings and the POV still uses the old appliance.** The seed
is one-way: once a row exists it is the truth. Edit the tenant on the POV page.

**Verify says the credentials are rejected and I am sure they are right.** For PRA, the
client id and secret come from an API account in the appliance (Management → API
Configuration) — a user login always fails there. For Password Safe, a token that succeeds
and a `SignAppIn` that fails means the OAuth client is fine and its linked BeyondInsight
user is missing, disabled, or has no Password Safe API access.

**Everything is refused with "not enabled" on a fresh POV instance.** `pra_enabled` and
`password_safe_enabled` default to `True` in code but the compose file sets them explicitly;
if you wrote your own env file, set them. `configured()` is separate again: PRA and Password
Safe also need their credentials before their endpoints do anything.

**The wizard still shows cloud steps.** You are on `demo`. The Purpose step drives the step
list, so re-run `/setup` and change it — nothing else needs re-entering.

**A cloud region key appeared in a POV instance's config.** It should not. The `pov` profile
skips the cloud **writes**, not just the screens, precisely because the wizard otherwise
persists every non-secret field whether or not you filled it in — leaving
`aws_region="us-east-2"` and friends behind. If you see one, it predates the profile or was
set by hand.
