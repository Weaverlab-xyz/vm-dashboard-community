# Operations

> **Audience:** operator · **Profile:** `both` · **Read this when:** the dashboard is installed and you are keeping it running: where it is hosted, how its jobs run, what it tells you, and what it cleans up.

Running the dashboard itself, after [onboarding](ONBOARDING.md).

| Page | Read it when |
|---|---|
| [Cloud Hosting](operations/cloud-hosting.md) | you want the dashboard reachable from outside your LAN, or fronting remote agents |
| [Job Worker](operations/job-worker.md) | a long job is sitting queued, or you are sizing the worker for more of them |
| [Notifications](operations/notifications.md) | you want to hear about expiring resources and failed jobs without opening the dashboard |
| [Auto-delete Timer](operations/auto-delete-timer.md) | you want lab resources to clean themselves up. Read it before enabling it, because it deletes infrastructure |
| [Config Migration](operations/config-migration.md) | you are standing up a second instance and do not want to re-type months of configuration |

Related: [Scheduling](scheduling.md), for changes that run later or only inside an approved
window, and [Storage Management](storage-management.md), for the backends several features
need.
