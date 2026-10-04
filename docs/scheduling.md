# Scheduling

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want a change to run later, run repeatedly, or run only inside an approved window — or blocked before it starts — and you are not sure which of the dashboard's features that is.

"Schedule" means three different things in this dashboard. They are separate features
with separate controls, and picking the wrong one is the usual reason a schedule "did not
work". This page tells them apart and sends you to the page with the detail. The detail
pages live in the `scheduling/` folder beside this one, the way `workload-lab/` sits beside
its hub.

Two pages decide whether a change may start at all, and they are two halves of one control:
[Change Windows](scheduling/change-windows.md) decide **when**, and
[Action Guardrails](scheduling/policy-guardrails.md) decide **whether**, by evaluating the
request against OPA policy before a job exists. They share one list of gated actions, and
a guardrail's `needs_approval` verdict can hold a change for the same approver a window
uses.

| You want to… | Use | Where | Detail |
|---|---|---|---|
| run **one** change later — at a time, or in the next approved window | a **booking** | **When to run** on the form, or **Schedule** on a selection toolbar | [Change Windows — Scheduling one job](scheduling/change-windows.md#scheduling-one-job) |
| run the **same** change on every occurrence of a window | a **recurring schedule** (Repeat) | **Repeat in a change window** on a finished job's page; listed at **Schedules** (`/schedules`) | [Change Windows — Repeating a change](scheduling/change-windows.md#repeating-a-change) |
| block a disallowed change before it starts — wrong region, oversized, a change freeze | an **Action Guardrail** | Settings → Action Guardrails | [Action Guardrails](scheduling/policy-guardrails.md) |
| stop cloud VMs out of business hours to save money | a **suspend schedule** | Settings, per cloud | [Cloud VMs — Suspend schedules](cloud/vms.md#suspend-schedules-all-four-clouds) |

The first two share one engine — the job queue holds a booked job `pending` until its
time — and both depend on **change windows**, the named recurring periods defined in
Settings. The third is its own sweeper with per-VM eligibility rules, and has nothing to do
with windows.

---

## The two pages

**Waiting changes** — `/jobs?scheduled=1`, reached from the **Waiting changes** button on
the Schedules page or the **Scheduled only** tick on the Jobs page. Every one-off booking that has
not started yet. Cancel one there like any other pending job.

**Scheduled Changes** — `/schedules`, **Schedules** in the nav. Every recurring schedule:
its window, next and last run, and whether it is active. Each row can be **disabled**,
**enabled** or **deleted**. A schedule that switched itself off (three failures in a row,
its window deleted, or its owner's account gone) says why in red on its row.

There is no **New schedule** button, on purpose: a schedule is always made from a job that
has already run, so what repeats is the run you already tested.

Both pages render for any signed-in user; what each shows is scoped by the API to the jobs
and schedules you are allowed to see.

---

## What can be scheduled

| Surface | Book once | Repeat |
|---|---|---|
| Config Management runs (single and bulk) | yes — form | yes |
| Cloud VM deploy, single and bulk — AWS, Azure, GCP, OCI | yes — form | no: the saved job carries live teardown handles |
| Cloud VM destroy | yes — offered when a window refuses one, or via the API | no: the target is gone after the first run |
| Power — AWS, Azure, GCP, OCI | yes — selection toolbar | yes |
| Power — Proxmox, vSphere, Hyper-V, XCP-ng, VMware Workstation | **agent-bound connections only** — selection toolbar | no: not on the repeat allowlist yet — book each occurrence |
| Power — Nutanix | no, never | no |
| Image export, capture, AMI copy | yes — dialog | exports only |
| Image promotion | yes — Promote dialog (automated path) | yes |
| Packer builds | yes — Build tab | no: a provisioner variable may hold a literal value |
| Kubernetes cluster / cloud database provisioning | no | no |
| EPM for Linux package sync | — | yes |

Why the exceptions exist, and what happens when a window is missed, needs approval, or
is required for a whole workgroup, is all in [Change Windows](scheduling/change-windows.md). Why an
on-premises power booking needs an agent is under
[Scheduling power operations](scheduling/change-windows.md#scheduling-power-operations).

---

## See also

* [Change Windows](scheduling/change-windows.md) — the full reference for bookings, repeats, approval
  and the per-workgroup requirement.
* [Action Guardrails](scheduling/policy-guardrails.md) — the policy half: what is refused,
  or held for approval, before it is ever queued.
* [Cloud VMs](cloud/vms.md) — suspend schedules and spend caps.
* [Job Worker](operations/job-worker.md) — how a booked job gets a turn once its time arrives.
* [Remote Agents — hypervisors](remote-agents/hypervisors.md) — binding a hypervisor
  connection to an agent, which is what makes its power operations bookable.
