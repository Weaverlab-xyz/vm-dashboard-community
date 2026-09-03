# Standing up a POV instance

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are installing a POV instance, or registering the BeyondTrust tenant a POV will point at.

Part of [A POV Instance](README.md). Its own JWT key, its own env file, and the registry of customer tenants it resolves against.

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
([config-migration.md](../../config-migration.md)); never a database dump.

**Bring it up alone the first time.** First boot runs `init_db`, which takes a Postgres
advisory lock. Two cold instances started together can wedge on it. Start this stack, let it
finish, then start the other.

**Its agent endpoint must be reachable from inside your POV environments.** POV wiring works
by running an agent on a broker VM *inside* the customer environment, which polls outward —
see [the broker VM](skytap.md#the-broker-vm) for what that VM must carry.
That means `/api/agent` on this instance has to be reachable over public HTTPS from there.
The provision refuses the broker step outright when this instance does not know its own
public URL, or when that URL is plaintext: the agent will not sign over `http://`, so a
broker installed against one would never enrol.
When you host it beyond your LAN, use the gateway-sidecar split in
[cloud-hosting.md](../../cloud-hosting.md) so the agent endpoint is separate from the UI, and keep
the UI behind its own allowlist. A UI 404 that returns in microseconds is the allowlist
doing its job, not a broken deploy.

**Auto-delete is off, observe-only is on, and two one-hour arming clocks have not started.**
A POV is a 30-day resource, so this is the one piece of configuration a POV instance is not
useful without — and enabling it is deliberately not something the compose file can do.
Follow the rollout in [auto-delete-timer.md](../../auto-delete-timer.md) *before* you create a POV
you expect to be cleaned up. Note also that `resource_expiry_max_total_hours` defaults to
`720` — exactly 30 days — so a 30-day POV starts at the ceiling and cannot be extended until
you raise it.

---


## Blueprints, and where templates come from

**POV → Templates** (`/pov/templates`), admin only. Two things live there.

**The template builder** authors the templates a POV is built from — see
[building a template](skytap.md#building-a-template). It exists because a POV
*is* a template instantiated whole, so before it the whole feature was downstream of a
catalogue nobody could author from here, and because the one piece of the
[template contract](skytap.md#the-template-contract) that has to live in your
image — the metadata runner — had no automation at all.

**A blueprint** is a saved POV recipe: the fields the create form asks, under one name. Pick
one on the POV page and it fills the form; you still type the POV's own name. For a given
*kind* of POV — a Password Safe evaluation, a PRA-only demo — every field except the name
has the same right answer every time, and two SEs building "the same" POV should build the
same POV.

| A blueprint carries | It does not carry |
|---|---|
| The template, project and broker VM name | The POV's **name** — that is per-POV by definition |
| The idle timeout and an expiry override | The **workgroup**, which decides RBAC and the expiry exempt list. Not something a saved recipe should set silently |
| The three tenant references | Any **secret** — see below |
| The Gateway name, and the Resource Broker's VM, zone and installer asset | |

### It is defaults, not a second provision path

A blueprint supplies values for the fields a request left blank and nothing else. Everything
after that is the same provision job that runs without one — a blueprint that forked the
flow would be a second place for the rules about orphaned environments to drift. If you
typed a broker VM name before picking a recipe, the recipe does not overwrite it.

The tenant ids on a blueprint are validated **when you save it**, against the same check the
create form uses. A stored selection that only failed at provision time would move a form
error into a job, against an environment that already exists.

### Nothing on a blueprint auto-runs, and nothing on it is a secret

It is tempting to have a blueprint chain the Gateway, Resource Broker and wire-up straight
off a provision. It cannot, for two reasons that are worth stating rather than discovering:

- **Both installs run through the broker agent**, which does not exist until minutes after
  the environment comes up. A step enqueued at provision time would find no agent.
- **Both need a credential that is per-tenant, not per-recipe** — a PRA deploy key and the
  Password Safe installer key. A blueprint that carried those would spread two secrets
  across every recipe an SE ever saved, to save one paste.

So a blueprint fills in the non-secret half — the Gateway name, the Resource Broker's VM,
zone and asset — onto the new POV, and those panels open ready to press with only the secret
left to paste. A field the POV already carries is never overwritten.

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
genuinely per-customer, while Entitle is multi-tenant behind a regional API URL and is
usually the same tenant for every POV. One row holding all three would force a duplicate
Entitle credential per customer, and duplicated credentials rotate apart.

| | |
|---|---|
| **Name** | A slug — lowercase letters, digits, hyphens. See [below](#about-customer-data) |
| **URL / hostname** | `tenant.beyondtrustcloud.com` for PRA, or the Password Safe URL. **Entitle's is regional** — `api.us.entitle.io` (the shipped default), `api.entitle.io` and `api.ca.entitle.io` are separate deployments, not aliases. The field prefills with *this instance's* region and says so; check it against the customer's tenant, because every region answers a probe and a wrong one surfaces much later as a tenant holding none of their resources. Clearing it is refused rather than re-defaulted |
| **OAuth client id** | PRA and Password Safe. Entitle authenticates with a bearer token and has no paired id |
| **Client secret** | Encrypted with the same Fernet key as every other secret in this dashboard |
| **…or a vault reference** | `aws_sm://`, `azure_kv://`, `gcp_sm://`, `bt_safe://` — for operators who want no credential in this database at all. One or the other, never both |
| **Jump Group / Gateway** | PRA only. Names *inside that appliance*, which is why they belong on the tenant and not in global settings. The stored option key is still `jumpoint_name`, matching the `bt_jumpoint_name` setting it seeds from — see [why the key looks wrong](#why-the-gateway-option-key-looks-wrong) |
| **Password Safe run-as user** | Required by the `passwordsafe` Terraform provider block |
| **Owner id / Workflow id** | Entitle only, and **both are required** — `pov_wireup` refuses before creating *any* integration without them, the REST accessor adapter included. They are ids inside that tenant, which is why they are not a global setting |
| **Agent token name / SSH sudo user** | Entitle only, and only the SSH ephemeral-accounts connector needs them. The agent token is the manual answer: a POV that installs its own agent [names that one instead](wiring.md#the-entitle-agent) |

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


## Failure modes

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
