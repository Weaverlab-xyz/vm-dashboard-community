# What the customer sees

> **Audience:** customer · **Profile:** `pov` · **Read this when:** you have been given a login or a link to a POV environment, or you are the one handing them out.

Part of [A POV Instance](README.md). The use-case checklist, PRA vendor access, the ephemeral accessor login, and the share link.

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

The instance-wide catalog is **not on that page at all** on a POV instance. It answers a
question about the *instance* — "can this instance run it?" — which a POV page has no use for,
and the twenty-six grey cards it produced here were the wall the lead exists to replace;
leaving them collapsed underneath left them one find-in-page away. An estate instance opens
exactly as it always did.

That page **reads**. Each row shows a ✓ or a – and the word for it, not a control: ticking a
card off lives on the POV's own page, which is where every other action on that POV lives too,
and a checkbox on a page that records nothing would be worse than no checkbox.

The page shows the cards **this POV can run**. The product mix decides what each one says:

| The card | What it means | What it offers |
|---|---|---|
| Live | This POV has the products, and the wire-up has run for them | A link to the tab that runs it |
| Needs wiring | The tenant is set and the artifact is not | What to run, and a link to the Wired tab |

A card for a product this POV has no tenant for is **not shown**, on your page or the
customer's, and a product with nothing else in it gets no heading either — a POV scoped to
Password Safe shows the Password Safe and environment cards, and that is the whole page.
Which products the evaluation includes is on the **Overview** tab, next to the tenants that
decide it; add a tenant there and that product's cards appear, with nothing to re-enable.

Those cards are not gone, only undrawn: nothing subtracts them from the catalog and no route
is closed. This is **not** the same state as an estate-only feature being masked on this
instance — nothing is wrong, and nobody needs to turn anything on from Settings. Earlier
builds did list them on your page, in a dashed *not part of this POV* section, which on a
one-product evaluation put two products' worth of rows you could not act on above the handful
you were there for.

A card that names two products needs both. On a POV missing one of them it is out of scope
rather than "needs wiring" — "run the wire-up" would send you to a button that skips the
half you came for.

### Ticking them off

Each card has a **checkbox** and a **Skip** control, and on your page clicking anywhere on the
row that is not a link or a button ticks it too. The count rides on the row in the POV list.
The customer's version of the same row has a checkbox, a **Not for us** control, and a note box
that is always visible rather than behind a control — a comment behind a click is a comment
nobody leaves.

