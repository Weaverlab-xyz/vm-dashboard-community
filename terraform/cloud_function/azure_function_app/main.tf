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

# The bearer secret arrives one of two ways, and exactly one of them at a time.
#
# `shared_secret_kv_uri` is the supported path: the dashboard has already written the
# secret to Key Vault and passes a REFERENCE, so the value never becomes an app
# setting and never enters Terraform state — which makes Azure the only one of the
# three clouds where it does neither.
#
# `shared_secret` is the literal value, for a caller driving this module directly
# with no vault. The precondition on the app refuses both-or-neither rather than
# letting an empty setting deploy a function that fails auth closed on every request.
variable "shared_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Bearer secret the handler verifies on every request (fnruntime.auth). Leave empty when shared_secret_kv_uri is set."
}

variable "shared_secret_kv_uri" {
  type        = string
  default     = ""
  description = "@Microsoft.KeyVault(SecretUri=…) reference to the bearer secret. Not sensitive: it is a name, not a value."
}

# Needed only to GRANT this app's identity read on the vault. Without the grant the
# app setting stays the literal @Microsoft.KeyVault(...) string, the handler sees
# nonsense, and every request fails closed — the failure this module exists to avoid.
variable "key_vault_name" {
  type        = string
  default     = ""
  description = "Key Vault holding the referenced secrets. Required with shared_secret_kv_uri."
}

