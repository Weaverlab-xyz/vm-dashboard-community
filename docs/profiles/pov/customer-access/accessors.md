# Accessors: a customer login to one POV

> **Audience:** customer · **Profile:** `pov` · **Read this when:** you are giving the customer a login of their own that can only reach their POV.

Part of [What the customer sees](../customer-access.md).

**POV page → a POV's name → Access.**

The share link below is a door into the *lab*. An accessor is a door into *this dashboard*:
an ephemeral login, bound to one POV, that opens that POV's use-case checklist and nothing
else. It exists so the evaluation continues when nobody from your side is on the call, and
so the prospect's own view of what they have covered is theirs to keep.

## Accessor, or a real user scoped to one POV?

There are two ways to let somebody outside your team tick off use cases, and they are for
different people.

| | **Accessor** | **A user with a POV grant** |
|---|---|---|
| Who it is for | a prospect you are emailing a credential to | a named person — a customer champion, or a colleague covering the account |
| What they get | one POV's checklist, five endpoints, nothing else in the product | an ordinary dashboard login whose POV pages show only the POVs you named |
| How it is created | POV page → Access → mint | RBAC → Users → Permissions: `POV` = `read` + `use`, then pick their POV under the grid |
| Lifetime | expires, and is deleted when the POV is reaped | until you change it |
| Shows up on the RBAC → Users tab | no | yes |
| Can hold anything else | never | yes — whatever else you grant them |

Rule of thumb: if you would put their name in an org chart, give them a user with a POV
grant. If you are emailing a link to an evaluation, mint an accessor.

The `use` level is the important half of that grant. With `read` alone they can look at the
checklist but not tick it, and a checklist nobody can tick is a screenshot. With `write`
they could provision and destroy POVs. See [Permissions](../../../admin/permissions.md) for the
whole model; a POV they were not granted answers *"No such POV environment"*, the same as
an id that does not exist, because confirming somebody else's POV exists is itself a leak.

Three properties are not configurable, for the same reason the share link's three are not.

## It can only ever reach its own POV

Not "has few permissions" — **cannot reach anything else**. Every other route in the
dashboard refuses it server-side, on an allowlist rather than a blocklist, so a page added
next month is refused before anybody remembers to think about it. It is invisible on the
Users page, it cannot be granted anything through the Entitle integration that grants
dashboard permissions, it cannot open a job's Live Output, and it cannot be handed a
personal access token. An admin cannot promote one by editing it, either: those routes
refuse and point back here.

## It expires, and never outlives the POV

Fourteen days by default, and always shortened to the POV's own auto-delete date. Ask for
longer than the environment has left and you get what the environment has left.

Belt and braces, because the failure mode is a credential nobody associates with anything
any more: destroying a POV removes its accessors **first**, ahead of the share link; the
auto-delete timer goes through the same job, so a reaped POV takes its logins with it; and
a sweep on the POV reconcile pass catches anything the other two missed — expired logins,
and logins whose POV is already gone. That sweep does **not** depend on the auto-delete
timer being on, because a fresh POV instance starts with it off.

The use-case record is deliberately *not* removed with them. An accessor is a credential;
the checklist is the account of what the evaluation covered.

## The password is shown once

There is no reveal button, unlike the share link's. That link's password has to be
re-readable because you read it to a customer days later; an accessor that has lost its
password is **replaced**, which is one click, leaves an audit line, and ends with a
credential exactly one person ever saw. Minting and revoking are both audited, so "who
could log in to this POV" is answerable afterwards.

## What they can do with it

They land on their own page — their POV's name, the lab link, what is set up on each
machine, and the checklist.

**They tick cards off themselves**, and every tick is recorded as theirs, so your Use cases
tab distinguishes "we showed them" from "they did it". **They can leave a note on any
card**, and those come back to you on the same tab, marked as the customer's. A note is not
a verdict: somebody writing "couldn't get this working" on a card they have not marked
leaves exactly that, and the card stays unmarked.

Your ticks and theirs live on the same row, and neither erases the other's note.

They also get the lab link and, on request, its password — revealed one press at a time and
audited, the same as when you reveal it. What they do **not** get is anything that is an
identifier inside your appliances: no jump item ids, no managed account ids, no integration
ids, no tenant names, and none of the wiring errors written for you. That list is a
projection built for them, not your view with fields removed.

## Whether they are using it

The POV list's **Customer** column, and the **Customer** fact on a POV's page, answer the
question to ask before a check-in call: has the customer actually opened their login?

| Shows | Means |
|---|---|
| `no login` | no customer login was ever issued for this POV |
| `never signed in` (amber) | logins exist and nobody has used one |
| `signed in` | somebody signed in and has not ticked anything yet |
| `active` (green) | the customer ticked a use case themselves |

Underneath is how long ago the customer last did anything, whether a sign-in or a tick. On
the POV page it turns amber after a week.

A sign-in is recorded when an accessor completes one: their password, plus the FIDO2 step
if they have one. A token used all afternoon counts as one sign-in. The time is kept on the
accessor record as well as on the login, so it survives the accessor being revoked and the
POV being destroyed. Every dashboard account now records its last interactive sign-in in
the same way, whether by password, password and FIDO2, or SSO.

## Where they come from

Two ways, and the Access tab shows which for every row.

**By hand.** Fill in an email — a label, so you can tell two accessors apart; nothing is
sent to it — and press **New accessor**. Give the customer the username and password on the
spot. They sign in at the normal `/login` and land on their own page.

**From Entitle.** This dashboard hosts an Entitle **Remote Adapter** in Ephemeral Accounts
mode at `/api/pov/accessor/rest`, so a prospect can request access in Entitle and have the
login minted, and removed, by the grant itself. **Register with Entitle** on the Access tab
creates that integration in *this POV's own* Entitle tenant — not the instance's, and not
one shared between customers: the asset is the POV.

Four things have to be true first, and the button is replaced by the reason when one is not:

| | |
|---|---|
| This POV names an Entitle tenant | There is nowhere to register it |
| `pov_accessor_rest_secret` is set | Unset, the adapter answers **503** to everything, so the integration would be created and then reject every call Entitle made to it |
| This instance knows its own public URL | Entitle calls the adapter from its own cloud |
| That URL is HTTPS | Entitle will not call a plaintext endpoint |

The secret is deliberately **not** `entitle_rest_secret`: that one authenticates an
integration that grants dashboard permissions, and this one mints logins. Give Entitle the
same value as a bearer token.

⚠️  **Check the connection mode once.** Whether Entitle infers Ephemeral Accounts from the
route set, as this assumes, is unconfirmed against a live tenant. After your first
registration, open the integration in Entitle and confirm its **Connection** setting says an
Ephemeral option — if it says Standing, tell us, because the discriminator is real and
belongs in the registration. Minting an accessor by hand does not depend on this at all,
which is why that path exists.

Removing the integration does **not** revoke logins that already exist: those are accounts
here, not grants there. Destroying the POV removes the integration first and the logins
immediately after — in that order, because a live integration can mint a new accessor while
the destroy is running.

The asset Entitle sees per POV is `pov:<environment id>`, and the account it mints is
always named `povguest_…`. That prefix is load-bearing: the delete route refuses any name
without it, so the integration can never be talked into removing one of your accounts.
