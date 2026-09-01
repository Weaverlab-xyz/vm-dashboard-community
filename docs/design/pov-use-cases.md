# Design: use cases per POV, and the accessor who ticks them off

Three slices. This note covers all of them, because the first one has to carry the
columns and the vocabulary the other two need, and deciding that twice is how they end up
disagreeing.

Slice 1 is built. Slices 2 and 3 are designed here and not yet written.

## The problem

`services/personas.py` ships eight roles and five cards each, and `/use-cases` renders
them all. Every card resolves its readiness through `feature_flags.enabled()` — an
instance-wide answer — and every card targets a demo page: `/aws#instances`, `/vsphere`,
`/k8s`, `/databases`.

On a POV instance those pages are masked or 404. So every card resolves `masked`, and the
catalog is complete, correct and unusable: a wall of grey saying "Not available on a POV
instance". The page an SE would most want in front of a customer is the one that has
nothing to say there.

The deeper reason is that **a POV is not an instance.** `PovEnvironment` carries
`pra_tenant_id`, `ps_tenant_id` and `entitle_tenant_id` as three independent columns
precisely because a Password-Safe-only evaluation, a PRA + Password Safe one and an
all-three one are all normal shapes. Every one of them runs on an instance where
`pra_enabled` is on. The flag cannot tell them apart, and it is the flag that every card
was asking.

## The third axis

| Axis | Module | Power |
|---|---|---|
| Tenancy | `feature_flags.install_profile` | **Gates.** Subtracts, 404s routes |
| Role | `services/personas.py` | **Curates.** Reorders, surfaces, never hides |
| This POV's products | `services/pov_use_cases.py` | **States.** Says what this environment can run |

The third one is emphatically **not** a gate. A POV with no Entitle tenant still sees
every Entitle card; it is told they are out of scope for this environment. That is the
same promise the persona axis makes one layer up, and it is the property that keeps this
from becoming a second `install_profile`.

### Three states, and three words that are not the demo page's

| State | Meaning | Renders |
|---|---|---|
| `ready` | This POV has the products, and the wire-up has run for them | Links to the POV page tab |
| `needs_wiring` | The tenant is set, the artifact is not | No link; names what to run, points at the Wired tab |
| `out_of_scope` | This POV has no tenant for that product at all | No link, no action |

`out_of_scope` is deliberately not `masked`. Masked means *this instance's profile refuses
the feature* and no operator can change it. Out of scope means *this customer's POV was
not wired into that product* — a scoping decision, not a fault. Borrowing the word would
make a correctly scoped evaluation read as a misconfiguration, which is exactly the
impression this whole change exists to remove.

Absence beats unwired when a card names two products. A card needing PRA and Password Safe
on a POV with no PRA tenant cannot be run at all, so reporting "run the wire-up" would send
an operator to a button that will skip the half they came for.

### Where the line between the modules falls

`services/personas.py` may not import `database`. That rule is load-bearing —
`api/docs_pages` imports the module deliberately, to survive without the api layer — so
the POV resolvers take a **dict of booleans**, never a row:

```python
{"pra": bool, "password_safe": bool, "entitle": bool,      # does this POV INCLUDE it?
 "wired": bool, "onboarded": bool, "entitle_wired": bool}  # has the wire-up RUN for it?
```

`services/pov_use_cases.products_for` is the one function that turns a `PovEnvironment`
into that dict, and it reads the counts `pov_wireup.describe` already computes rather than
re-deriving them. The import direction is one-way and asserted by test.

The two halves of the dict are separate on purpose. "We did not include Entitle in this
evaluation" and "we have not wired Entitle yet" are different sentences with different
next actions, and only one of them is somebody's next click.

## Progress: the checklist

`PovUseCaseProgress` — one row per card somebody touched, keyed `(environment_id, card_id)`.