`Skip` is a real answer rather than a way to hide a card: "we showed them and it did not
land" and "we never got to it" are different things to walk into a renewal conversation
with, and only one of them is fixed by running the demo. Every tick records who made it —
which matters more once the customer can make their own, which is the
[accessor](design/use-cases.md#slice-2--the-ephemeral-accessor) slice.

### Sample activity before a demo

A POV wired an hour ago has an empty request history, so the first Password Safe screen you
show is a blank table. The **Sample activity** card at the top of the Use cases tab fills it
in. **Password Safe check-outs** requests, releases and checks back in the credentials of
up to five of this POV's onboarded accounts, so the request and release history show real
activity.

**This writes to the customer's tenant, and it is meant to.** So:

- the button asks you to confirm first, naming what it writes
- the request is written to the audit log as `pov_seed_requested`
- every request's reason is tagged `[pov-seed:<pov>:<set>] Sample activity for a demo`, so
  anyone reading the tenant can tell it apart from real use
- check-in is the plain one, so **nothing is rotated**
- the released credential is discarded the moment it is read, and is never logged or kept
- it touches only the accounts this POV's wire-up onboarded

It cannot be undone. Request and release history is the tenant's audit trail, and the
dashboard will not edit it. The card shows when it last ran and how many requests Password
Safe accepted. A run Password Safe refused completely fails its job with Password Safe's
own message, for example `4031`, which means the API identity needs the Requestor role. It
can run again after a minute.

Some activity is deliberately not seeded:

- **PRA sessions:** no API creates one, because a session needs a representative.
- **Entitle requests:** the dashboard's request client uses the instance-wide Entitle
  credentials, not this POV's tenant, and an auto-approved request is a real grant.
- **EPM for Linux events:** there is no events API. To produce some, run a script on a guest
  with a [guest step](ps-runbook.md#running-a-step-on-a-guest).

### Destroying a POV keeps this

Everything else in a teardown is removed. This is not: the POV row itself is marked
`destroyed` rather than deleted because it is the record of something that existed, and the
use-case history is what that record contains. The destroy job logs the summary — *"use-case
record kept: 9 of 14 run, 2 skipped"* — at the moment somebody is closing the evaluation
out, which is the one time anybody reads it.

The design note, including the accessor identity that is **not** kept, is in
[design/use-cases.md](design/use-cases.md).

---

## Pages

The ways in have a page each:

| Page | Read it when |
|---|---|
| [Vendor access to a POV](customer-access/vendor-access.md) | a third party needs into the lab, with its own login, TOTP and an expiry |
| [Accessors: a customer login to one POV](customer-access/accessors.md) | you are giving the customer a login of their own that can only reach their POV |
| [The customer-facing share link](customer-access/share-link.md) | you want to share a POV's results with the customer through a password-protected link that expires |

---

## What a POV leaves behind

**POV page → Past** tab, or the **Summary** tab on any POV. The Past tab appears once a
POV has been destroyed.

Destroying a POV removes the environment, the logins, the jump items and the integrations.
It does not remove the account of what the evaluation covered — that row is marked
`destroyed` rather than deleted precisely because it is the record of something that
existed, and the checklist is that record's contents.

Until now nothing could reach it. The table of POVs filters destroyed ones out, which is
right for a list of things you can act on, so a finished evaluation appeared nowhere. **Past
POVs** is the way back: name, when it started, what it was wired into, how much was covered,
and whether the customer worked it themselves. Clicking one opens its Summary.

### The summary, and one distinction in it

The Summary tab is the same on a live POV and a finished one: coverage overall and per
product group,
every card somebody ticked or skipped, and **what was said** — the customer's own notes,
marked as theirs.

One thing it is careful about. *"A login was issued"* and *"they used it"* are different
facts, and only the second is evidence. The page says **the customer worked the checklist
themselves** only when a card was actually ticked by them; the Past POVs table reports the
two separately, so a login nobody opened reads as exactly that.

**Copy as Markdown** puts the whole thing on the clipboard — for a renewal note, a CRM, or an
internal write-up. It is copied rather than downloaded because that is where it is going
anyway.

### The close-out report

**Open report** on the Summary tab opens a page laid out for print. Use the browser's
**Print / Save as PDF** to turn it into the document you send after a technical win. It
lists:

- what was covered and skipped, by area, with who marked each one and when
- what was said, attributed to the customer or your team
- every sign-off

It works for a finished POV as well as a running one.

The customer's name and the opportunity appear as the placeholders **«Customer name»** and
**«Opportunity»**. Replace them in the PDF, or in the document you paste the report into.
The page has no field to type them into, deliberately: this database is not approved to
hold either. That is the same reason a POV has no customer field. The report also never
shows an email address. A customer appears as *Customer*, followed by their generated
login name.

### Signing it off

A sign-off records that somebody agreed the coverage is what was evaluated. There are two
ways to give one:

- **Sign off** on the Summary tab, for you or another SE
- **Confirm what we covered** at the bottom of the customer's own page, for the customer

Both need at least one use case marked as covered, and both are written to the audit log.

A sign-off is a **snapshot**. If more is ticked after somebody signs, their sign-off is shown
as *earlier version*, and the report says so too. It is never stretched over coverage it
did not see. The customer's page then offers **Confirm** again. Pressing the button twice
on the same coverage records one sign-off, not two.

**Withdraw** marks a sign-off withdrawn. It is never deleted, and the report lists it struck
through, because a record that could vanish is not worth asking a customer for. A sign-off
carries no comment field. What the customer thought of each use case is already in their
notes, in their own words.

---

## Failure modes

**The share link 400s with "has no share links".** The platform's `share_link` capability
is false. Give the customer access through PRA instead — the wire-up's jump items already
point at these VMs.

**"this POV has no stored share password".** The link predates the stored password, or the
password was cleared. Re-share: it publishes a new URL and a new password together, which
is the only way to get back to a consistent pair.

**"publishing the share link failed: … publish_sets failed (400): …
`Expiration date tz is invalid`".** Skytap did not accept the expiry's timezone. Before
this was fixed the dashboard sent no timezone at all and *every* share failed this way; a
current build sends `UTC`, and the message names the zone it sent. If it recurs, that
account wants a different one — `SHARE_EXPIRY_TZ` in `services/skytap_service.py`.
