# Documentation

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are looking for a page and do not yet know which one.

Every page here opens with the same one-line block: who it is for, which install profile
it applies to, and the situation that should send you to it. This index is the same
information collected in one place.

The **profile** matters more than it looks. `install_profile` is `demo` or `pov` and the
two are mutually exclusive, so a page marked `pov` describes features a demo instance does
not have, and the reverse. See [Demo and POV profiles](profiles/README.md).

## Start here

| You are… | Go to |
|---|---|
| **installing or running** the dashboard — an **operator** | [Onboarding](ONBOARDING.md), then the platform reference below |
| **showing** it to someone — a **presenter** | [Demo profile](profiles/demo/README.md) |
| running **customer proof-of-value** work | [POV profile](profiles/pov/README.md) |
| **evaluating** a POV somebody handed you — a **customer** | [What the customer sees](profiles/pov/customer-access.md) |
| **changing the code** — a **contributor** | [Design notes](design/README.md), [runbooks](runbooks/README.md), [notes](notes/README.md), and [CONTRIBUTING.md](../CONTRIBUTING.md) |

## Platform reference

What the dashboard does, and the discipline it expects from you. These are written once
and shared: the page describing how to deploy a cloud VM is the same page whether you are
demoing, running a lab, or running production.

| Page | Read this when |
|---|---|
| [Infrastructure as Code](infrastructure-as-code.md) | you are about to deploy your first cloud resource and want to know what is actually running underneath. |
| [Onboarding Guide](ONBOARDING.md) | you are setting the dashboard up for the first time and want the shortest path to a running instance. |
| [Cloud Sandbox Guide](CLOUD_SANDBOX.md) | you want an isolated cloud account for the dashboard's labs, bootstrapped rather than hand-built. |
| [Cloud VMs](cloud-vms.md) | you are deploying cloud VMs and want the full access and onboarding story. |
| [Databases](databases.md) | you are standing up a managed database, or want to manage one you already run. |
| [Kubernetes](kubernetes.md) | you are managing Kubernetes clusters and the privileged access into them. |
| [Cloud Containers](cloud-containers.md) | you want a containerised app on a cloud runtime without standing up Portainer. |
| [Image Management](image-management.md) | you are about to build a custom image and need to know how it will reach the other clouds. |
| [Config Management](config-management.md) | you are about to run an Ansible job and want to know how the runner handles secrets and isolation. |
| [Secrets Management](secrets-management.md) | you are deciding where to store cloud credentials, and how to evolve that over time. |
| [Certificates](certificates.md) | you are onboarding certificate identities, and need a private CA to issue them from. |
| [Storage Management](storage-management.md) | you are enabling a feature that needs a storage backend, which several of them do. |
| [Remote Agents](remote-agents.md) | your hypervisors, databases or clusters live somewhere the dashboard cannot reach. |
| [Auto-delete Timer](auto-delete-timer.md) | you want lab resources to clean themselves up — read it before enabling it, because it deletes infrastructure. |
| [Notifications](notifications.md) | you want to hear about expiring resources and failed jobs without opening the dashboard. |
| [Action Guardrails](policy-guardrails.md) | you want disallowed deploys blocked before they start rather than reviewed after. |
| [Job Worker](job-worker.md) | a long job is sitting queued, or you are sizing the worker for more of them. |
| [Cloud Hosting](cloud-hosting.md) | you want the dashboard reachable from outside your LAN, or fronting remote agents. |
| [Config Migration](config-migration.md) | you are standing up a second instance and do not want to re-type months of configuration. |
| [Community vs. hosted](saas-comparison.md) | you are choosing between running this yourself and a managed edition. |
| [SaaS Roadmap](saas-roadmap.md) | you want to know which capabilities are reserved for the hosted edition, and why. |

## The rest of the tree

| Folder | What's in it |
|---|---|
| [profiles/](profiles/README.md) | The `demo` / `pov` gate, the per-feature matrix, and everything specific to one profile or the other. |
| [integrations/](integrations/README.md) | One page per external system the dashboard talks to — the BeyondTrust products, the hypervisors, the runners, SSO. |
| [design/](design/README.md) | Why a subsystem is shaped the way it is. Facts that are not recoverable from reading the code. |
| [runbooks/](runbooks/README.md) | Procedures to run against a real instance, usually to prove a phase of work landed. |
| [notes/](notes/README.md) | Dated investigations, kept for their conclusions rather than their narrative. |

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
