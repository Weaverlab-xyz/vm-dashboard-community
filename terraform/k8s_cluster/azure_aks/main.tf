terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.35" # >= 3.35, < 4.0: oidc_issuer/workload_identity + azurerm_federated_identity_credential need ≥3.35; must stay < 4.0 (the AAD-legacy `managed` block below is removed in 4.0)
    }
  }
  required_version = ">= 1.3.0"
}

# Auth comes from the ARM_* env the dashboard injects
# (terraform_provider_env.azure_env): ARM_CLIENT_ID / ARM_CLIENT_SECRET /
# ARM_TENANT_ID / ARM_SUBSCRIPTION_ID. No creds in this file or in state.
provider "azurerm" {
  features {
    # The per-cluster agent Key Vault (below) is torn down with the cluster.
    # Don't purge on destroy: purging needs the subscription-scope action
    # Microsoft.KeyVault/locations/deletedVaults/purge/action, which the
    # dashboard's service principal isn't granted — so a purge-on-destroy
    # fails the whole `terraform destroy` even though every resource
    # (including the vault's soft-delete) was already torn down. The vault's
    # name is derived per-cluster-id (agent_kv_name), so a lingering
    # soft-deleted vault never collides with a new cluster, and its 7-day
    # soft_delete_retention_days auto-purges it. recover_soft_deleted_key_vaults
    # still recovers a same-named soft-deleted vault on a same-cluster re-apply.
    key_vault {
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
  }
}

# The authenticated service principal — used to grant the dashboard's own
# identity cluster-admin (Azure RBAC for Kubernetes) so the minted AAD token
# (azure_service.aks_get_token) can drive the cluster.
data "azurerm_client_config" "current" {}

# ── Variables ────────────────────────────────────────────────────────────────

variable "location" {
  type        = string
  description = "Azure region for the AKS cluster (e.g. eastus)"
}

variable "cluster_name" {
  type        = string
  description = "AKS cluster name (unique within the resource group)"
}

variable "resource_group_name" {
  type        = string
  default     = ""
  description = "Resource group to create the cluster in. Empty = create a dedicated '<cluster_name>-rg' (self-contained, torn down with the cluster)."
}

variable "k8s_version" {
  type        = string
  default     = ""
  description = "Kubernetes version for the control plane + node pool. Empty = AKS default for the region."
}

variable "vm_size" {
  type        = string
  default     = "Standard_B2s"
  description = "VM size for the default node pool"
}

variable "node_count" {
  type        = number
  default     = 2
  description = "Node count for the default node pool"
}

variable "vnet_cidr" {
  type        = string
  default     = "10.96.0.0/16"
  description = "Address space for the self-contained VNet"
}

variable "subnet_cidr" {
  type        = string
  default     = "10.96.0.0/22"
  description = "Subnet for the cluster nodes + (Azure CNI) pods — /22 gives ~1k IPs"
}

# Public API endpoint restricted to these CIDRs. Empty = open to all (AKS does
# not accept 0.0.0.0/0 as an authorized range, so 'open' is expressed by leaving
# the access profile unset). Tighten to the dashboard's egress IP in real use.
variable "authorized_ip_ranges" {
  type        = list(string)
  default     = []
  description = "CIDRs allowed to reach the public API endpoint (empty = open to all)"
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Resource tags (managed-by, cluster id)"
}

# ── Entitle agent (workload identity + per-cluster Key Vault for azure_secret_manager) ──
# The agent's KMS backend on AKS is Azure Key Vault, reached via a federated
# user-assigned managed identity (workload identity). These are all defaulted so
# the destroy path (_build_cluster_tf_variables with opts={}) still resolves.

variable "cluster_id" {
  type        = string
  default     = ""
  description = "Dashboard cluster id — used to derive the per-cluster agent Key Vault's globally-unique name."
}

variable "agent_namespace" {
  type        = string
  default     = "entitle"
  description = "Namespace the Entitle agent runs in (the federated-credential subject's namespace)."
}

variable "agent_service_account" {
  type        = string
  default     = "entitle-agent-sa"
  description = "ServiceAccount the Entitle agent pod uses (federated-credential subject; the chart's default)."
}

# ── Networking (self-contained VNet + subnet; egress via managed outbound LB) ──

locals {
  rg_name = var.resource_group_name != "" ? var.resource_group_name : "${var.cluster_name}-rg"
  # Key Vault names are global + ≤24 chars, alphanumeric; derive a unique-per-cluster
  # slug from the (hyphen-stripped) cluster id. The dashboard always passes cluster_id
  # on both apply and destroy, so the name stays stable across the cluster's lifecycle.
  agent_kv_name = substr("entkv${replace(var.cluster_id, "-", "")}", 0, 24)
}

resource "azurerm_resource_group" "this" {
  count    = var.resource_group_name != "" ? 0 : 1
  name     = local.rg_name
  location = var.location
  tags     = var.tags
}

resource "azurerm_virtual_network" "this" {
  name                = "${var.cluster_name}-vnet"
  location            = var.location
  resource_group_name = local.rg_name
  address_space       = [var.vnet_cidr]
  tags                = var.tags
  depends_on          = [azurerm_resource_group.this]
}

resource "azurerm_subnet" "nodes" {
  name                 = "${var.cluster_name}-nodes"
  resource_group_name  = local.rg_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.subnet_cidr]
}

# ── AKS cluster ──────────────────────────────────────────────────────────────

