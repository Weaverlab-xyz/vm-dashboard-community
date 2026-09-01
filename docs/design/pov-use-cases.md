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

## Slice 2 — the ephemeral accessor (not built)

The prospect ticks cards off themselves. That means an identity for somebody outside the
account, which exists only as long as the POV does.

### Identity

Entitle's `create_actor` mints a real `User` row, so login, MFA, audit and sessions are the
ones already here — plus one column that is the whole security model:

```python
accessor_env_id = Column(String(36), nullable=True, index=True)   # non-NULL ⇒ POV accessor
```

and a `PovAccessor` table for the binding's detail (environment, user, Entitle request id,
expiry). The env id is duplicated onto `User` deliberately: the guard below runs on every
request and must not need a join.

### The trap, stated before the code

`User.effective_permissions_dict` returns `{}` for a user with no permissions, and
`require_permission` treats an empty dict as **unrestricted** — backward compatibility with
pre-OIDC users. So an accessor created with "no permissions" would have *every* permission.

**Deny by default, in one reader.** `api/auth.get_current_user` is the single dependency
every authenticated route resolves through — the same one-reader shape as
`feature_flags.enabled`. It gains a check: an accessor reaching any path outside an
**allowlist** (`/pov/access`, its own API, `/api/auth/me`, logout, `/static`) gets a 403.
`require_admin` refuses an accessor unconditionally. Accessors are excluded from
`/api/users`, from `entitle_rest.get_actors` and from `get_all_permissions`. Every one of
those is a silent failure if missed, so every one gets a test.

### The Entitle side

A new router serving Entitle's **Ephemeral Accounts** route set — the half
`api/entitle_rest.py` deliberately omits, because dashboard users are permanent accounts
and these are the opposite case:

```
GET  /get_assets            live POVs, identifier "pov:<env_id>"
GET  /get_all_permissions
POST /create_actor          mint the accessor, return its credentials
POST /delete_actor          delete it
POST /check_config
```

Reuse rather than reinvent:

* `api/entitle_rest._require_secret`'s pattern, with **its own secret** — an endpoint that
  mints logins must not share a credential with one that grants permissions. Its prefix
  joins `main._SETUP_503_PREFIXES`, for the reason the existing one is there.
* `functions/fnworkloads/db_grant.py` is the working reference for this contract and carries
  two hard-won details: in Ephemeral mode the identity arrives in `provisioning_data`, not
  `actor`; and `delete_actor` must refuse any username without the ephemeral prefix, so
  Entitle can never delete an operator's account.
* In Ephemeral mode `create_actor` **is** the grant — there is no `give_access` — and it
  returns the credentials Entitle hands the requester.
* `entitle_registration_service.register_rest(..., ephemeral=True)` registers the adapter
  against **this POV's own** Entitle tenant; `deregister` removes it at teardown. One
  integration per POV, because the asset is the POV.

**Known unknown:** `register_rest` already records that Entitle's Ephemeral-mode
discriminator is unconfirmed against a live tenant. So slice 2 ships a manual path as well —
an SE can mint an accessor from the POV page — and the Entitle path is the automation on
top rather than the only way in.

### Reaping

`pov_accessor_service.teardown` runs in `run_env_destroy` **first, ahead of the share
link**. The current ordering comment gives the share link that position because it is "the
only artifact somebody outside the account can be holding"; an accessor is that too, and it
is a credential into this dashboard rather than a door into a lab. It deletes the users and
bindings, then deregisters the Entitle integration; it never raises.

`services/expiry_reaper` enqueues the identical `pov_env_destroy` job, so auto-delete is
covered by the same hook with no second code path. A sweep on `main._pov_reconcile_loop`
catches accessors whose expiry passed while the POV lives on.

## Slice 3 — the accessor page (not built)

`GET /pov/access` — **no env id in the URL.** The accessor's own `accessor_env_id` decides
which POV; an id in the path is an invitation to try someone else's.

A standalone template, not `base.html`: an accessor must never render the SE nav, and
inheriting it would make that a CSS problem rather than a structural one.

It carries the checklist (ticked with `checked_by_kind='accessor'`, so the SE's page can
tell who ran what), a note per card, the lab share link with its password revealed one row
at a time, and a read-only list of what was wired — jump item names and hosts, managed
account names, integrations. **No credentials, no tenant ids, no terraform state:** a
projection written for this page, not `_serialize` with fields removed.
