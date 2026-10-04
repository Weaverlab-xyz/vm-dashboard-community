# Cloud Costs

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want month-to-date cloud spend on the dashboard, an alert when it runs over budget, or a budget in the cloud itself that keeps alerting when the dashboard is down.

**Costs** (`/costs`, admins only) shows month-to-date spend for every configured cloud, and
the dashboard home page shows the same total as a tile. It is off until you turn on
`cost_explorer_enabled` under **Settings → Cloud Costs**.

## What it shows

- **Account spend (MTD).** The whole account's or subscription's spend per cloud, and the
  total. It is **net**: what the account pays. Where a cloud can separate them, the gross
  figure and the credits are shown under it, so a credit running out does not read as
  infrastructure that grew.
- **Monthly budget.** Spend against the budget, and whether it is projected to exceed it
  by month-end. A per-cloud budget shows on that cloud's row as well.
- **Attributed spend (MTD).** Per cloud and per service, split by the `managed-by` tag:
  - **dashboard**: resources tagged `managed-by=vm-dashboard`, which everything the
    dashboard deploys carries;
  - **sandbox**: `managed-by=dashboard-sandbox`, the sandbox bootstrapper's baseline;
  - **unattributed**: spend no tag reaches. It is read-only by design: there is no reclaim
    control, because the reapers never touch what the dashboard did not create.

## Where each figure comes from

| Cloud | Source | What it needs |
|---|---|---|
| AWS | Cost Explorer (`GetCostAndUsage`) | `ce:GetCostAndUsage`. For the attributed split, activate `managed-by` as a **cost-allocation tag** in the Billing console: forward-only, and about 24 hours to populate. One activation covers both values |
| Azure | Cost Management query | Cost Management Reader on the subscription. Tags do not inherit to disks, NICs or ACI unless you turn on Cost Management **tag inheritance** |
| GCP | the **BigQuery billing export** | GCP has no cost API. Enable Cloud Billing export to BigQuery, set `gcp_billing_export_table` (`project.dataset.table`), and grant the dashboard's service account BigQuery Data Viewer and Job User on that dataset. The export lags about 24 hours. Cloud Router and NAT take no labels, so their spend is unattributed |
| OCI | the Usage API | Scoped by **compartment**, because the Usage API ignores freeform tags. So OCI's sandbox figure includes dashboard-provisioned resources in the same compartment |

A cloud that fails, is throttled or is not configured reports itself as unavailable or
stale; it never takes the others down with it.

## The API calls cost money

**Cost Explorer bills about $0.01 per request**, and a dashboard that polls it can become
the largest line on the bill it is reporting. So figures are cached, and the page and tile
only ever read the cache:
- a background warmer keeps it populated;
- **Refresh** forces a live query, at most once per cloud per
  `cost_refresh_min_interval_seconds`, and never for a cloud in throttle cooldown;
- a cloud that fails keeps its last known figure, marked stale with its age, rather than
  blanking.

The reasoning, with the numbers, is in
[cloud cost guardrails](notes/cloud-cost-guardrails.md).

## Budgets

| Setting | Key | Default | What it does |
|---|---|---|---|
| Monthly budget | `cost_monthly_budget` | `0` (off) | Overall, in the account currency. The home page's **Needs attention** panel flags *over* (spend ≥ budget) and *approaching* (projected to exceed it by month-end) |
| Per-cloud budgets | `cost_budget_aws`, `cost_budget_azure`, `cost_budget_gcp`, `cost_budget_oci` | `0` (off) | Each cloud's spend against its own limit, as well as the overall one |

These are checked by the dashboard, against figures it fetched, so **nothing watches the
spend while the dashboard is down**. [Notifications](notifications.md) can send the budget
alert; the condition scan reads the cached figures.

### A budget in the cloud itself

For AWS and Azure the dashboard can create the same budget in the cloud account, where it
alerts whether the dashboard is running or not. This is an action, not a side effect of
saving a number, and it has no button yet: it is two API calls, needing the `costs`
permission.

| Call | Permission | What it does |
|---|---|---|
| `GET /api/budgets/{aws\|azure}` | `costs:read` | What is in the account now beside what a push would set, field by field. Answers 200 with the reason if a setting is missing |
| `POST /api/budgets/{aws\|azure}` | `costs:write` | Creates or updates the dashboard's budget |

It needs two settings, set through the headless import (`POST /api/setup/import`); neither is
on the Settings page:

| Key | Default | |
|---|---|---|
| `cost_budget_notify_emails` | blank | Comma-separated addresses the **provider** notifies. Required: the point is an alert that does not pass through the dashboard |
| `cost_budget_alert_percent` | `80` | Notify at this percentage of the limit, clamped to 1–100 |

The limit is the cloud's own budget, or the overall one when that is blank. Safeguards:
- **Only its own budgets.** Budgets the dashboard creates are named `vm-dashboard-…`, and it
  never writes to one without that prefix. AWS budgets carry no tags, so the name is the only
  marker. A budget a person names `vm-dashboard-…` will be adopted.
- **Nothing is deleted.** Setting the limit to `0` stops the dashboard managing the number;
  it does not remove a budget someone may rely on.
- **AWS budgets are in USD.** Cost Explorer reports in the account currency, and the
  dashboard converts nothing.
- **Not GCP.** A GCP budget belongs to a billing account, an id the dashboard does not hold.

## Settings

All on **Settings → Cloud Costs**, except where the tables above say otherwise.

| Key | Default | What it does |
|---|---|---|
| `cost_explorer_enabled` | off | The page, the API and the home-page tile |
| `gcp_billing_export_table` | blank | GCP's BigQuery export table. Blank leaves GCP unavailable |

Cache tuning lives in the environment or `config.py` only, deliberately. These are
throttle-safety knobs, not features:

| Key | Default | |
|---|---|---|
| `cost_cache_ttl_seconds` | `86400` | How old a good figure may get |
| `cost_refresh_min_interval_seconds` | `300` | The floor between two forced refreshes of one cloud and view |
| `cost_query_lease_seconds` | `120` | Single-flight claim expiry |
| `cost_query_gap_seconds` | `2` | Minimum spacing between queries to one cloud |
| `cost_cold_wait_seconds` | `5` | How long a cold miss waits for the query already in flight |

## Related

- [Cloud VMs → Spend caps](cloud-vms.md#spend-caps), which stop a VM rather than report it.
- [Tags and labels](cloud-vms.md#tags-and-labels): `managed-by` is how spend is attributed,
  which is why it cannot be edited.
- [Notifications](notifications.md), for budget alerts.
