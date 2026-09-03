# The Password Safe Cloud POC runbook, on a POV

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are running the Password Safe Cloud POC against a POV instance.

BeyondTrust's canonical procedure for a customer-facing Password Safe Cloud POC is the SE
Lab step-by-step: **Confluence SELab page 870514897, "Using Skytap for a Password Safe
Cloud POC: Step-by-Step"**, rev 7.0 (2026-09-02), validated by its author against PWS SaaS
26.2.0.1427. It is 75 pages, and it runs as roughly two 90-minute working sessions.

This page is about how that document and this dashboard fit together. The POV detail
page's **Use cases** tab carries the runbook as a group of tickable cards, in the
document's own numbering — so a POC that runs across several sessions and more than one
person knows where it got to.

## What the cards are, and are not

There are **fourteen cards**: use cases 1–10, 11A, 11B, 18 and 20. Two kinds of thing in
the runbook deliberately have no card.

**Its setup steps.** Steps 1–11 — the tenant, the Skytap environment, the resource zone and
broker, policies, functional accounts, discovery credentials, the scan, the directory
credentials and the user groups — are a precondition for every use case, they happen once,
and an SE who has not finished them has nothing to tick.

**Its seven unfinished use cases**, listed below. A card is a demo an SE can actually give;
one pointing at a procedure the document never wrote is a checkbox nobody can honestly
tick, and it would sit unticked on every POV reading either as a gap in this dashboard or,
worse, as a demo somebody promises and then cannot show.

Progress is per POV and records who ticked each card and in what capacity, so a prospect
ticking one through the accessor page is distinguishable from the SE doing it. `skipped` is
a real answer, deliberately different from never having got to it.

## The seven use cases with no card

The document has holes. They are recorded here rather than as cards, so an SE who goes
looking for use case 15 finds out why it is absent instead of wondering. **When the runbook
fills one in, it gets a card.**

| Use case | State in the runbook |
| --- | --- |
| 12 — SSMS application session | Work in progress; needs SQL Server and SSMS on App01, which its own step 3 leaves unwritten |
| 13 — Web application (AWS/Gmail) | Not QA'd since Q4 2022; needs shared AWS and Gmail accounts that do not exist |
| 14 — Bring your own tool | Never written up. PuTTY ships on the workstation, so it is demonstrable, just not scripted |
| 15 — DR via Password Safe Cache | Personal notes only. The `BtPocCache01` guest it needs is not in template Part 1, though the VMs tab can now copy one in from Appendix 1's template |
| 16 — API access via the Resource Kit | Unwritten; the document notes it overlaps use case 15's API setup |
| 17 — Workforce Passwords | Outline only, though short |
| 19 — SSH key management | Points at another repository instead of documenting it; needs a key-based Linux account seeded in the template |

Several are close. 16 overlaps 15's API-registration setup, and 17 is short. If you work one
of them out on a real POC, that is worth taking back to the document — and the card follows.

## Use case 20 does have a card, but it is not what the wire-up does

Password Safe / PRA integration is new in rev 7.0 and marked work-in-progress by its author,
so it sits on the line drawn above. It keeps its card because, unlike the seven, the document
does lay out the whole sequence — its own words are "documents very closely what needs to
happen".

Worth being clear about what it is: it discovers Password Safe accounts *inside PRA* so a
user works from the PRA console and off VPN, and it needs Builder to enable tunnel and web
jump, a Gateway on the **Windows** broker, and Jump Clients on the guests. The dashboard's
slice 6a creates Shell Jump and RDP jump items pinned to the POV's own Gateway on the
**Linux** broker. That is a real and useful thing the runbook does not do, and it is not a
substitute for use case 20.

## What the dashboard automates, and what stays manual

Automated, from the POV page:

- the Skytap environment (deploy, name, power, the customer share link with a generated
  password and a hard expiry), which is the runbook's step 2;
- the in-environment broker agent;
- the **Password Safe Resource Broker** install on the named Windows guest — the runbook's
  step 5, minus creating the resource zone and fetching the install key, which stay in the
  Password Safe console;
- **running a staged file on any guest you allow** — the runbook's step 3 (the RDS role and
  the SQL Server / SSMS install on `BtPocApp01`) and use case 18's `SetupMoreUsers.ps1` on
  the domain controller. See below;
- **adding VMs from a template to a live POV** — use case 18's `BtPocApp02` and
  `BtPocLin02` from template Part 2, and use case 15's `BtPocCache01`. On the VMs tab;
- per-VM PRA jump items and Password Safe managed systems and accounts;
- the auto-delete timer, sleep schedule and spend cap, which the runbook has no equivalent
  for.

Manual, and correctly so:

- **step 1**, provisioning the customer's Password Safe Cloud tenant — a Salesforce
  Evaluation Request and cs.cloudops, across two systems this dashboard holds no credential
  for;
- **step 4**, the customer's first login and admin password reset — the whole point is that
  the customer holds the tenant;
- **steps 5b–11 and use cases 1–10**, which are Site Options, password and access
  policies, discovery credentials, discovery scans, directory queries, user groups and
  Smart Rules. None of that exists in `ps_api_service` today, and Smart Rules are the
  engine of most of the use cases.

