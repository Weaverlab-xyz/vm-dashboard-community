# Design: use cases per POV, and the accessor who ticks them off

> **Audience:** contributor · **Profile:** `pov` · **Read this when:** you are changing the per-POV use-case checklist or who may tick it off.

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

## Slice 2b — registering the adapter with Entitle

Built. **POV page → Access → Register with Entitle** creates the REST integration in
*this POV's own* Entitle tenant, so a prospect requests access there and the login is
minted, and removed, by the grant itself.

### The gap it had to close first, which was two gaps

`register_rest` took no tenant context. Without one it authenticates with the **global**
Entitle key *and* writes the **global** owner and workflow ids into the HCL — two separate
readers of the singleton, and threading the context through only one of them is worse than
neither: you authenticate as one tenant and name another's ids. So `ctx` now reaches both
`_hcl_fields(ctx)` (whose ids the HCL names) and `_apply_hcl_sync` (whose key applies it),
and a test pins both halves.

This was never a live bug. The one caller was `cloud_function_service`, behind
`cloud_functions_enabled`, which is `_DEMO_ONLY` — on a demo instance the singleton *is*
the tenant, and that path is unchanged: with no `ctx` the fallback is exactly what it was.

### A lighter tenant context

`pov_wireup.entitle_context` refused without an agent token and an SSH key. Both are real
prerequisites for the **SSH ephemeral-accounts connector**, which reaches a private target
from inside the network — and neither applies to a REST adapter, which Entitle calls over
public HTTPS. Reusing it would have blocked accessor registration on prerequisites that do
not exist for it.

So `entitle_tenant_ctx` is the part every integration in a tenant shares — whose tenant, and
the owner and workflow ids `_common_attrs_hcl` refuses without — and `entitle_context` layers
the SSH-specific three (agent token, sudo user, private key) on top rather than duplicating
the resolution.

### Everything is checked before anything is created

A registration that half-succeeds leaves an integration in a customer's Entitle tenant that
this dashboard has no state for and therefore cannot remove. So each prerequisite is a
refusal with the remedy in it, reported on the row as `accessor_blocker` and shown on the
page **instead of** the button rather than beside a live one:

| Missing | Why it is fatal rather than a warning |
|---|---|
| An Entitle tenant on this POV | There is nowhere to register it |
| `pov_accessor_rest_secret` | The adapter answers 503 to everything, so the integration would be created and then reject every call Entitle made — which looks like an Entitle fault and is not |
| This instance's public URL | Entitle calls the adapter from its own cloud |
| HTTPS on that URL | Entitle will not call a plaintext endpoint — the same refusal, for the same reason, the broker enrolment makes about the agent endpoint |

### Teardown: the tap before the drain

`pov_accessor_entitle.teardown` runs **before** `pov_accessor_service.teardown`, and that
order is the point rather than housekeeping. While the integration is live Entitle can mint
a **new** accessor, so removing the logins first races a destroy against a grant and can
leave one created after the step that removed them. The destroy order is now: the
integration, the logins it mints, the share link, then the customer's appliance objects.

A failed deregistration **keeps** the terraform state so a re-run can finish it — the rule
the per-VM wire-up teardown already follows. An integration left in a customer's tenant is
untidy; one this dashboard can no longer find is worse, and it is standing access.

### The open question, still open

Entitle's Ephemeral-mode discriminator is **unconfirmed against a live tenant**:
`register_rest` assumes the API infers the mode from `allow_creating_accounts` and the route
key set. The person who can settle it is the operator who just pressed Register, so the page
asks them — open the integration in Entitle and check its **Connection** setting says
Ephemeral Accounts. If it says Standing, the discriminator is real and belongs in
`register_rest`. Until somebody has looked, the SE-driven mint is the path that does not
depend on the answer, and it is why that path exists.

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

## Slice 4 — the page an SE actually clicks

Built, and it is the one that closes the complaint this document opens with.

