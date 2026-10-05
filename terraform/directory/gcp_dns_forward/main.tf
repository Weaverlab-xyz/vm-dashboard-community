# GCP "directory link": a Cloud DNS private forwarding zone that sends an on-prem Active
# Directory domain's queries to its domain controllers over a VPN or Interconnect,
# driven by web_dashboard/services/directory_service.py.
#
# This is the cheap way to let GCE Windows servers join an on-prem domain: GCP has no
# AD Connector, and Managed Microsoft AD with a trust costs hundreds a month. With this
# zone the VPC resolves the domain and finds its DCs; the join itself is run from the
# on-prem remote agent over WinRM. Nothing secret is in state.
#
# Cost: one managed zone (about $0.20/month) plus per-query charges.
# Queries leave the VPC from 35.199.192.0/19, so the on-prem firewall and the VPN's
# routes must allow that range to reach the DCs on 53.

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
  description = "The on-prem AD domain, e.g. corp.example.com"
}

variable "dns_ips" {
  type        = list(string)
  description = "On-prem DNS servers (normally the domain controllers) reachable over the VPN"
}

variable "networks" {
  type        = list(string)
  description = "VPC network self-links or URLs that should resolve the domain"
}

variable "directory_row_id" {
  type        = string
  description = "The dashboard row id, used to name the zone uniquely"
}

resource "google_dns_managed_zone" "link" {
  name        = "ad-link-${substr(replace(var.directory_row_id, "-", ""), 0, 20)}"
  dns_name    = "${trimsuffix(var.domain_name, ".")}."
  description = "Forwards ${var.domain_name} to on-prem domain controllers (vm-dashboard)"
  visibility  = "private"

  private_visibility_config {
    dynamic "networks" {
      for_each = var.networks
      content {
        network_url = networks.value
      }
    }
  }

  forwarding_config {
    dynamic "target_name_servers" {
      for_each = var.dns_ips
      content {
        ipv4_address = target_name_servers.value
        # Private routing: the query goes over the VPN, never to the internet.
        forwarding_path = "private"
      }
    }
  }

  labels = {
    managed-by = "vm-dashboard"
  }
}

output "zone_name" {
  value = google_dns_managed_zone.link.name
}

output "dns_ips" {
  value = var.dns_ips
}