## Running a step on a guest

The POV detail page's **VMs** tab has a Configure column and a "Run a step on a guest"
panel. Tick a guest, choose a file you have uploaded on the Storage page, press Run. A
`.ps1` runs on a Windows guest, a `.sh` on a Linux one, a `.yml` playbook on either, and an
`.exe`/`.msi` installs — the only one of the four that takes arguments, because it is the
only play shape with a variable for them.

There is no login field. The credential comes from the lab platform's own stored
credentials, read per run, which is also why nothing goes stale when a template password
changes.

**Two things will trip you up.**

The guest must be **ticked first**. The broker agent is granted a closed list of `/32`
addresses, so a run at a guest outside that list is refused up front — it would otherwise
reach the agent and fail as a connection timeout, which reads as a firewall problem.

The grant is written when the agent is **enrolled**, so a guest ticked since then is not
granted yet. The page says so in amber, and the run is refused with the remedy: press
**Broker** on the POV list to rewrite the policy. This is the one step in the sequence that
is not obvious, and it is why the refusal exists instead of a timeout.

What this deliberately is *not*: a way to run arbitrary commands. The file is one you
staged, the guest is one you ticked, and both are recorded on the job.

## Checking what the admin actually finished

Setup steps 5b–11 are clicked into the Password Safe console by the customer's own
administrator, and the twenty use cases assume it was done right. The question that costs
an SE time is not "how do I do step 9" — it is **"did they finish step 9?"**, which today
is a screen-share and a lot of "can you click Configuration for me".

The POV detail page's **Wired** tab has a **Password Safe readiness** panel. Press Check
and it reads the tenant back, one signed-in pass, and reports each collection against the
runbook step that creates it:

| Reported | Meaning |
| --- | --- |
| `N found` | the step is done |
| `N of M` | the step is half done — one access policy where the runbook wants two |
| `none` | the step has not been started |
| `no API` | this Password Safe version does not serve that endpoint |
| `unreadable` | the read failed, usually the API identity's permissions |

Readiness is a **count**, never a name match. The runbook's names are per-customer —
`btpoc.upm.academy`, `PSDirBrowser`, `ServiceDesk_Users` — and a POV that renamed them has
still done the step.

Four steps have **no endpoint to read at all**, and the panel lists them anyway rather than
leaving them out: the Resource Broker and its zone (step 5), discovery credentials (step 9),
the discovery scan (step 10) and directory queries (step 11). A step absent from a checklist
reads as a step nobody thought about.

## Re-running a Smart Rule

Use case 1 is watching a Smart Rule onboard the discovered systems on its own. Use case 18
is that again, after two guests and a batch of AD users arrive. Neither needs a rule
*created* — both need one **re-run**, in front of a customer, repeatedly.

The same tab has a rule picker and a Process button. It is queued tenant-side, so the
button returns before Password Safe finishes; Re-check, or the rule's own last-processed
date, is what says it landed.

Nothing on either panel writes to the customer's tenant. The readiness read is a read, and
Process re-runs a rule their admin made — which is what makes both safe on a POV somebody
else configured.

## Why the dashboard does not just configure the tenant

Because the API does not let it. This is worth stating plainly so nobody plans around it:

| Runbook item | Public API |
| --- | --- |
| Step 5 — resource zones, resource brokers | no documented endpoint at all |
| Step 6 — password policies | read-only |
| Step 6 — access policies | read, plus `POST AccessPolicies/Test` |
| Step 9 — discovery credentials | no documented endpoint |
| Step 10 — discovery scans | no documented endpoint; run from the console |
| Step 11 — directory queries | no documented endpoint |
| Use cases 1–10 — Smart Rules | create is `POST SmartRules/FilterAssetAttribute` alone, which is not the runbook's account rules with actions |
| Use cases 12–13 — applications | read-only |

What *is* creatable — user groups with their permissions and Smart Group access (step 11),
a directory managed system (step 8), an API registration (use cases 15 and 16), workgroups
and assets — is not built yet, and would be writing to a customer's tenant on objects this
dashboard did not previously own. See `docs/integrations/password-safe.md`.

## One thing worth knowing about reachability

Password Safe reaches these private guests through the Resource Broker, and what arranges
that is the broker's **resource zone** and the workgroup mapped to it — the runbook's step
5 creates the zone, adds the workgroup to it, and installs the broker into that zone. It
never mentions an "application host" anywhere.

`ps_application_host_id` on a POV is therefore an optional override, not the routing
mechanism, and the wire-up sends `0` when it is unset. If a rotation fails to reach a guest
days after onboarding, check the zone-to-workgroup mapping first. See
[`docs/profiles/pov/design/resource-broker.md`](design/resource-broker.md) §6.

## A caution about the shared template

The runbook is written against `Btpoc.upm.academy POC Template`, whose AD forest is
`btpoc.upm.academy`. It **must not** be pointed at a Password Safe instance that has
already seen it — the document warns that doing so conflicts with the previously-tested
domain and generates mass email to every SE. Use a freshly provisioned customer tenant, or
build a custom environment per the runbook's Appendix 1.