* **A row exists only for a card somebody touched.** Absence is "not started", the same
  reading `expires_at` has everywhere else in this codebase. Adding a card to the registry
  needs no backfill; enabling this on an existing estate selects zero rows.
* **The registry is the allowlist.** An unknown card id is refused with a 400 rather than
  stored. A progress table that accepts any string is a free-text store nobody can render,
  and these rows outlive registry edits — so the mistake would outlive it too.
* **A retired card leaves its row behind**, un-rendered. Deleting an SE's history on a copy
  edit is the worse outcome.
* **`state` is `done` or `skipped`**, and `skipped` is a real answer. "We showed them and it
  did not land" and "we never got to it" are different things to take into a renewal
  conversation, and only one is recoverable by running the demo.
* **Destroy keeps it.** `run_env_destroy` marks the POV row `destroyed` rather than
  deleting it, because it is the record of something that existed; these rows are that
  record's contents. What the destroy owes them is the summary, in the job log, at the
  moment somebody is closing the evaluation out — which is what `destroy_note` writes. The
  accessors of slice 2 are the opposite case: they are credentials, and they are deleted.

`note` and `checked_by_kind` ship in slice 1 though only slice 3 writes them, so that slice
needs no migration — the discipline `PovEnvironment` already follows with its wire-up
columns.

This is the first thing in the persona stack that **writes**, which is worth naming:
`services/personas` opens by arguing that a card navigates and never starts work. A tick is
not that — it spends nothing, builds nothing, reaches no tenant — and it does not live in
the persona layer at all. The registry stays a pure read; the write lands in `api/pov.py`,
behind the auth every other POV action already carries.

## Slice 2 — the ephemeral accessor

Built. The prospect gets a login of their own; slice 3 gives them something to do with it.

### Identity

Entitle's `create_actor` — or an SE pressing a button — mints a real `users` row, so login,
the sign-in throttle, MFA and the audit trail are the ones already here rather than a
second implementation of each. What makes it an accessor is one column:

```python
accessor_env_id = Column(String(36), nullable=True, index=True)   # non-NULL ⇒ accessor
```

plus a `PovAccessor` table holding the binding and its provenance: which POV, who minted
it, from where, and when it dies. The environment id is duplicated onto `User` deliberately
— the guard below runs on every request and must not need a join.

### The trap, and why the guard is where it is

`User.effective_permissions_dict` returns `{}` for a user none of the three permission
columns says anything about, and `require_permission` reads `{}` as **unrestricted** —
deliberate backward compatibility for the users that predate those columns. So an accessor
created "with no permissions" holds every permission in the dashboard.

So the confinement is a **path allowlist** in `api/auth.get_current_user`, the one
dependency every authenticated route resolves through — the same one-reader shape as
`feature_flags.enabled`. An allowlist and not a denylist: a route added tomorrow is refused
by default, which is the only direction this can fail safely in. Two entries today,
`/api/pov/accessor/self` and `/api/auth/me`, and a test pins that it has not grown.

Three surfaces resolve users *without* going through that dependency, so each is closed
where it lives:

| Surface | Why it matters | What it does now |
|---|---|---|
| `api/entitle_rest` | Grants dashboard permissions **up to administrator**, and resolves an actor by scanning users and matching a name. An accessor reaching that lookup is a direct escalation. | Every route filters `accessor_env_id IS NULL`, on the query rather than the response |
| `api/websocket._authenticate` | Its own resolver, so the path allowlist does not reach it. Job Live Output is not for a prospect. | Refuses an accessor on both the JWT and the PAT branch |
| `/api/users` | An admin `PATCH` setting `is_admin`, or clearing the binding, is two keystrokes to an operator account | Accessors are hidden from the list, and every mutation route — patch, deactivate, permanent delete, PAT mint — refuses one with a 409 naming the Access tab |

`require_admin` also refuses an accessor outright. No admin route is in the allowlist, so
that is unreachable today — which is the point: the day one is added by mistake, an
accessor still does not become an administrator.