variable "key_vault_resource_group" {
  type        = string
  default     = ""
  description = "Resource group of key_vault_name. Empty = the function's own resource group."
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

# The dashboard sizes every function the same way on all three clouds, so this
# variable has to EXIST here even though Azure expresses it differently: there is no
# per-function timeout argument, only host.json's `functionTimeout`, which an app
# setting overrides. Leaving it undeclared is not a no-op — terraform fails the whole
# apply with "Value for undeclared variable" before it creates anything.
#
# Memory is deliberately NOT a variable: on an App Service plan it is a property of
# the plan SKU (var.sku_name), not of the app, so the service does not pass memory_mb
# to this module at all.
variable "timeout_seconds" {
  type        = number
  default     = 60
  description = "Max execution time per invocation, written to AzureFunctionsJobHost__functionTimeout. Note an HTTP-triggered function is still capped at 230s by the Azure front door, whatever this says; and a Consumption (Y1) plan caps it at 600s."

  validation {
    condition     = var.timeout_seconds > 0
    error_message = "timeout_seconds must be positive."
  }
}

# ── Networking (optional) ─────────────────────────────────────────────────────

variable "subnet_id" {
  type        = string
  default     = ""
  description = "Subnet DELEGATED TO Microsoft.Web/serverFarms. This is a different subnet from the database one (delegated to the DB service). Empty = no VNet integration."
}

locals {
  vnet_attached = length(trimspace(var.subnet_id)) > 0

  kv_enabled = length(trimspace(var.shared_secret_kv_uri)) > 0
  kv_rg      = var.key_vault_resource_group != "" ? var.key_vault_resource_group : var.resource_group_name

  # host.json takes HH:MM:SS, not seconds. floor() because format's %d refuses a
  # fractional number, and HCL division is float division.
  function_timeout = format("%02d:%02d:%02d", floor(var.timeout_seconds / 3600), floor((var.timeout_seconds % 3600) / 60), var.timeout_seconds % 60)

  base_settings = {
    FUNCTIONS_WORKER_RUNTIME = "python"
    # Pinned explicitly: the v2 programming model silently registers ZERO
    # functions on an older host, producing an app that boots, serves the default
    # landing page, and 404s every route.
    FUNCTIONS_EXTENSION_VERSION = "~4"
    AzureWebJobsFeatureFlags    = "EnableWorkerIndexing"
    WEBSITE_RUN_FROM_PACKAGE    = var.package_sas_url

    # Overrides host.json's functionTimeout without republishing the package, which
    # is the only way to set it from Terraform — the double underscore is the
    # documented app-setting spelling of a nested host.json key.
    AzureFunctionsJobHost__functionTimeout = local.function_timeout

    FN_SHARED_SECRET = local.kv_enabled ? var.shared_secret_kv_uri : var.shared_secret
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

# A USER-assigned identity, and that is the whole point of it existing.
#
# A system-assigned identity does not exist until the app has been created, so the
# app would boot with an unresolvable Key Vault reference and only recover when Azure
# re-checks references — up to 24 hours later. A user-assigned identity can be
# created and granted BEFORE the app, so the very first cold start resolves.
resource "azurerm_user_assigned_identity" "kv" {
  count               = local.kv_enabled ? 1 : 0
  name                = "${var.name}-kv-id"
  resource_group_name = var.resource_group_name
  location            = var.location

  tags = {
    ManagedBy = "vm-dashboard"
  }
}

data "azurerm_key_vault" "shared" {
  count               = local.kv_enabled ? 1 : 0
  name                = var.key_vault_name
  resource_group_name = local.kv_rg
}

# Which grant to create is not a preference. An RBAC vault ignores access policies
# outright, and a policy-based vault ignores role assignments — pick the wrong one
# and Terraform reports success while the function reads nothing. The data source
# knows which mode the vault is in, so detect it instead of asking the operator.
resource "azurerm_role_assignment" "kv_secrets_user" {
  count                = local.kv_enabled && data.azurerm_key_vault.shared[0].enable_rbac_authorization ? 1 : 0
  scope                = data.azurerm_key_vault.shared[0].id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.kv[0].principal_id
}

resource "azurerm_key_vault_access_policy" "kv_get" {
  count        = local.kv_enabled && !data.azurerm_key_vault.shared[0].enable_rbac_authorization ? 1 : 0
  key_vault_id = data.azurerm_key_vault.shared[0].id
  tenant_id    = azurerm_user_assigned_identity.kv[0].tenant_id
  object_id    = azurerm_user_assigned_identity.kv[0].principal_id

  # Get only. The app resolves references; it never writes or lists.
  secret_permissions = ["Get"]
}

resource "azurerm_linux_function_app" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  service_plan_id     = azurerm_service_plan.this.id

  storage_account_name       = var.storage_account_name
  storage_account_access_key = var.storage_account_access_key

  https_only = true

  # Key Vault references in app_settings — the bearer secret, and any credential a
  # workload takes — are resolved by the PLATFORM before the worker starts, so the
  # handler needs no SDK and the value never enters Terraform state. That resolution
  # runs as an identity, and if the identity cannot read the vault the setting
  # silently stays the literal @Microsoft.KeyVault(...) string and the handler sees
  # nonsense rather than a secret.
  #
  # System-assigned is kept unconditionally: an operator may already have granted it
  # directly, and removing it would break that. The user-assigned one is added when a
  # vault is wired up, because only a user-assigned identity can be granted BEFORE
  # the app exists.
  identity {
    type         = local.kv_enabled ? "SystemAssigned, UserAssigned" : "SystemAssigned"
    identity_ids = local.kv_enabled ? [azurerm_user_assigned_identity.kv[0].id] : []
  }

  # Which identity resolves EVERY reference in app_settings. Pointing it at the
  # granted user-assigned one is what makes a db_grant credential in the same vault
  # resolve without a manual per-function grant as well.
  key_vault_reference_identity_id = local.kv_enabled ? azurerm_user_assigned_identity.kv[0].id : null

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

  # The grant must land before the app starts resolving references, and neither
  # resource is referenced from here, so the dependency has to be explicit.
  depends_on = [
    azurerm_role_assignment.kv_secrets_user,
    azurerm_key_vault_access_policy.kv_get,
  ]

  lifecycle {
    precondition {
      condition     = (local.kv_enabled != (trimspace(var.shared_secret) != ""))
      error_message = "Set exactly one of shared_secret_kv_uri (the supported path) or shared_secret (a literal value, no vault). Neither leaves the function with no bearer secret, and it would fail auth closed on every request."
    }
    precondition {
      condition     = !local.kv_enabled || trimspace(var.key_vault_name) != ""
      error_message = "shared_secret_kv_uri needs key_vault_name too: without it the module cannot grant this app's identity read on the vault, and the reference would never resolve."
    }
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
  description = "System-assigned identity. Only needed for a vault this module was not told about — the one in key_vault_name is granted automatically, to the user-assigned identity below."
}

output "kv_identity_principal_id" {
  value       = local.kv_enabled ? azurerm_user_assigned_identity.kv[0].principal_id : ""
  description = "User-assigned identity that resolves every Key Vault reference in app_settings; already granted `get` on key_vault_name"
}
