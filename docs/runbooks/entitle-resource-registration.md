# Runbook — Entitle resource registration (E2E)

End-to-end check that built VMs/DBs register as Entitle integrations, and (for
private targets) that the Entitle agent makes them reachable. See
[`../integrations/entitle.md`](../integrations/entitle.md) and
[`../design/entitle-resource-registration.md`](../design/entitle-resource-registration.md).

## Prerequisites
- An Entitle tenant; in the Entitle console note an **owner** user UUID and a
  **workflow** UUID, and confirm the application slugs (`ssh`, `postgresql`,
  `mysql`, `mssql`) via the `entitle_applications` data source.
- Settings → Integrations → Entitle: API URL (pre-filled), API token, Terraform provider
  API key, **Owner ID** (`entitle_owner_id`) and **Workflow ID** (`entitle_workflow_id`) —
  all in the panel — and toggle **Register built VMs & databases in Entitle** on. The SSH
  sudo user is optional (each VM registers with its cloud-default login user automatically).

## 1. Public resource (no agent)
1. Build a **public** Linux VM (AWS/Azure/GCP) with **Register in Entitle** checked
   (Azure/GCP: a VM with a public IP).
2. Expect: the deploy job logs `Registered in Entitle (integration …)`; an
   **SSH ephemeral accounts** integration appears in Entitle with no `agent_token`.
3. Terminate the VM → the integration is removed.

> If registration is skipped, confirm both the global **Registration enabled** flag
> and the per-build checkbox are on (`docker compose logs app | grep -i entitle`).

## 2. Provision the agent (one-time, for private targets)
1. **Provision a dedicated cluster**: `POST /api/k8s/clusters/provision` (EKS), or
   register an existing one.
2. **Install the agent**: the cluster page → **Entitle agent** button (or
   `POST /api/k8s/clusters/{id}/entitle-agent {"action":"install"}`). On first install the
   dashboard **auto-mints** an agent token via the provider key, stashes its value in the
   secrets backend (`entitle_agent_token_ref` → `config://entitle/agent-token`), and records
   `entitle_agent_token_name` — no manual token step. (Pre-set `entitle_agent_token_ref`
   yourself only to use an externally-minted token.) `entitle_agent_chart_repo` defaults to
   `https://anycred.github.io/entitle-charts/`; add `entitle_agent_helm_extra_set` only if the
   bundled Datadog needs it. Track the `k8s_entitle_agent` job; expect the agent pod healthy
   and `entitle_agent_cluster_id` set.

## 3. Private resource (agent path)
1. Build a **private** cloud database (RDS) — or a private VM — with **Register in
   Entitle** checked.
2. Expect: a PostgreSQL/MySQL/SQL Server (or SSH) integration in Entitle **with**
   `agent_token = {entitle_agent_token_name}`; the agent reaches the private target.
3. Request JIT access in Entitle → confirm the ephemeral account / DB role is minted.
4. Decommission → the integration is removed.

## Notes
- Registration is **non-fatal**: a failure never blocks the VM/DB; the reason lands
  on the job. A private target with no agent configured raises a clear error.
- The agent token is passed to the chart as a plaintext Helm value (`agent.token`,
  streamed over stdin — not `--set`; chart limit) — resolved server-side, but it lands in
  the in-cluster Helm release Secret.
  The agent's **integration** secrets are governed by `entitle_agent_kms_type` (set to
  a cloud secret manager / Vault to keep them out of etcd) — not ESO.