The client-side half is **a convenience, not a control**. `/api/auth/me` reports
`accessor_env_id`, the Alpine store keeps it, and `requireAuth()` sends an accessor to
`/pov/access` rather than leaving them on a dashboard whose every call 403s. It reads
`localStorage`, which the holder can edit, and it runs in their browser. The gate consults
nothing the client sends.

### The Entitle side

`api/pov_accessor_rest.py` serves Entitle's **Ephemeral Accounts** route set — the half
`api/entitle_rest.py` deliberately omits, because dashboard users are permanent accounts
and these are the opposite case:

```
GET  /api/pov/accessor/rest/get_assets            live POVs, "pov:<env_id>"
GET  /api/pov/accessor/rest/get_all_permissions
POST /api/pov/accessor/rest/create_actor          mint; returns the credentials
POST /api/pov/accessor/rest/delete_actor          delete
POST /api/pov/accessor/rest/check_config
```

There is deliberately no `give_access`: in Ephemeral mode `create_actor` **is** the grant.

Three things are copied from `functions/fnworkloads/db_grant.py`, each learned expensively
there. The identity arrives in `provisioning_data`, not `actor` — reading only `actor` is
what made every real ephemeral grant come back 400. `delete_actor` refuses any username
without the `povguest_` prefix, checked on the name in the *request* before any lookup, so
a row that disagreed could not talk it into deleting an operator's account. And
`create_actor` returns the credentials, because handing them to the requester is the whole
point of the call.

Its secret is its own (`pov_accessor_rest_secret`), fail-closed in the same shape as the
standing adapter's: 503 when unset, one response for missing and wrong. An endpoint whose
job is minting logins must not authenticate with a credential that already grants
permissions.

### The manual path, and why it exists

`POST /api/pov/managed/{id}/accessors` mints one directly from the POV page's Access tab.
That is not a convenience feature — it is what makes this slice shippable and testable with
no Entitle tenant at all, and it is how the open question below gets answered.

The password is in that response **and nowhere else**. There is no reveal endpoint, unlike
the share link's: that password has to be re-readable because an SE reads it to a customer
days later, while an accessor that lost its password is replaced, which is one click and
leaves an audit line.

### Lifetime

An accessor keeps its own clock, on `pov_share`'s three-step rule — an explicit request,
else the POV's own `expires_at`, else 14 days — and it is **clamped** so an accessor can
never outlive the POV it can reach. Entitle owning expiry on its side is not the same as
this instance knowing when to stop trusting a login: an integration that is removed,
misconfigured, or simply never calls back would otherwise leave a working credential behind
forever.

### Reaping

`pov_accessor_service.teardown` runs **first** in `run_env_destroy`, taking the position the
share link used to hold. That position belongs to whatever a person outside the account can
be holding, and an accessor is that — but it is a credential into *this dashboard* rather
than a door into a lab, so its window is the one worth making shortest. Revoking **deletes**
the user row rather than deactivating it: an inactive row is still a username every actor
lookup in the codebase has to remember to skip, and the one that forgets is an escalation.

`expiry_reaper` enqueues the identical `pov_env_destroy` job, so the auto-delete timer is
covered by the same hook with no second code path.

The backstop is a sweep on the POV reconcile pass, **outside** its platform loop: a fresh
POV instance starts with the auto-delete timer off, and whether a prospect's login should
still work has nothing to do with whether Skytap is reachable. It catches accessors past
their expiry and accessors whose POV already reached `destroyed`.

The use-case record is **not** removed with them. That distinction is the point: an
accessor is a credential, and the checklist is the account of what the evaluation covered.

## Slice 2b — registering the adapter with Entitle (not built)

Today an SE points an Entitle REST integration at the routes above **by hand**. That is
deliberate, and it is also how the open question gets answered:
`entitle_registration_service.register_rest` records that Entitle's Ephemeral-mode
discriminator is unconfirmed against a live tenant. Registering by hand once tells you
whether the integration comes back showing "Standing Accounts" in its Connection dropdown,
which is the thing the automation has to get right.

