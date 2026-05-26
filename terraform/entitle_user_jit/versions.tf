terraform {
  required_version = ">= 1.5.0"

  required_providers {
    entitle = {
      # Provider source per BeyondTrust's published registry name.
      # See https://docs.beyondtrust.com/entitle/docs/entitle-terraform-provider
      source  = "beyondtrust/entitle"
      version = "~> 1.0"
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
