# Operations

> **Audience:** operator · **Profile:** `both` · **Read this when:** the dashboard is installed and you are running it day to day: managing the infrastructure that already exists and what you built with it, and keeping the dashboard itself healthy.

Day-to-day operation, after [onboarding](ONBOARDING.md). Two halves.

**Managing what exists**: the infrastructure you already run and everything you built
with the dashboard. Configure it, change it on a schedule or inside an approved window,
and keep the playbooks, scripts and images it depends on.

| Page | Read it when |
|---|---|
| [Config Management](operations/config-management.md) | you are about to run an Ansible job against existing hosts and want to know how the runner handles secrets and isolation |
| [Scheduling](operations/scheduling.md) | you want a change to run later, run repeatedly, or run only inside an approved window, or it has to wait for a second person to sign it off. The full reference, [Change Windows](operations/scheduling/change-windows.md), and [Action Guardrails](operations/scheduling/policy-guardrails.md) are under [`scheduling/`](operations/scheduling.md) |
| [Storage Management](operations/storage-management.md) | you are enabling a feature that needs a storage backend for playbooks, scripts or images, which several of them do |

**Running the dashboard itself**: where it is hosted, how its jobs run, what it tells you,
and what it cleans up.

| Page | Read it when |
|---|---|
| [Cloud Hosting](operations/cloud-hosting.md) | you want the dashboard reachable from outside your LAN, or fronting remote agents |
| [Container User](operations/container-user.md) | you are upgrading from a release that ran as root, a job hits "permission denied", or you are setting a security context on the container |
| [Job Worker](operations/job-worker.md) | a long job is sitting queued, or you are sizing the worker for more of them |
| [Notifications](operations/notifications.md) | you want to hear about expiring resources and failed jobs without opening the dashboard |
| [Auto-delete Timer](operations/auto-delete-timer.md) | you want lab resources to clean themselves up. Read it before enabling it, because it deletes infrastructure |
| [Config Migration](operations/config-migration.md) | you are standing up a second instance and do not want to re-type months of configuration |

Related: [Inventory](inventory.md), the one list of everything across every cloud and
hypervisor, to act on a selection.
