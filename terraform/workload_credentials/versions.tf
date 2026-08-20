terraform {
  # 1.11, not the 1.5 the other modules use. The BeyondTrust provider models the Azure
  # integration's client secret as a write-only attribute, which is a 1.11 feature — on
  # an older CLI the provider fails to load rather than degrading.
  required_version = ">= 1.11.0"

  required_providers {
    beyondtrust = {
      # https://registry.terraform.io/providers/BeyondTrust/beyondtrust/latest
      # Listed under "beyondtrust", which is easy to miss when looking for
      # "workload credentials".
      source  = "beyondtrust/beyondtrust"
      version = "~> 1.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    azuread = {
      # 3.0, not 2.47: azuread_application_owner takes `application_id` as a Terraform
      # resource ID (`/applications/{object-id}`), and that is the shape verified against
      # the v3 documentation. Pinning to a version whose argument shape has not been
      # checked is how a plan-time error becomes a surprise.
      source  = "hashicorp/azuread"
      version = ">= 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

provider "beyondtrust" {
  # Reads BEYONDTRUST_ACCESS_TOKEN and BEYONDTRUST_SITE_ID from the environment when the
  # variables are left empty, matching the provider's own documented default and keeping
  # the PAT out of both the shell history and the state file.
  #
  # api_version is deliberately NOT set: the provider's own default is the value to use,
  # and pinning it here would silently freeze this module on an old response shape.
  access_token = var.beyondtrust_access_token != "" ? var.beyondtrust_access_token : null
  site_id      = var.beyondtrust_site_id != "" ? var.beyondtrust_site_id : null
  api_url      = var.beyondtrust_api_url
}
