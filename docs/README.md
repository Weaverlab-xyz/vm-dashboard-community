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
| **installing or running** the dashboard — an **operator** | [Getting started](getting-started.md), then the sections below |
| **running your own infrastructure** with it | the sections below — you want the `demo` profile, whatever its name suggests |
| **showing** it to someone, or running **customer proof-of-value** work | [Demo and POV profiles](profiles/README.md) |
| **changing the code** — a **contributor** | [Design notes](design/README.md), [runbooks](runbooks/README.md), [notes](notes/README.md), and [CONTRIBUTING.md](../CONTRIBUTING.md) |

## Sections

The sections follow the app. Six mirror the groups of its navigation menu, two mirror the
Settings → Integrations groups that have no menu of their own, and one is first-run setup.
Each is a hub page naming which page answers what, and a folder of the pages themselves,
so this index links the hubs and each hub links its own pages.

| Section | In the app | Read it when |
|---|---|---|
| [Getting started](getting-started.md) | first run | you are setting the dashboard up for the first time and want the shortest path to a running instance, then the per-cloud setup |
| [Hypervisors](hypervisors.md) | menu → Hypervisors | your VMs run on VMware, Proxmox, Hyper-V, Nutanix or XCP-ng |
| [Clouds](clouds.md) | menu → Clouds | you are putting workloads on AWS, Azure, GCP or OCI: how a deploy works, a sandbox to run it in, VMs and Windows servers |
| [Cloud services](cloud-services.md) | menu → Cloud services | you are running containers, databases, Kubernetes or functions |
| [Preview](preview.md) | menu → Preview | you are trying virtual desktops, directories or the Workload Lab |
| [Operations](operations.md) | menu → Operations | you are running it day to day: Config Management, scheduling, images, storage, inventory, jobs, costs and clean-up |
| [Admin](admin.md) | menu → Admin | you are deciding who may use it, where its secrets live and how you would prove what happened, or running its agents, health checks and notifications |
| [BeyondTrust](beyondtrust.md) | Settings → BeyondTrust | you are wiring it into Privileged Remote Access, Password Safe, EPM for Linux or Entitle |
| [Platform](platform.md) | Settings → Platform | you are deciding how people sign in, how the dashboard proves its own identity, where it is hosted, or how its runners and AI clients connect |
| [Editions](editions.md) | — | you are choosing between running the dashboard yourself and a hosted edition |

## Collections

Folders of pages that share a kind rather than a feature, each indexed by its own `README.md`.

| Folder | What's in it |
|---|---|
| [profiles/](profiles/README.md) | The `demo` / `pov` gate, the per-feature matrix, and everything specific to one profile or the other: the personas, the demo cells, what a POV customer sees. |
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
