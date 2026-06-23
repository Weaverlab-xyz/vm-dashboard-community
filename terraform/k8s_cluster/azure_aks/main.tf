terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
  required_version = ">= 1.3.0"
}

# Auth comes from the ARM_* env the dashboard injects
# (terraform_provider_env.azure_env): ARM_CLIENT_ID / ARM_CLIENT_SECRET /
# ARM_TENANT_ID / ARM_SUBSCRIPTION_ID. No creds in this file or in state.
provider "azurerm" {
  features {}
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

# ── Networking (self-contained VNet + subnet; egress via managed outbound LB) ──

locals {
  rg_name = var.resource_group_name != "" ? var.resource_group_name : "${var.cluster_name}-rg"
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
}

output "ca_certificate" {
  value       = azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate
  description = "Cluster CA, base64 PEM (kubeconfig certificate-authority-data)"
}
