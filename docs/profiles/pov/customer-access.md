# What the customer sees

> **Audience:** customer · **Profile:** `pov` · **Read this when:** you have been given a login or a link to a POV environment, or you are the one handing them out.

Part of [A POV Instance](README.md). The use-case checklist, the ephemeral accessor login, and the share link.

## Use cases, per POV

**Use cases in the nav, or POV page → a POV's name → Use cases.** Both lead to the same
checklist; the first picks the POV for you, the second is where you act on it.

The in-app [Use cases](../demo/personas/README.md) catalog is instance-wide: it asks `feature_flags` whether
a demo can run here, and on a POV instance the answer for most of it is "no, this profile
masks that". Correct, and useless in front of a customer — it was twenty-six greyed-out
cards saying so. So **on a POV instance that page leads with a POV**: pick one from the
selector and you get its checklist, asking a different question — **can I run this on THIS
POV?** — whose answer comes from the three tenant columns on the row, which is exactly where
a Password-Safe-only evaluation differs from an all-three one.

The instance-wide catalog is still there, underneath, collapsed behind one click. Collapsed
and not removed: nothing on this page is ever hidden from you, and a demo instance opens
exactly as it always did.

Every role and every card is always on the page. The product mix decides what each card
**says**, never whether you can see it:

| The card | What it means | What it offers |
|---|---|---|
| Live | This POV has the products, and the wire-up has run for them | A link to the tab that runs it |
| Needs wiring | The tenant is set and the artifact is not | What to run, and a link to the Wired tab |
| Not part of this POV | No tenant for that product on this row | Nothing — the fix is a decision about the evaluation, not a button |

That last row is the one worth reading twice. It is **not** the same state as a demo-only
feature being masked on this instance: nobody can turn it on from Settings, because there
is nothing wrong. A POV scoped to Password Safe is a normal POV, and a page that greyed
those cards out as unavailable would say the opposite.

A card that names two products, on a POV missing one of them, reads "not part of this POV"
rather than "needs wiring". "Run the wire-up" would send you to a button that skips the
half you came for.

### Ticking them off

Each card has **Mark done** and **Skip**, and the count rides on the row in the POV list.

`Skip` is a real answer rather than a way to hide a card: "we showed them and it did not
land" and "we never got to it" are different things to walk into a renewal conversation
with, and only one of them is fixed by running the demo. Every tick records who made it —
which matters more once the customer can make their own, which is the
[accessor](design/use-cases.md#slice-2--the-ephemeral-accessor) slice.

### Destroying a POV keeps this

Everything else in a teardown is removed. This is not: the POV row itself is marked
`destroyed` rather than deleted because it is the record of something that existed, and the
use-case history is what that record contains. The destroy job logs the summary — *"use-case
record kept: 9 of 14 run, 2 skipped"* — at the moment somebody is closing the evaluation
out, which is the one time anybody reads it.

The design note, including the accessor identity that is **not** kept, is in
[design/pov-use-cases.md](design/use-cases.md).

---


## Accessors: giving the customer a login

**POV page → a POV's name → Access.**

The share link below is a door into the *lab*. An accessor is a door into *this dashboard*:
an ephemeral login, bound to one POV, that opens that POV's use-case checklist and nothing
else. It exists so the evaluation continues when nobody from your side is on the call, and
so the prospect's own view of what they have covered is theirs to keep.

Three properties are not configurable, for the same reason the share link's three are not.

### It can only ever reach its own POV

Not "has few permissions" — **cannot reach anything else**. Every other route in the
dashboard refuses it server-side, on an allowlist rather than a blocklist, so a page added
next month is refused before anybody remembers to think about it. It is invisible on the
Users page, it cannot be granted anything through the Entitle integration that grants
dashboard permissions, it cannot open a job's Live Output, and it cannot be handed a
personal access token. An admin cannot promote one by editing it, either: those routes
refuse and point back here.

### It expires, and never outlives the POV

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

### The password is shown once

There is no reveal button, unlike the share link's. That link's password has to be
re-readable because you read it to a customer days later; an accessor that has lost its
password is **replaced**, which is one click, leaves an audit line, and ends with a
credential exactly one person ever saw. Minting and revoking are both audited, so "who
could log in to this POV" is answerable afterwards.

### What they can do with it

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

### Where they come from

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

---


## What a POV leaves behind

**POV page → Past POVs**, or the **Summary** tab on any POV.

Destroying a POV removes the environment, the logins, the jump items and the integrations.
It does not remove the account of what the evaluation covered — that row is marked
`destroyed` rather than deleted precisely because it is the record of something that
existed, and the checklist is that record's contents.

Until now nothing could reach it. The table of POVs filters destroyed ones out, which is
right for a list of things you can act on, so a finished evaluation appeared nowhere. **Past
POVs** is the way back: name, when it started, what it was wired into, how much was covered,
and whether the customer worked it themselves. Clicking one opens its Summary.

### The summary, and one distinction in it

The Summary tab is the same on a live POV and a finished one: coverage overall and per role,
every card somebody ticked or skipped, and **what was said** — the customer's own notes,
marked as theirs.

One thing it is careful about. *"A login was issued"* and *"they used it"* are different
facts, and only the second is evidence. The page says **the customer worked the checklist
themselves** only when a card was actually ticked by them; the Past POVs table reports the
two separately, so a login nobody opened reads as exactly that.

**Copy as Markdown** puts the whole thing on the clipboard — for a renewal note, a CRM, or an
internal write-up. It is copied rather than downloaded because that is where it is going
anyway.

---


## The customer-facing share link

Everything above is something an SE touches. This is the one artifact a **customer** opens:
a single URL onto the POV's desktops, published by the lab platform. On Skytap it is a
*publish set*; on the POV page it is the **Share** column.

Three properties are not configurable, because each one exists to stop a specific way a
POV link goes wrong.

### It always has a password, and the password is generated

Skytap treats the publish set's password as optional. This does not, and there is no field
to type one into. A blank password box is how an anonymous door into a lab holding a
Gateway, a Resource Broker and a set of Entitle integrations gets left open — and an SE's
usual password is barely better.

So the dashboard generates a 20-character password, hands it to the platform, and stores it
Fernet-encrypted alongside the POV's other secrets (`pov/<env-id>/share_password`, the same
shape as the Gateway deploy key). It is shown once when you share, and re-readable
afterwards from the **password** button — that read is audited, because "who has this
link's password" is the first question asked when a URL turns up somewhere it should not
have.

