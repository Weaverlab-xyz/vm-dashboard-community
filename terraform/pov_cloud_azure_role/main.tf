# Least-privilege Azure custom roles for a POV instance's Azure cloud provider
# (web_dashboard/services/pov_cloud_azure.py). The Azure twin of pov_cloud_aws_policy:
#
#   POV cloud read       what the request path needs: the overview, the environment list,
#                        Test connection and the image lookup.
#   POV cloud provision  read, plus what the worker's jobs need to build, power and tear
#                        down a POV: its resource group, VNet, NSG, public IPs, NICs and VMs.
#
# With the SPIFFE identity alone, assign provision to the federated app
# (azure_client_id). With SPIFFE for everyday calls and Workload Credentials for jobs,
# assign read to the federated app and provision to the Workload Credentials target app.
#
# Every action below is one pov_cloud_azure.py makes. Unlike IAM, Azure RBAC cannot
# condition these on a tag, and each POV creates its own resource group, so both roles are
# scoped to the SUBSCRIPTION. Keep POVs in a subscription of their own: that subscription
# is the blast radius. Neither role can write role assignments, so neither can widen
# itself.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 3.80"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

locals {
  scope = "/subscriptions/${var.subscription_id}"

  read_actions = [
    "Microsoft.Resources/subscriptions/read",
    "Microsoft.Resources/subscriptions/resourceGroups/read",
    "Microsoft.Compute/virtualMachines/read",
    "Microsoft.Compute/virtualMachines/instanceView/read",
    "Microsoft.Compute/disks/read",
    "Microsoft.Compute/images/read",
    "Microsoft.Compute/galleries/read",
    "Microsoft.Compute/galleries/images/read",
    "Microsoft.Compute/galleries/images/versions/read",
    "Microsoft.Network/virtualNetworks/read",
    "Microsoft.Network/virtualNetworks/subnets/read",
    "Microsoft.Network/networkSecurityGroups/read",
    "Microsoft.Network/networkInterfaces/read",
    "Microsoft.Network/publicIPAddresses/read",
  ]

  provision_actions = concat(local.read_actions, [
    # One resource group per POV; destroying the POV deletes it whole.
    "Microsoft.Resources/subscriptions/resourceGroups/write",
    "Microsoft.Resources/subscriptions/resourceGroups/delete",
    # Network: the VNet and its subnets, the NSG, public IPs, NICs — and the join actions
    # a NIC and a VM need to attach to them.
    "Microsoft.Network/virtualNetworks/write",
    "Microsoft.Network/virtualNetworks/delete",
    "Microsoft.Network/virtualNetworks/subnets/write",
    "Microsoft.Network/virtualNetworks/subnets/delete",
    "Microsoft.Network/virtualNetworks/subnets/join/action",
    "Microsoft.Network/networkSecurityGroups/write",
    "Microsoft.Network/networkSecurityGroups/delete",
    "Microsoft.Network/networkSecurityGroups/join/action",
    "Microsoft.Network/publicIPAddresses/write",
    "Microsoft.Network/publicIPAddresses/delete",
    "Microsoft.Network/publicIPAddresses/join/action",
    "Microsoft.Network/networkInterfaces/write",
    "Microsoft.Network/networkInterfaces/delete",
    "Microsoft.Network/networkInterfaces/join/action",
    # VMs, their managed OS disks, and power.
    "Microsoft.Compute/virtualMachines/write",
    "Microsoft.Compute/virtualMachines/delete",
    "Microsoft.Compute/virtualMachines/start/action",
    "Microsoft.Compute/virtualMachines/deallocate/action",
    "Microsoft.Compute/disks/write",
    "Microsoft.Compute/disks/delete",
  ])
}

resource "azurerm_role_definition" "read" {
  name        = "${var.name_prefix} read"
  scope       = local.scope
  description = "vm-dashboard POV cloud provider: read-only (request path)."

  permissions {
    actions = local.read_actions
  }

  assignable_scopes = [local.scope]
}

resource "azurerm_role_definition" "provision" {
  name        = "${var.name_prefix} provision"
  scope       = local.scope
  description = "vm-dashboard POV cloud provider: build, power and tear down POV environments (jobs)."

  permissions {
    actions = local.provision_actions
  }

  assignable_scopes = [local.scope]
}

# Optional: assign the roles here rather than by hand. Object ids of SERVICE PRINCIPALS
# (az ad sp show --id <client id> --query id), not application (client) ids.
resource "azurerm_role_assignment" "read" {
  for_each           = toset(var.read_principal_ids)
  scope              = local.scope
  role_definition_id = azurerm_role_definition.read.role_definition_resource_id
  principal_id       = each.value
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_assignment" "provision" {
  for_each           = toset(var.provision_principal_ids)
  scope              = local.scope
  role_definition_id = azurerm_role_definition.provision.role_definition_resource_id
  principal_id       = each.value
  principal_type     = "ServicePrincipal"
}
