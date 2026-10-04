# Documentation

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are looking for a page and do not yet know which one.

Every page here opens with the same one-line block: who it is for, which install profile
it applies to, and the situation that should send you to it. This index is the same
information collected in one place.

The **profile** matters more than it looks. `install_profile` is `demo` or `pov` and the
two are mutually exclusive, so a page marked `pov` describes features an estate instance
does not have, and the reverse. `demo` is a config value naming one BeyondTrust tenant of
your own, not a claim about what you are doing with the instance — a page marked `demo`
applies just as much to infrastructure you run in production. See
[Demo and POV profiles](profiles/README.md).

## Start here

| You are… | Go to |
|---|---|
| **installing or running** the dashboard — an **operator** | [Onboarding](ONBOARDING.md), then the platform reference below |
| **running your own infrastructure** with it | the platform reference below — you want the `demo` profile, whatever its name suggests |
| **showing** it to someone — a **presenter** | [Demo profile](profiles/demo/README.md) |
| running **customer proof-of-value** work | [POV profile](profiles/pov/README.md) |
| **evaluating** a POV somebody handed you — a **customer** | [What the customer sees](profiles/pov/customer-access.md) |
| **changing the code** — a **contributor** | [Design notes](design/README.md), [runbooks](runbooks/README.md), [notes](notes/README.md), and [CONTRIBUTING.md](../CONTRIBUTING.md) |

## Platform reference

What the dashboard does, and the discipline it expects from you. These are written once
and shared: the page describing how to deploy a cloud VM is the same page whether you are
demoing, running a lab, or running production.

Each group below is a **section**: a hub page that says which page answers what, and a
folder of the pages themselves. A page that stands alone is a row on its own.

### Get started

| Page | Read this when |
|---|---|
| [Onboarding Guide](ONBOARDING.md) | you are setting the dashboard up for the first time and want the shortest path to a running instance. The per-cloud setup pages are under [`onboarding/`](ONBOARDING.md). |

### Cloud

| Page | Read this when |
|---|---|
| [Cloud](cloud.md) | you are putting workloads on AWS, Azure, GCP or OCI and want to know which page covers which part. |
| [Infrastructure as Code](cloud/infrastructure-as-code.md) | you are about to deploy your first cloud resource and want to know what is actually running underneath. |
| [Cloud Sandbox](cloud/sandbox.md) | you want an isolated cloud account for the dashboard's labs, bootstrapped rather than hand-built. |
| [Cloud VMs](cloud/vms.md) | you are deploying cloud VMs, or want the dashboard to see and power ones it did not deploy. |
| [Cloud Containers](cloud/containers.md) | you want a containerised app on a cloud runtime without standing up Portainer. |
| [Virtual Desktops](cloud/virtual-desktops.md) | you need a pool of private desktop VMs that reps reach through the PRA Gateway rather than over the internet. |
| [Image Management](cloud/image-management.md) | you are about to build a custom image and need to know how it will reach the other clouds. |
| [Cloud Costs](cloud/costs.md) | you want month-to-date cloud spend on the dashboard, a budget alert, or a budget in the cloud that alerts while the dashboard is down. |

### Platforms and workloads

| Page | Read this when |
|---|---|
| [Databases](databases.md) | you are standing up a managed database, or want to manage one you already run. |
| [Kubernetes](kubernetes.md) | you are managing Kubernetes clusters and the privileged access into them. |
| [KubeSolo](kubernetes/kubesolo.md) | you need the Entitle agent on an edge or plant-floor host that will not carry a real cluster — or you want to see the single-node cluster the OT demo cell runs on. |
| [Config Management](config-management.md) | you are about to run an Ansible job and want to know how the runner handles secrets and isolation. |
| [Inventory](inventory.md) | you want one list of everything across every cloud and hypervisor, to filter it by tag or Password Safe attribute, or to act on a selection. |
| [Storage Management](storage-management.md) | you are enabling a feature that needs a storage backend, which several of them do. |
| [Remote Agents](remote-agents.md) | your hypervisors, databases or clusters live somewhere the dashboard cannot reach. The pages are under [`remote-agents/`](remote-agents.md). |
| [Scheduling](scheduling.md) | you want a change to run later, run repeatedly, or run only inside an approved window, or it has to wait for a second person to sign it off. The hub tells the three kinds of "schedule" apart; the full reference, [Change Windows](scheduling/change-windows.md), and its other half, [Action Guardrails](scheduling/policy-guardrails.md) — disallowed changes blocked before they start — are under [`scheduling/`](scheduling.md). |

