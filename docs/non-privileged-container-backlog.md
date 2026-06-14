# Non-privileged container integrations — cross-pollination backlog

> **Purpose:** a logged backlog of **non-privileged containers** that are
> candidates to expose for user deployment in **Azure Container Instances (ACI)**,
> **AWS ECS (Fargate)**, and **GCE + Container-Optimized OS**, once a
> general-purpose container-deploy seam ships. Not committed work — captured for
> later cross-pollination. Grouped by which existing dashboard stream each app
> plugs into. This is the cloud-runtime sibling of the Kubernetes app-integration
> backlog (logged separately in the engineering planning workspace).
>
> *Logged 2026-06-14.*

## Where we are today

We already run containers in ACI (container groups), ECS (Fargate tasks), and
GCP (Cloud Run Jobs + a GCE/Container-Optimized-OS Jumpoint) — but only for our
*own* internal workloads: the BeyondTrust Jumpoint daemon, the Ansible runner,
and the image promote-runner (`web_dashboard/services/{azure,aws,gcp}_service.py`).
The **only privileged path** is the *EC2*-launch ECS Jumpoint, which adds
`NET_ADMIN` / `NET_RAW` + `/dev/net/tun` for tunneling (`aws_service.py`). On ACI,
Fargate, Cloud Run, and GCE-COS those same workloads already run **non-privileged**.

There is no general-purpose "deploy a container" catalog for these runtimes today —
`/api/containers/deploy` routes only to Portainer. The apps below are what such a
catalog could safely offer, since each runs inside the non-privileged envelope all
three named runtimes enforce.

## The non-privileged envelope

All three targets run standard non-privileged OCI images (CPU/mem, env, ports,
optional managed volume). None allow privileged mode, host devices, host
networking, or kernel capabilities for customer workloads.

| Capability | ACI | ECS Fargate | GCE + COS (konlet) |
|---|---|---|---|
| Multi-container in one unit | Yes (container group) | Yes (task, multiple containers) | One container per instance |
| One-shot job pattern | `restart=Never` (existing) | task exits (existing) | run-once VM |
| Long-running service | Yes | Yes | Yes |
| Managed volume | Azure Files | EFS | Persistent disk |
| Privileged / host devices | No | No (EC2 launch only) | No |

## Access / brokered entry — `saas-virtual-desktop` + existing Jumpoint seam

| App | What it is | Plugs into | Note |
|---|---|---|---|
| **BeyondTrust Jumpoint** | Outbound-only access daemon | existing Jumpoint seam | *Existing baseline.* Already runs **non-privileged** on ACI / Cloud Run; the privileged exception is only the EC2-launch ECS path. Proves the seam is already there. |
| **Apache Guacamole** (guacd + webapp) | Clientless RDP/SSH/VNC gateway | `saas-virtual-desktop` | Non-privileged; brokerable through the PRA Jumpoint. ACI multi-container group / Fargate task / GCE-COS. |
| **Kasm Workspaces single-container desktops** (`kasmweb/*`) | Streamed desktops / browsers | `saas-virtual-desktop` | The non-privileged, single-image counterpart to the k8s-native Kasm entry on the Kubernetes backlog. ACI / Fargate friendly. |

## Image supply-chain / scanning — `saas-image-supplychain` + `saas-self-supplychain` + `saas-ai-image-hardening`

Roadmap anchors: *Continuous CVE scanning per image version*, *Per-tenant signed
build manifests*, *Self-supply-chain for the platform's own privileged containers*.

| App | What it is | Plugs into | Note |
|---|---|---|---|
| **Trivy** | One-shot image / SBOM CVE scan | `saas-image-supplychain` (CVE scanning) | Non-privileged job; mirrors the existing promote-runner job pattern. The serverless counterpart to the Kubernetes backlog's Trivy-Operator. |
| **Syft + Grype** (Anchore) | SBOM generation + vuln scan | `saas-self-supplychain` (SBOM) • `saas-image-supplychain` (CVE) | Pure-CLI one-shot jobs. |
| **Cosign** (sigstore) | Sign / verify images | `saas-self-supplychain` • `saas-image-supplychain` (signed manifests) | One-shot job; the cloud-runtime half of signing the runner / Arc-worker images and producing per-tenant signed manifests. |
| **Harbor** | Registry with built-in scan + signing | *(not a fit)* | Stateful — needs a DB + persistent storage. Out of scope for serverless single containers; called out so it isn't mistaken for a candidate. |

## Policy / admission / compliance-as-code — `saas-action-admission` + `saas-iac-hardening`

Roadmap anchors: *Action-level policy guardrails (pre-action admission control)*,
*Compliance-as-code*.

| App | What it is | Plugs into | Note |
|---|---|---|---|
| **OPA (Open Policy Agent) server** | Non-privileged REST decision service | `saas-action-admission` • `saas-iac-hardening` (compliance-as-code) | Cloud-runtime equivalent of the in-cluster Gatekeeper/Kyverno entry: the same OPA policy intent the dashboard evaluates pre-action, runnable as an ACI / Fargate service. |
| **Conftest** | One-shot OPA test of Terraform plans / manifests | `saas-iac-hardening` (compliance-as-code) | Feeds compliance-as-code as a job. |

## IaC & config-drift — `saas-iac-hardening` + `saas-config-drift`

Roadmap anchors: *Continuous drift detection*, *Centralised Terraform state with
locking*, *Drift-aware runs*.

| App | What it is | Plugs into | Note |
|---|---|---|---|
| **Checkov / tfsec / Terrascan** | IaC static analysis | `saas-iac-hardening` (compliance-as-code) | One-shot jobs scanning Terraform. |
| **terraform (`plan`-in-a-container) / driftctl** | Drift detection | `saas-iac-hardening` (drift detection) • `saas-config-drift` | The containerized form of the roadmap's `terraform plan` drift check. |

## Durable workflows — `image-promote-saas`

Roadmap anchor: *Durable cross-cloud promote (SaaS replay-safety)*.

| App | What it is | Plugs into | Note |
|---|---|---|---|
| **Temporal worker** | Durable workflow worker | `image-promote-saas` | Non-privileged; Fargate / Cloud-Run / ACI friendly — the promote-runner could become a Temporal worker. The Temporal **server** needs a managed DB (workers fit serverless, server does not). |

## Secrets (job-time) — `secrets-management`

| App | What it is | Plugs into | Note |
|---|---|---|---|
| *(note only)* BeyondTrust Password Safe at job start | Secret fetch into the container env | `secrets-management` | For non-privileged one-shot containers the in-cluster ESO / Secrets-Agent pattern collapses to a **job-start fetch** — already done via secure env values (`secure_value` in `azure_service.py`). No new deployable app; logged so the gap is explicit. |

---

A mirrored copy of this backlog is logged in the engineering planning workspace
(Notion), as a child of *VM Dashboard Execution Plan — Phase Tracker*, alongside
the Kubernetes app-integration backlog.
