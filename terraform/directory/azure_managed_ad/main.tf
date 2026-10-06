# Microsoft Entra Domain Services — Azure's managed Active Directory, driven by
# web_dashboard/services/directory_service.py.
#
# Unlike AWS and GCP there is NO built-in administrator: admins are Entra users in the
# tenant's "AAD DC Administrators" group, and their passwords sync from Entra. This module
# creates no user and changes no group; the service pins an existing account (a Password
# Safe managed account) for domain joins after the build.
#
# Preconditions the service checks BEFORE this runs, because each fails late and
# expensively here: the Microsoft.AAD resource provider is registered, the Domain
# Services service principal (appId 2565bd9d-da50-47d4-8b85-4c97f669dc36) exists in the
# tenant, and the subscription has no Entra DS already (one per tenant).
#
# It gets its own subnet and network security group in an existing VNet. Pointing that
# VNet's DNS at the domain controllers is opt-in (manage_vnet_dns): it changes name
# resolution for every VM on the VNet, and joining needs it unless DNS already forwards
# the domain.
#
# Creation takes 45–60 minutes. It bills while it exists; the service refuses to destroy
# one that dashboard VMs are still joined to.

terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "azurerm" {
  features {}
}

variable "resource_group_name" {
  type        = string
  description = "Resource group the managed domain lands in"
}

variable "location" {
  type        = string
  description = "Azure region (e.g. eastus)"
}

variable "domain_name" {
  type        = string
  description = "Fully qualified domain name, e.g. aadds.contoso.com"
}

variable "sku" {
  type        = string
  default     = "Standard"
  description = "Standard | Enterprise | Premium"
}

variable "vnet_resource_group" {
  type        = string
  description = "Resource group of the VNet the domain controllers join"
}

variable "vnet_name" {
  type = string
}

variable "subnet_cidr" {
  type        = string
  description = "An unused /24 in that VNet for the domain controllers' dedicated subnet"
}

variable "manage_vnet_dns" {
  type        = bool
  default     = false
  description = "Point the VNet's DNS servers at the domain controllers"
}

variable "directory_row_id" {
  type = string
}

locals {
  short = substr(replace(var.directory_row_id, "-", ""), 0, 8)
  tags = {
    "managed-by"             = "vm-dashboard"
    "dashboard-directory-id" = var.directory_row_id
  }
}

data "azurerm_virtual_network" "vnet" {
  name                = var.vnet_name
  resource_group_name = var.vnet_resource_group
}

resource "azurerm_subnet" "ds" {
  name                 = "aadds-${local.short}"
  resource_group_name  = var.vnet_resource_group
  virtual_network_name = var.vnet_name
  address_prefixes     = [var.subnet_cidr]
}

# The two inbound rules Microsoft documents for a managed domain's subnet: PowerShell
# remoting from the service's own management plane, and RDP from Microsoft's support
# hosts. Nothing else is opened.
resource "azurerm_network_security_group" "ds" {
  name                = "aadds-${local.short}-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = local.tags

  security_rule {
    name                       = "AllowPSRemoting"
    priority                   = 301
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5986"
    source_address_prefix      = "AzureActiveDirectoryDomainServices"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowRD"
    priority                   = 201
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3389"
    source_address_prefix      = "CorpNetSaw"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "ds" {
  subnet_id                 = azurerm_subnet.ds.id
  network_security_group_id = azurerm_network_security_group.ds.id
}

resource "azurerm_active_directory_domain_service" "ds" {
  name                = "aadds-${local.short}"
  location            = var.location
  resource_group_name = var.resource_group_name
  domain_name         = var.domain_name
  sku                 = var.sku
  tags                = local.tags

  initial_replica_set {
    subnet_id = azurerm_subnet.ds.id
  }

  notifications {
    notify_dc_admins     = true
    notify_global_admins = true
  }

  security {
    sync_kerberos_passwords = true
    sync_ntlm_passwords     = true
    sync_on_prem_passwords  = true
  }

  depends_on = [azurerm_subnet_network_security_group_association.ds]
}

resource "azurerm_virtual_network_dns_servers" "ds" {
  count              = var.manage_vnet_dns ? 1 : 0
  virtual_network_id = data.azurerm_virtual_network.vnet.id
  dns_servers        = azurerm_active_directory_domain_service.ds.initial_replica_set[0].domain_controller_ip_addresses
}

output "resource_name" {
  value = azurerm_active_directory_domain_service.ds.id
}

output "dns_ip_addresses" {
  value = azurerm_active_directory_domain_service.ds.initial_replica_set[0].domain_controller_ip_addresses
}

output "subnet_id" {
  value = azurerm_subnet.ds.id
}