resource "azurerm_kubernetes_cluster" "this" {
  name                = var.cluster_name
  location            = var.location
  resource_group_name = local.rg_name
  dns_prefix          = var.cluster_name
  kubernetes_version  = var.k8s_version != "" ? var.k8s_version : null
  tags                = var.tags

  # Workload identity: the Entitle agent pod uses a federated user-assigned MI to
  # reach Azure Key Vault (kmsType=azure_secret_manager). Both flags are required —
  # oidc_issuer_url is only populated when oidc_issuer_enabled = true.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  default_node_pool {
    name           = "default"
    node_count     = var.node_count
    vm_size        = var.vm_size
    vnet_subnet_id = azurerm_subnet.nodes.id
  }

  identity {
    type = "SystemAssigned"
  }

  # Azure CNI; outbound_type defaults to loadBalancer (managed egress to the
  # internet) — the Entitle agent's outbound to its SaaS works out of the box.
  network_profile {
    network_plugin = "azure"
  }

  # AAD-integrated, Azure RBAC for Kubernetes — the dashboard authenticates with
  # an AAD token (azure_service.aks_get_token) rather than a static admin cert.
  azure_active_directory_role_based_access_control {
    managed            = true
    azure_rbac_enabled = true
  }

  # Public endpoint; restrict only when authorized_ip_ranges is non-empty.
  dynamic "api_server_access_profile" {
    for_each = length(var.authorized_ip_ranges) > 0 ? [1] : []
    content {
      authorized_ip_ranges = var.authorized_ip_ranges
    }
  }

  depends_on = [azurerm_resource_group.this]
}

# Grant the dashboard's service principal cluster-admin via Azure RBAC so its
# minted AAD token has full cluster access (mirrors EKS, where the provisioning
# IAM principal is implicitly cluster admin).
resource "azurerm_role_assignment" "dashboard_admin" {
  scope                = azurerm_kubernetes_cluster.this.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = data.azurerm_client_config.current.object_id
}

# ── Entitle agent identity + per-cluster Key Vault (azure_secret_manager) ──────
# The agent stores its keys in Azure Key Vault instead of k8s Secrets (the
# in-cluster-Secrets path 401s on AKS). It authenticates with a federated
# user-assigned managed identity (workload identity): the entitle-agent chart
# annotates the agent ServiceAccount with this MI's client id + labels the pod
# (azure.workload.identity/*), the AKS webhook injects a token, and the MI holds
# Secrets Officer on the vault below. Live-validated on a standard AKS cluster.

resource "azurerm_key_vault" "agent" {
  name                       = local.agent_kv_name
  location                   = var.location
  resource_group_name        = local.rg_name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  enable_rbac_authorization  = true  # grant the agent MI via the role assignment below
  purge_protection_enabled   = false # torn down with the cluster; see the provider key_vault features
  soft_delete_retention_days = 7
  tags                       = var.tags
  depends_on                 = [azurerm_resource_group.this]
}

resource "azurerm_user_assigned_identity" "agent" {
  name                = "${var.cluster_name}-entitle-agent"
  location            = var.location
  resource_group_name = local.rg_name
  tags                = var.tags
  depends_on          = [azurerm_resource_group.this]
}

resource "azurerm_federated_identity_credential" "agent" {
  name                = "entitle-agent"
  resource_group_name = local.rg_name
  parent_id           = azurerm_user_assigned_identity.agent.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject             = "system:serviceaccount:${var.agent_namespace}:${var.agent_service_account}"
}

resource "azurerm_role_assignment" "agent_kv" {
  scope                = azurerm_key_vault.agent.id
  role_definition_name = "Key Vault Secrets Officer" # the agent writes + reads its own keys
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

# ── Outputs ──────────────────────────────────────────────────────────────────
# k8s_service._assemble_aks_kubeconfig builds a kubelogin exec kubeconfig from
# these; the transient runner swaps the exec for a server-minted AAD token
# (_runner_kubeconfig → azure_service.aks_get_token).

output "cluster_name" {
  value       = azurerm_kubernetes_cluster.this.name
  description = "AKS cluster name"
}

output "endpoint" {
  value       = azurerm_kubernetes_cluster.this.kube_config[0].host
  description = "API server URL (kubeconfig server / api_server)"
  # kube_config is sensitive in the azurerm provider, so any output derived from
  # it must be explicitly marked sensitive or `terraform apply` errors out. The
  # value is still emitted by `terraform output -json` (flagged sensitive) for
  # k8s_service._assemble_aks_kubeconfig to read.
  sensitive = true
}

output "ca_certificate" {
  value       = azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate
  description = "Cluster CA, base64 PEM (kubeconfig certificate-authority-data)"
  sensitive   = true # see endpoint above — kube_config is sensitive
}

# Entitle agent (azure_secret_manager) — the dashboard captures these per cluster
# and threads them into the chart's platform.azure.* Helm values.
output "agent_identity_client_id" {
  value       = azurerm_user_assigned_identity.agent.client_id
  description = "Client id of the agent's user-assigned MI (Helm platform.azure.clientId)"
}

output "agent_key_vault_name" {
  value       = azurerm_key_vault.agent.name
  description = "Per-cluster agent Key Vault name (Helm platform.azure.keyVaultName)"
}

output "agent_identity_tenant_id" {
  value       = data.azurerm_client_config.current.tenant_id
  description = "Tenant id for the agent pod (Helm platform.azure.tenantId)"
}