Two things have to change before it can be automated, and the first is not optional:

* **`register_rest` takes no tenant context.** Every other `register_*` helper accepts an
  `EntitleTenantCtx`; this one does not, so it registers against the **global Entitle
  singleton**. Not a live bug — its only caller is `cloud_function_service`, behind
  `cloud_functions_enabled`, which is `_DEMO_ONLY` and so correctly uses the singleton. But
  a POV caller registering against the global tenant is precisely the silent cross-tenant
  mistake the tenant registry exists to prevent, so this is the first thing 2b fixes.
* **A lighter tenant context than `pov_wireup.entitle_context`.** That one refuses without
  an agent token and an SSH key — both real prerequisites for the SSH *connector*, which
  reaches a private target from inside the network. A REST adapter is called by Entitle over
  public HTTPS and needs neither. Reusing it would block accessor registration on
  prerequisites that do not apply, so the tenant resolution (owner id, workflow id) wants
  extracting and the SSH-specific checks want layering on top.

Then: `accessor_integration_id` / `accessor_tf_state` on the POV row, registration on
demand, and `deregister` in the teardown ahead of the accessor rows themselves.

## Slice 3 — the accessor writes

Built. `GET /pov/access` takes **no env id in the URL**, and neither does anything under
`/api/pov/accessor/self/*`: every one of them resolves the POV from
`current_user.accessor_env_id`. "Could an accessor write to somebody else's POV?" is not
guarded here — there is no parameter to guard.

That is also why the write routes needed **no change to the allowlist**. The allowlisted
prefix is `/self`, and everything under it is env-from-session by construction; a write
placed anywhere else would have had to widen that list, which is exactly the moment
somebody should stop and think.

### A comment is not a verdict

`PovUseCaseProgress.state` gained a third value, the empty string. Somebody typing
"couldn't get this working" on a card they have not marked is exactly the feedback the
feature exists to capture, and making them claim they covered it first would be a lie the
UI put in their mouth. `_summarize` counts `done` and `skipped` by name, so a note-only row
is neither — which is the truth.

### Two writers on one row

`set_state`'s `note` became tri-state, and this is the sharpest bug the slice could have
shipped:

```
None   leave whatever note is there alone   (the default)
""     clear it
text   replace it
```

The SE's tick button sends a state and no note. With `note` defaulting to `""` and written
unconditionally — which is what slice 1 did, correctly, when only one party wrote these
rows — an operator ticking a card the customer had just commented on would silently erase
the comment. That is the one piece of evidence in this feature that cannot be
reconstructed, and it is a regression test rather than a note here.

Every tick records `checked_by_kind`, the column slice 1 shipped unused, so the operator's
page can say whose it was. "We showed them" and "they did it themselves" are not the same
claim to take into a renewal conversation.

### What the accessor may see

Two projections, both written **for** this audience:

| | |
|---|---|
| `share_view` | The lab link and its expiry. Drops `share_id` — the publish set's own id, which exists so this dashboard can revoke exactly that one later and means nothing to a customer. No password: revealing it is its own POST, audited under the same action as the operator's own reveal, because it is a second door onto a live credential |
| `wired_view` | Per VM: what it is, where it is on their own lab network, and **which kinds** of access exist — a brokered session, a vaulted credential, access on request. Never `pra_jump_id`, `ps_managed_system_id`, `entitle_integration_id` or `wiring_error`: those are ids inside a *customer's own* appliance, meaningful only to teardown, and an error message written for an operator |

Naming what goes in, rather than subtracting from `api/pov._serialize`, is the point.
Subtraction is how one of them comes back on the next edit to the serializer.

The customer's note is rendered back on the operator's page with `x-text`, never `x-html`:
it is prose typed by somebody outside the account.
