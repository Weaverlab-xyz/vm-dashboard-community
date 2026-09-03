# Azure setup

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are giving the dashboard an Azure subscription to deploy into.

Part of the [Onboarding Guide](../ONBOARDING.md).


The dashboard deploys Azure VMs into **your** Azure subscription using a
service principal (SP) scoped to a single resource group.

### 1. Log in and pick a subscription

```powershell
az login
az account show --query id -o tsv
```

Copy the subscription id.

### 2. Create the resource group and pick a region

```powershell
az group create --name dashboard-rg --location eastus
```

The `AZURE_RESOURCE_GROUP` value in `.env` becomes the default RG for deployed
VMs, and `AZURE_LOCATION` its region (e.g. `centralus`, `eastus`,
`westeurope`). Create it now rather than letting the first deploy create it —
the SP in the next step is scoped to this resource group, so it has to exist
first.

### 3. Create the service principal

```powershell
az ad sp create-for-rbac `
  --name "dashboard-dev" `
  --role Contributor `
  --scopes /subscriptions/<your-subscription-id>/resourceGroups/dashboard-rg
```

The output includes `appId`, `password`, and `tenant` — you need all three
(plus the subscription id) for `.env`.

> **Scope it to the resource group, not the subscription.** The sandbox scripts
> scope `Contributor` to a single RG, and so should you; a subscription-wide
> grant hands the dashboard every resource you own. Widen it only if you
> deliberately want deploys landing outside `dashboard-rg`.

> **Security note:** the client secret (`password`) rotates. Azure will
> warn you when it nears expiry; create a new one and update `.env`.

### 4. Grant the additional roles

`Contributor` on the RG is **not sufficient on its own.** It is a control-plane
role, so it cannot write blob data — and Terraform state is blob data. Add the
grants for the features you intend to use:

| Feature | Grant | Scope |
|---------|-------|-------|
| **Terraform state + storage backend** | `Storage Blob Data Contributor` | the storage account |
| Secrets backend (Key Vault) | secret permissions `get list set delete` | the vault |
| Kubernetes clusters (AKS) | `User Access Administrator` | the resource group |
| Cloud Costs | `Cost Management Reader` | the **subscription** |
| Container registry pulls | `AcrPull` | the registry |
| Image promote into a Compute Gallery | a custom role with the gallery/image actions, or `Contributor` | the gallery's RG |

```powershell
# Terraform state and the azure_blob storage backend — data plane, required
az role assignment create `
  --assignee <appId> `
  --role "Storage Blob Data Contributor" `
  --scope /subscriptions/<sub-id>/resourceGroups/dashboard-rg/providers/Microsoft.Storage/storageAccounts/<account>

# Key Vault secrets backend — write is required so per-VM admin passwords can
# be vaulted and removed again on teardown
az keyvault set-policy --name <vault> `
  --object-id <sp-object-id> `
  --secret-permissions get list set delete

# AKS only: the cluster module creates its own role assignment, which
# Contributor cannot do (it lacks Microsoft.Authorization/roleAssignments/write)
az role assignment create `
  --assignee <appId> `
  --role "User Access Administrator" `
  --scope /subscriptions/<sub-id>/resourceGroups/dashboard-rg

# Cloud Costs only: cost data is queried at subscription scope
az role assignment create `
  --assignee <appId> `
  --role "Cost Management Reader" `
  --scope /subscriptions/<sub-id>
```

> **Key Vault: access policy or RBAC.** The command above uses a vault access
> policy, which is what the sandbox scripts do. If your vault uses the RBAC
> permission model instead, the equivalent is the **Key Vault Secrets Officer**
> role — see [secrets-management.md](../secrets-management.md#iam-permissions-required-per-backend).

For the authoritative list, see the role assignments in
[`scripts/sandbox/Linux/setup-azure.sh`](../../scripts/sandbox/Linux/setup-azure.sh);
that script is the source of truth.

---
