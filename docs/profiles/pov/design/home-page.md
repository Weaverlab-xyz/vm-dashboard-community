# Design: the home page on a POV instance

> **Audience:** contributor · **Profile:** `pov` · **Read this when:** you are changing what the dashboard's landing page shows, on either profile.

## The problem

`templates/dashboard.html` is an estate overview: tile bands for clouds, hypervisors,
containers and managed services, a quick-deploy strip, a persona use-case band, and the
jobs list. Every one of those is gated on something a POV instance does not have.

Walked as a POV install, the page came out like this:

| Band | What a POV instance saw |
|---|---|
| Cloud Infrastructure | gone — the four cloud keys are forced false by the `cloud_pages` page group |
| Hypervisors | gone — all six flags are estate-only |
| Containers | one tile, Gateways |
| Managed Services | one tile, OT Demo Cells — **whose only link 404s here** |
| Quick deploy | gone — all eight shortcuts are estate-only |
| Workgroup actions | gone |
| Use cases for *persona* | rendered, and every card grey: "Not available on this instance" |
| Overview | Active Jobs, Deployed Resources |

So the landing page of a POV install was an overview of nothing, with one dead link and one
band that existed only to say it had nothing to offer. Everything an SE actually needed was
a click away on `/pov`, and nothing on the home page said so.

## What it leads with instead

The POVs, and what each one needs next.

That sentence is not new work. `GET /api/pov/managed` already returns, per POV, the setup
ladder from `services/pov_setup_steps` — a pure function over the row the endpoint had
already built, which names the next step and says why it cannot be pressed yet. It is the
same ladder `/pov` renders row by row. Putting it on the home page is presentation over
data that was already computed, and the two surfaces read one serializer, so they cannot
disagree about what to press next.

Beside it: three tiles (`pov_active`, `pov_guests`, `pov_coverage`), POV rows in **Needs
attention**, and the persona use-case band suppressed.

## The decisions worth knowing before you change it

### The gate is the profile. It is not a persona.

`services/personas` is curation only — it may reorder and emphasise, never hide. The
profile is the other axis, the one that subtracts and already 404s whole pages. This band
appears and the persona band disappears because of the **profile**, and no persona key
appears anywhere in either.

Suppressing the persona band is a subtraction, so it has to leave a way back: `/use-cases`
stays in the nav and renders the POV checklist there rather than the estate catalog. The
band is wrapped, never deleted — a guard suite pins that it is still gated on a persona
being set, and measures how far into its own marker comment that gate appears.

### The branch reads `/api/features`, not `/api/persona`

Both carry `install_profile`. `visibleTiles` already reads `this.features`, and gating
bands off one source while gating tiles off another is two answers to one question waiting
to disagree. It is also settled before the first paint, in an await the page already makes.

Not the Jinja context either, although `_profile_context` offers it: five guard suites
parse this template **as text**, and a `{% if %}` inside those literals either breaks the
parse or leaves a tile key visible to the parity scan that never renders.

### One request, three consumers

The band, the tiles' link targets and the POV rows in Needs attention all read a single
`loadPovs()`. `_serialize` costs a handful of indexed queries per POV; a second fetch would
be a second answer to one question and a second helping of those queries, on every poll, in
every open tab.

For the same reason the list is **polled only while something is in flight** — a POV lives
for weeks, and re-reading a settled one every 20 seconds from every forgotten tab is the
DB-pool exhaustion this page was rebuilt to remove. A settled list refreshes on focus.

If that ever measures too heavy, add `?view=summary` to the *same* endpoint and have
`_serialize` skip the spreads a summary does not need. A second route would be a second
projection of one row, which is the reader/writer drift this codebase keeps eliminating.

### The attention rows are free, and one of them is deliberately absent

They are derived from `this.povs` in memory, so the panel composes the same six requests it
always did.

**There is no POV expiry rule, and adding one would be a regression.**
`inventory_service.collect` queries `PovEnvironment` unconditionally, so a POV nearing its
auto-delete already appears as an inventory-sourced item. A second would mean two rows per
POV with separate dismissals, and dismissing one would silently leave the other.

Two rules exist that look redundant and are not:

- **broker offline** has its own rule because the ladder reports an offline agent as
  `ready`, not `blocked`, so the blocked rule never sees it.
- **guests with no OS** is the `else` branch of the blocked rule. When *every* guest is
  unknown the ladder is already `blocked` and names it; the separate rule carries the
  partial case, where the ladder says `ready` and the wire-up will quietly skip exactly
  those guests and still report success.

The panel caps at eight items, so POV rows roll up past three. One badly-behaved POV can
raise five on its own, which would otherwise bury every failed job on the instance.

### Links, never buttons

The next-step line links into the tab of `/pov/<id>` that owns that step. The dashboard
holds a row and a step name; the POV page holds the handlers, the refusals and the
confirmations. A button here would either send blind or need its own copy of that decision
— the same reasoning the workgroup card on this page already records.

### Two things that look like bugs and are not

**`deployed_resources` stays.** It reads as an estate tile, but
`inventory_service.collect` includes `PovEnvironment` rows, so on a POV instance it counts
the POVs and links to the page that carries their auto-delete timers. Hiding it would have
required a new per-tile profile gate for no gain.

**The OT tile hides itself via `cloud_pages`.** That is the *same* key the nav link and
`main._profile_page_gate` read, so the tile, the nav and the route cannot disagree — which
is the rule `profile_page_allowed` exists to enforce. `api/dashboard._ot_cells` holds the
server half and refuses to compute a number whose only link 404s. This is why `cloud_pages`
is now forwarded into the map `/api/features` serves.

### Three traps this page sets for its next editor

All three are enforced, and all three fail with a message that blames something else:

1. A tile key or comment inside the tile catalog must not contain the string that names the
   persona card band — a card scanned as a tile fails the collector-parity suite with a
   message about a missing collector. The coverage tile is keyed `pov_coverage` for exactly
   this reason.
2. A comment sitting between a tile's opening brace and its `key` hides that tile from the
   parity scan entirely, so it renders "unavailable" forever with nothing to say why.
3. A comment that explains one of these banned literals **by quoting it** fails the very
   guard it is explaining. Describe them; this page does.

## Where the code is

| What | Where |
|---|---|
| The band, the tiles, the attention rules | `web_dashboard/templates/dashboard.html` |
| The three tiles' numbers, and the OT console guard | `web_dashboard/api/dashboard.py` (`_pov_tiles`, `_ot_cells`) |
| The ladder's colour map, shared with `/pov` | `web_dashboard/static/js/app.js` (`povStepClass`, `povStepMark`) |
| The ladder itself | `web_dashboard/services/pov_setup_steps.py` |
| The rows | `web_dashboard/api/pov.py` (`_serialize`) |
| The guards | `tests/test_dashboard_pov_home.py`, and the POV half of `tests/test_dashboard_stats_api.py` |