### Identity and access

| Page | Read this when |
|---|---|
| [Identity and access](access.md) | you are deciding who and what may use the dashboard, how it keeps credentials, or how you would prove what happened. |
| [Permissions](access/permissions.md) | you are deciding what a user may see or do — and especially before ticking "Full access", or handing a POV to a customer stakeholder. |
| [Service Accounts](access/service-accounts.md) | something that is not a person — a CI job, an MCP agent, a script — needs to call the API, and you would otherwise hand it a PAT. OAuth 2.0 client credentials, built in, no IdP needed. |
| [Secrets Management](access/secrets-management.md) | you are deciding where to store cloud credentials, and how to evolve that over time. |
| [Audit Log](access/audit-log.md) | you need to show who did what, or to satisfy yourself that the record has not been edited. |
| [OIDC and single sign-on](oidc.md) | you want single sign-on, Dex in front of your clusters, Entra federation to Kubernetes, or the dashboard reaching AWS, Azure, GCP and k3s with its own short-lived identity instead of a stored key. The pages are under [`oidc/`](oidc.md). |
| [Workload Lab](workload-lab.md) | something that is not a person needs a credential — a certificate, a SPIFFE identity, a cluster token or a cloud key — and you want to pick the mechanism before reading any one guide. The per-tab guides, the Workload Credentials product pages and the register of what consumes each credential are all under [`workload-lab/`](workload-lab.md). |

### Operations

| Page | Read this when |
|---|---|
| [Operations](operations.md) | the dashboard is installed and you are keeping it running. |
| [Cloud Hosting](operations/cloud-hosting.md) | you want the dashboard reachable from outside your LAN, or fronting remote agents. |
| [Job Worker](operations/job-worker.md) | a long job is sitting queued, or you are sizing the worker for more of them. |
| [Notifications](operations/notifications.md) | you want to hear about expiring resources and failed jobs without opening the dashboard. |
| [Auto-delete Timer](operations/auto-delete-timer.md) | you want lab resources to clean themselves up — read it before enabling it, because it deletes infrastructure. |
| [Config Migration](operations/config-migration.md) | you are standing up a second instance and do not want to re-type months of configuration. |

### Editions

| Page | Read this when |
|---|---|
| [Editions](editions.md) | you are choosing between running the dashboard yourself and a hosted edition, or want to know why a feature is not in this one. |
| [Community vs. hosted](editions/comparison.md) | you are choosing between running this yourself and a managed edition. |
| [SaaS Roadmap](editions/roadmap.md) | you want to know which capabilities are reserved for the hosted edition, and why. |

## The rest of the tree

Two kinds of folder, indexed two ways. A **section** (a feature with several pages) is
indexed by a hub page of the same name beside it, so `cloud.md` indexes `cloud/`, and the
hub is also the section's own entry page. A **collection** (pages that share a kind, not a
feature) is indexed by its own `README.md`.

| Folder | What's in it |
|---|---|
| [profiles/](profiles/README.md) | The `demo` / `pov` gate, the per-feature matrix, and everything specific to one profile or the other. |
| [integrations/](integrations/README.md) | One page per external system the dashboard talks to — the BeyondTrust products, the hypervisors, the runners. |
| [design/](design/README.md) | Why a subsystem is shaped the way it is. Facts that are not recoverable from reading the code. |
| [runbooks/](runbooks/README.md) | Procedures to run against a real instance, usually to prove a phase of work landed. |
| [notes/](notes/README.md) | Dated investigations, kept for their conclusions rather than their narrative. |

The sections — [`onboarding/`](ONBOARDING.md), [`cloud/`](cloud.md), [`access/`](access.md),
[`oidc/`](oidc.md), [`workload-lab/`](workload-lab.md), [`remote-agents/`](remote-agents.md),
[`scheduling/`](scheduling.md), [`operations/`](operations.md), [`kubernetes/`](kubernetes.md)
and [`editions/`](editions.md) — are listed with their hubs above.

Outside `docs/`: [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md), and
a `README.md` beside most directories that ships something —
[`runners/`](../runners/agent/README.md), [`examples/`](../examples/playbooks/README.md),
[`scripts/sandbox/`](../scripts/sandbox/README.md).

## Reading these in the app

The dashboard serves this tree at `/docs`, rendered, with no internet access required —
so an operator does not need the repo open to follow a setup instruction a Settings panel
gave them. That shell is public and unauthenticated, and it lists both profiles' sections
to everybody: making the index vary with the instance's own configuration would leak that
configuration to anyone who asked.