Slices 1 to 3 built the per-POV checklist and put it a click deeper, on the POV's own page.
The nav's **Use cases** page was left exactly as it was — on purpose, with a test pinning
that it must not change — so it went on opening to twenty-six greyed-out cards saying "Not
available on a POV instance". The wall survived four merges because the fix had been built
somewhere else.

So on a POV instance that page now **leads with a POV**: pick one from the selector, get its
checklist. Measured on the same instance, that is 26 masked cards leading the page before and
25 runnable ones after.

The instance-wide catalog keeps its place underneath and is **collapsed, never filtered**.
Every group and every card is still rendered, the toggle says what is behind it, and the page
says why it is all still there. The distance between *collapsed* and *removed* is the whole
argument this page rests on — a persona or a profile may reorder and emphasise, never
subtract — so both halves are pinned: the groups assignment may not `.filter(`, and the
collapse may not use `x-if`, which would take them out of the DOM. That is filtering with
extra steps. A demo instance opens on exactly what it opened on before.

No backend. The lead reads `/api/pov/managed` and the same `/api/pov/managed/{id}/use-cases`
the POV's own page reads, so the two cannot disagree about what a POV can run, and every way
that read can fail — a demo instance 404s the router, an expired session 401s — is a
non-event rather than an error on a page whose main content loaded fine. It performs no
writes: ticking a card off stays on the POV's own page, where every other action on that POV
already lives.

## Slice 5 — what a POV leaves behind

Built. The record was never the problem: `run_env_destroy` marks the row `destroyed` rather
than deleting it, and `pov_use_cases.destroy_note` deliberately keeps the checklist as that
record's contents. What was missing is that **nothing could reach it**. `api/pov.list_managed`
filters destroyed rows out — right for a list of things you can act on — so a finished
evaluation appeared nowhere and you needed its raw uuid to see it again. Evidence that
survived teardown with no way back to it is the same as losing it at the moment somebody
wants it, which is a renewal conversation weeks later.

### Two questions, two shapes

`services/pov_summary` answers them separately because they are separately asked:

| | |
|---|---|
| `archive(db, limit)` | "Which evaluations have we run?" A light row per POV, and deliberately **not** `_serialize`: that builds five describes per row — gateway, resource broker, wire-up, share, accessors — each asking a question about a *living* environment, every one meaningless and a wasted query for a POV that is gone. Coverage comes from one aggregate over the progress rows rather than resolving the whole catalog per row. It says when it truncated, because a list silently cut is one an SE trusts and should not |
| `build(db, env)` | "What happened in that one?" The whole account: coverage, per role, every card somebody touched, and what they said |

`/api/pov/managed/archive` is declared **before** `/managed/{env_id}` — the third time this
repo has met that trap, and pinned for the third time. Below it, "archive" is captured as an
environment id and the endpoint answers "No such POV environment".

`/summary` has no status filter, deliberately: it is the endpoint written for after a POV is
over, and filtering the finished ones out would leave it describing only the evaluations
whose story is not finished being told.

### The claim the data supports

**"They took part" means they ticked something.** A login issued and a login used are two
different facts, and only the second is evidence. `took_part` is computed from progress rows
carrying `checked_by_kind='accessor'` — never from the existence of a `PovAccessor` — and the
archive table reports the two separately: *"2 ticked by them"* against *"1 login, unused"*.
Conflating them would put a claim in a renewal conversation that the data does not support.

Only cards somebody touched are in the summary. A summary listing every untouched card would
be the catalog again, and the catalog is not what happened. Roles with nothing in scope are
dropped from the breakdown for the same reason — a Password-Safe-only evaluation has several,
and "0 of 0" against each is noise in a document read once.

### Nothing here writes or reaches the network

A summary that made platform calls would fail for exactly the POVs it exists to describe,
whose environment is gone. Products are read off the row — the only thing that *can* work for
a destroyed POV, and the honest question for a live one, since what an evaluation covered was
decided when its tenants were chosen.

The take-away is **copied, not downloaded**: what an SE does with this is paste it into a
renewal note or a CRM, and a file on disk is one more step to the same place. The markdown is
built in the browser from what the page already loaded.
