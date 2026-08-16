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

# ── Variables ────────────────────────────────────────────────────────────────

variable "resource_group_name" {
  type        = string
  description = "Existing resource group for the plan + function app"
}

variable "location" {
  type        = string
  description = "Azure region"
}

variable "name" {
  type        = string
  description = "Function App name (globally unique — it becomes <name>.azurewebsites.net)"
}

variable "workload" {
  type        = string
  description = "Workload packaged into the zip; surfaced to the handler as FN_WORKLOAD"
}

# The code is delivered as a blob URL in an app setting rather than through the
# Kudu /api/publish endpoint that Flex Consumption requires — that endpoint cannot
# be driven from Terraform, and going there would mean a hand-rolled Kudu client
# plus Oryx remote build (pip installing from PyPI at deploy time), i.e. a second
# deployment channel neither AWS nor GCP has.
variable "package_sas_url" {
  type        = string
  sensitive   = true
  description = "Blob SAS URL of the package. MUST contain the content hash in the path — WEBSITE_RUN_FROM_PACKAGE is a plain string setting, so reusing a fixed blob name leaves terraform seeing no diff while the app serves stale code."
}

variable "storage_account_name" {
  type        = string
  description = "Storage account backing the Functions host (AzureWebJobsStorage)"
}

variable "storage_account_access_key" {
  type        = string
  sensitive   = true
  description = "Access key for that storage account"
}

variable "shared_secret" {
  type        = string
  sensitive   = true
  description = "Bearer secret the handler verifies on every request (fnruntime.auth). Independent of the host key."
}

# B1 is the default because it is the cheapest SKU that supports BOTH regional
# VNet integration and run-from-package. Y1 (Linux Consumption) is cheaper still
# but CANNOT do VNet integration, which makes the private-database case impossible
# — the precondition below refuses that combination at plan time rather than
# letting it deploy and fail mysteriously at runtime.
variable "sku_name" {
  type        = string
  default     = "B1"
  description = "App Service plan SKU (B1 | S1 | P0v3 | EP1 | Y1). Y1 cannot do VNet integration."
}

variable "python_version" {
  type    = string
  default = "3.12"
}

variable "environment" {
  type        = map(string)
  default     = {}
  description = "Extra NON-SECRET app settings for the handler"
}

# ── Networking (optional) ─────────────────────────────────────────────────────

variable "subnet_id" {
  type        = string
  default     = ""
  description = "Subnet DELEGATED TO Microsoft.Web/serverFarms. This is a different subnet from the database one (delegated to the DB service). Empty = no VNet integration."
}

locals {
  vnet_attached = length(trimspace(var.subnet_id)) > 0

  base_settings = {
    FUNCTIONS_WORKER_RUNTIME = "python"
    # Pinned explicitly: the v2 programming model silently registers ZERO
    # functions on an older host, producing an app that boots, serves the default
    # landing page, and 404s every route.
    FUNCTIONS_EXTENSION_VERSION = "~4"
    AzureWebJobsFeatureFlags    = "EnableWorkerIndexing"
    WEBSITE_RUN_FROM_PACKAGE    = var.package_sas_url

    FN_SHARED_SECRET = var.shared_secret
    FN_WORKLOAD      = var.workload
    FN_CLOUD         = "azure"
    FN_REGION        = var.location
    FN_NAME          = var.name
    FN_NETWORK_MODE  = local.vnet_attached ? "vnet" : "public"
    FN_NETWORK       = var.subnet_id
  }

  # Azure's platform DNS resolver. vnet_route_all_enabled fixes ROUTING; it does
  # nothing for DNS, so without this the privatelink.* private zone never resolves
  # and a correctly-routed function still cannot find the database.
  dns_settings = local.vnet_attached ? { WEBSITE_DNS_SERVER = "168.63.129.16" } : {}
}

resource "azurerm_service_plan" "this" {
  name                = "${var.name}-plan"
  resource_group_name = var.resource_group_name
  location            = var.location
  os_type             = "Linux"
  sku_name            = var.sku_name

  tags = {
    ManagedBy = "vm-dashboard"
  }

  lifecycle {
    precondition {
      condition     = !local.vnet_attached || var.sku_name != "Y1"
      error_message = "VNet integration requires an Elastic Premium or App Service plan; Linux Consumption (Y1) does not support it. Use B1 (default) or EP1, or deploy without a subnet."
    }
  }
}

resource "azurerm_linux_function_app" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  service_plan_id     = azurerm_service_plan.this.id

  storage_account_name       = var.storage_account_name
  storage_account_access_key = var.storage_account_access_key

  https_only = true

  # Required for Key Vault references in app_settings. A workload needing a
  # database credential gets it as @Microsoft.KeyVault(SecretUri=...) in
  # `environment`: the PLATFORM resolves it before the worker starts, so the
  # handler needs no SDK and the secret never enters Terraform state. That
  # resolution is performed as this identity, so without it the setting silently
  # stays the literal @Microsoft.KeyVault(...) string and the workload sees
  # nonsense rather than a password. Grant it `get` on the vault's secrets.
  identity {
    type = "SystemAssigned"
  }

  virtual_network_subnet_id = local.vnet_attached ? var.subnet_id : null

  site_config {
    application_stack {
      python_version = var.python_version
    }
    # Send ALL outbound traffic through the VNet, not just RFC1918 — otherwise
    # the private endpoint is reachable but nothing else is consistent.
    vnet_route_all_enabled = local.vnet_attached
    ftps_state             = "Disabled"
  }

  app_settings = merge(var.environment, local.base_settings, local.dns_settings)

  tags = {
    ManagedBy = "vm-dashboard"
    Workload  = var.workload
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "resource_id" {
  value       = azurerm_linux_function_app.this.id
  description = "Function App resource id"
}

# A BASE url, not a single route: the workload routes on the path (an Entitle
# Remote Adapter's verb IS the path), so callers append /give_access and friends.
# The /api prefix comes from host.json's routePrefix and is stripped again by the
# adapter, so the same routes read identically on all three clouds.
#
# The host KEY is not here — azurerm exposes no data source for it — so the service
# fetches it post-apply over the ARM REST API.
output "invoke_url" {
  value       = "https://${azurerm_linux_function_app.this.default_hostname}/api"
  description = "HTTPS base URL — an Entitle REST integration appends /give_access etc. (plus ?code=<host key>)"
}

output "health_url" {
  value       = "https://${azurerm_linux_function_app.this.default_hostname}/api/health"
  description = "Anonymous liveness route — a 200 here proves the v2 model actually indexed the function"
}

output "default_hostname" {
  value       = azurerm_linux_function_app.this.default_hostname
  description = "App hostname, used to fetch the host key over ARM"
}

output "network_mode" {
  value       = local.vnet_attached ? "vnet" : "public"
  description = "Whether the app was integrated with a VNet"
}

output "principal_id" {
  value       = azurerm_linux_function_app.this.identity[0].principal_id
  description = "System-assigned identity — grant it `get` on a Key Vault's secrets for @Microsoft.KeyVault(...) app settings to resolve"
}
