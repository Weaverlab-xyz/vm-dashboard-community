# GCP Managed Service for Microsoft Active Directory — a domain for Windows servers to
# join, driven by web_dashboard/services/directory_service.py.
#
# GCP creates the delegated admin account (`admin`, default setupadmin) with no password.
# The service sets one through the Managed Identities resetAdminPassword API after this
# apply and stores it in Password Safe / a secret manager — nothing secret is in state.
#
# Creation takes up to an hour. The domain bills while it exists; the service refuses to
# destroy one that dashboard VMs are still joined to.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "google" {
  project = var.project
}

variable "project" {
  type = string
}

variable "domain_name" {
  type        = string
  description = "Fully qualified domain name, e.g. corp.example.com"
}

variable "locations" {
  type        = list(string)
  description = "Regions that host domain controllers"
}

variable "reserved_ip_range" {
  type        = string
  description = "An unused /24 for the domain controllers, e.g. 10.250.0.0/24"
}

variable "authorized_networks" {
  type        = list(string)
  description = "Full VPC network self-links allowed to reach the domain"
}

variable "admin" {
  type    = string
  default = "setupadmin"
}

resource "google_active_directory_domain" "ad" {
  domain_name         = var.domain_name
  locations           = var.locations
  reserved_ip_range   = var.reserved_ip_range
  authorized_networks = var.authorized_networks
  admin               = var.admin
  deletion_protection = false

  labels = {
    "managed-by" = "vm-dashboard"
  }
}

output "resource_name" {
  value = google_active_directory_domain.ad.name
}

output "fqdn" {
  value = google_active_directory_domain.ad.domain_name
}
