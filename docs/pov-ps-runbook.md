# The Password Safe Cloud POC runbook, on a POV

BeyondTrust's canonical procedure for a customer-facing Password Safe Cloud POC is the SE
Lab step-by-step: **Confluence SELab page 870514897, "Using Skytap for a Password Safe
Cloud POC: Step-by-Step"**, rev 7.0 (2026-09-02), validated by its author against PWS SaaS
26.2.0.1427. It is 75 pages, and it runs as roughly two 90-minute working sessions.

This page is about how that document and this dashboard fit together. The POV detail
page's **Use cases** tab carries the runbook as a group of tickable cards, one per use
case, in the document's own numbering — so a POC that runs across several sessions and
more than one person knows where it got to.

## What the cards are, and are not

The cards are the runbook's **twenty-one demonstrations** (its use cases 1–20, with 11 split
into 11A and 11B). They are not its setup steps. Steps 1–11 — the tenant, the Skytap
environment, the resource zone and broker, policies, functional accounts, discovery
credentials, the scan, the directory credentials and the user groups — are a precondition
for all twenty-one, they happen once, and an SE who has not finished them has nothing to
tick.

Progress is per POV and records who ticked each card and in what capacity, so a prospect
ticking one through the accessor page is distinguishable from the SE doing it. `skipped` is
a real answer, deliberately different from never having got to it.

## Seven cards say the runbook is incomplete

The document has holes, and the cards name them rather than hiding them:

| Use case | State in the runbook |
| --- | --- |
| 12 — SSMS application session | Work in progress; needs SQL Server and SSMS on App01, which its own step 3 leaves unwritten |
| 13 — Web application (AWS/Gmail) | Not QA'd since Q4 2022; needs shared AWS and Gmail accounts that do not exist |
| 14 — Bring your own tool | Never written up. PuTTY ships on the workstation, so it is demonstrable, just not scripted |
| 15 — DR via Password Safe Cache | Personal notes only, and it needs a `BtPocCache01` guest that is not in template Part 1 |
| 16 — API access via the Resource Kit | Unwritten; the document notes it overlaps use case 15's API setup |
| 17 — Workforce Passwords | Outline only, though short |
| 19 — SSH key management | Points at another repository instead of documenting it; needs a key-based Linux account seeded in the template |

A card that reads "the runbook's own steps for this are incomplete" stops an SE promising a
demo they cannot give. A *missing* card just reads as a gap in this dashboard, which is
why all twenty-one are present.

Use case **20 — Password Safe / PRA integration** is new in rev 7.0 and marked
work-in-progress by its author. It is also **not** what this dashboard's wire-up does: it
discovers Password Safe accounts inside PRA so a user works from the PRA console, and it
needs Builder to enable tunnel and web jump, a Gateway on the *Windows* broker, and Jump
Clients on the guests. The dashboard's slice 6a creates Shell Jump and RDP jump items
pinned to the POV's own Gateway on the *Linux* broker — a real and useful thing the runbook
does not do, and not a substitute.

## What the dashboard automates, and what stays manual

Automated, from the POV page:

- the Skytap environment (deploy, name, power, the customer share link with a generated
  password and a hard expiry), which is the runbook's step 2;
- the in-environment broker agent;
- the **Password Safe Resource Broker** install on the named Windows guest — the runbook's
  step 5, minus creating the resource zone and fetching the install key, which stay in the
  Password Safe console;
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

## One thing worth knowing about reachability

Password Safe reaches these private guests through the Resource Broker, and what arranges
that is the broker's **resource zone** and the workgroup mapped to it — the runbook's step
5 creates the zone, adds the workgroup to it, and installs the broker into that zone. It
never mentions an "application host" anywhere.

`ps_application_host_id` on a POV is therefore an optional override, not the routing
mechanism, and the wire-up sends `0` when it is unset. If a rotation fails to reach a guest
days after onboarding, check the zone-to-workgroup mapping first. See
`docs/design/pov-resource-broker.md` §6.

## A caution about the shared template

The runbook is written against `Btpoc.upm.academy POC Template`, whose AD forest is
`btpoc.upm.academy`. It **must not** be pointed at a Password Safe instance that has
already seen it — the document warns that doing so conflicts with the previously-tested
domain and generates mass email to every SE. Use a freshly provisioned customer tenant, or
build a custom environment per the runbook's Appendix 1.
