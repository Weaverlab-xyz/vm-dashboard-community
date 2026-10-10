terraform {
  required_version = ">= 1.5.0"

  required_providers {
    entitle = {
      # Published provider source on the Terraform Registry.
      # https://registry.terraform.io/providers/entitleio/entitle/latest
      source = "entitleio/entitle"
      # 3.2.2 is the release this module was validated against; entitle_resource_synced,
      # which it is built on, is not in every 3.x.
      version = ">= 3.2.2, < 4.0.0"
    }
  }
}

provider "entitle" {
  # The provider reads ENTITLE_API_KEY from the env var when set; the
  # var.entitle_api_key fallback below lets `terraform apply -var ...`
  # work in CI / wrapper-script flows that don't want to leak the key
  # into the shell environment.
  api_key = var.entitle_api_key
}