The alphabet excludes `I l 1 O 0`. These get read down a phone line more often than anyone
plans for.

### It always expires

There is no "never" option. The failure mode of a POV share is not that someone guesses the
URL — it is that the link *outlives everyone's attention*: the evaluation ends, the
environment sits on `suspend_on_idle`, and the URL keeps working for months.

The expiry is chosen in this order:

1. what you asked for, if you typed a number of days (1 to 90, refused outside that rather
   than clamped);
2. the POV's own auto-delete date, if it has one — so the link cannot outlive the
   environment it points at;
3. otherwise 14 days.

Skytap enforces the expiry itself, so an expired link is already dead. The page shows it as
`expired` rather than clearing the row, because during an evaluation the fact that a link
*was* shared is worth keeping.

### Re-sharing replaces; revoking is its own button

**Re-share** revokes the current publish set before creating a new one, and mints a new
password. Leaving the old one would give the POV two live URLs and one stored id — the
second could never be revoked from here again. It also means re-sharing is a real remedy
when a link went to the wrong person.

**Revoke** kills the link without touching the POV. The platform call happens first and its
failure is *not* swallowed: clearing the row while the URL still worked would leave a live
share nobody could find, let alone kill.

Both are also reachable from the API:

```
POST   /api/pov/managed/{id}/share          {"days": 7}   -> the link and its password
DELETE /api/pov/managed/{id}/share                        -> revoke
POST   /api/pov/managed/{id}/share/reveal                 -> the password again, audited
```

The URL is shown with a **copy** button rather than as a hyperlink. It is a customer-facing
address, and an accidental middle-click from this page is an SE opening a session as the
customer.

### A platform that cannot do this

The **Share** column reads `not supported` when the platform's `share_link` capability is
false, and the API refuses with a message pointing at PRA instead — the jump items the
wire-up created already reach these VMs, which is the better answer for a customer who
needs *audited* access rather than a URL. Skytap supports it; the column exists so the
second adapter degrades explicitly instead of 500ing from inside a job.

### Teardown

Destroy revokes the share **before** anything else, ahead even of the PRA jump items. It is
the only artifact somebody outside the account can be holding, so the window where it still
works is the one worth making shortest.

It never blocks the destroy. Deleting the environment removes its publish sets server-side
anyway, so a failed revoke costs a note in the job log rather than a stranded POV — and the
row is cleared regardless, so a destroyed POV is never left advertising a link.

---


## Failure modes

**The share link 400s with "has no share links".** The platform's `share_link` capability
is false. Give the customer access through PRA instead — the wire-up's jump items already
point at these VMs.

**"this POV has no stored share password".** The link predates the stored password, or the
password was cleared. Re-share: it publishes a new URL and a new password together, which
is the only way to get back to a consistent pair.
