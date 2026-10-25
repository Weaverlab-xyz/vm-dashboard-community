# Operations

> **Audience:** operator · **Profile:** `both` · **Read this when:** the dashboard is installed and you are running it day to day: managing the infrastructure that already exists and what you built with it, and keeping track of jobs, costs and clean-up.

Day-to-day operation, after [getting started](getting-started.md): the pages behind the app's
**Operations** menu. Two halves.

**Managing what exists**: the infrastructure you already run and everything you built
with the dashboard. Configure it, change it on a schedule or inside an approved window,
and keep the playbooks, scripts and images it depends on.

| Page | Read it when |
|---|---|
| [Config Management](operations/config-management.md) | you are about to run an Ansible job against existing hosts and want to know how the runner handles secrets and isolation |
| [Scheduling](operations/scheduling.md) | you want a change to run later, run repeatedly, or run only inside an approved window, or it has to wait for a second person to sign it off. The full reference, [Change Windows](operations/scheduling/change-windows.md), and [Action Guardrails](operations/scheduling/policy-guardrails.md) are under [`scheduling/`](operations/scheduling.md) |
| [Image Management](operations/image-management.md) | you are about to build a custom image and need to know how it will reach the other clouds |
| [Storage Management](operations/storage-management.md) | you are enabling a feature that needs a storage backend for playbooks, scripts or images, which several of them do |

**Keeping track**: one list of everything, how its jobs run, what it costs, and what it
cleans up.

| Page | Read it when |
|---|---|
| [Inventory](operations/inventory.md) | you want one list of everything across every cloud and hypervisor, to filter it by tag or Password Safe attribute, or to act on a selection |
| [Job Worker](operations/job-worker.md) | a long job is sitting queued, or you are sizing the worker for more of them |
| [Cloud Costs](operations/costs.md) | you want month-to-date cloud spend, a budget alert, or a budget in the cloud that alerts while the dashboard is down |
| [Auto-delete Timer](operations/auto-delete-timer.md) | you want lab resources to clean themselves up. Read it before enabling it, because it deletes infrastructure |

Related: [Admin](admin.md) for health, notifications and remote agents, and
[Platform](platform.md) for hosting the dashboard itself and moving its configuration.
