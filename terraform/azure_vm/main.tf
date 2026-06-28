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
  subscription_id = var.subscription_id
  client_id       = var.client_id
  client_secret   = var.client_secret
  tenant_id       = var.tenant_id
}

# ── Variables ────────────────────────────────────────────────────────────────

variable "subscription_id" {
  type        = string
  description = "Azure Subscription ID"
}

variable "client_id" {
  type        = string
  description = "Azure Service Principal App ID"
}

variable "client_secret" {
  type        = string
  sensitive   = true
  description = "Azure Service Principal Password"
}

variable "tenant_id" {
  type        = string
  description = "Azure Tenant ID"
}

variable "vm_name" {
  type        = string
  description = "Name of the virtual machine"
}

variable "resource_group" {
  type        = string
  description = "Resource group name"
}

variable "location" {
  type        = string
  default     = "eastus"
  description = "Azure region"
}

variable "vm_size" {
  type        = string
  default     = "Standard_B2s"
  description = "Azure VM size"
}

variable "image_id" {
  type        = string
  description = "Full ARM resource ID of the source image"
}

variable "subnet_id" {
  type        = string
  description = "Subnet resource ID"
}

variable "nsg_ids" {
  type        = list(string)
  default     = []
  description = "List of NSG resource IDs to attach to the NIC"
}

variable "ssh_username" {
  type        = string
  default     = "azureuser"
  description = "Admin username"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key text (RSA)"
}

variable "create_public_ip" {
  type        = bool
  default     = false
  description = "Whether to create and attach a public IP"
}

# ── Optional Public IP ───────────────────────────────────────────────────────

resource "azurerm_public_ip" "vm_pip" {
  count               = var.create_public_ip ? 1 : 0
  name                = "${var.vm_name}-pip"
  resource_group_name = var.resource_group
  location            = var.location
  allocation_method   = "Dynamic"
  tags = {
    "managed-by" = "vm-dashboard"
  }
}

# ── Network Interface ────────────────────────────────────────────────────────

resource "azurerm_network_interface" "vm_nic" {
  name                = "${var.vm_name}-nic"
  resource_group_name = var.resource_group
  location            = var.location

  ip_configuration {
    name                          = "ipconfig1"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = var.create_public_ip ? azurerm_public_ip.vm_pip[0].id : null
  }

  tags = {
    "managed-by" = "vm-dashboard"
  }
}

resource "azurerm_network_interface_security_group_association" "vm_nic_nsg" {
  count                     = length(var.nsg_ids) > 0 ? 1 : 0
  network_interface_id      = azurerm_network_interface.vm_nic.id
  network_security_group_id = var.nsg_ids[0]
}

# ── Virtual Machine ──────────────────────────────────────────────────────────

resource "azurerm_linux_virtual_machine" "vm" {
  name                = var.vm_name
  resource_group_name = var.resource_group
  location            = var.location
  size                = var.vm_size
  admin_username      = var.ssh_username

  network_interface_ids = [azurerm_network_interface.vm_nic.id]

  admin_ssh_key {
    username   = var.ssh_username
    public_key = var.ssh_public_key
  }

  source_image_id = var.image_id

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_delete_option   = "Delete"
  }

  tags = {
    "managed-by" = "vm-dashboard"
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "vm_id" {
  value = azurerm_linux_virtual_machine.vm.id
}

output "vm_name" {
  value = azurerm_linux_virtual_machine.vm.name
}

output "private_ip" {
  value = azurerm_network_interface.vm_nic.private_ip_address
}

output "public_ip" {
  value = var.create_public_ip ? azurerm_public_ip.vm_pip[0].ip_address : null
}
