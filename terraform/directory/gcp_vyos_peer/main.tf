# GCP "VyOS site link": the cloud end of a WireGuard tunnel to an on-prem VyOS router,
# driven by web_dashboard/services/directory_service.py (provider gcp_vyos_link).
#
# The cheapest way to put an on-prem Active Directory on a GCP VPC: no Cloud VPN, no
# Managed AD. One small VyOS VM with a static external IP. The on-prem router dials out
# to it (so the on-prem side needs no static IP and no inbound port), and the VPC routes
# the on-prem DC subnets to it. The peer also answers DNS for the AD domain, forwarding
# to the DCs over the tunnel, so a DNS link can target an address INSIDE the VPC.
#
# This module builds only the infrastructure. The VyOS configuration -- the WireGuard
# interface and its private key, the routes, DNS forwarding -- is applied afterwards by
# the GCP Ansible runner over SSH to the internal IP, so no key is ever in instance
# metadata or Terraform state.
#
# Cost: the VM (e2-small about $12/month; e2-micro may fall in the free tier in
# us-central1, us-east1 and us-west1) plus a static external IP (about $3.65/month) and
# egress. A single VM is a single point of failure: use it for labs and POVs.

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

variable "zone" {
  type = string
}

variable "network" {
  type        = string
  description = "VPC network self-link or name the peer and the routes live in"
}

variable "subnetwork" {
  type        = string
  description = "Subnetwork self-link or name for the peer's NIC"
}

variable "image" {
  type        = string
  description = "A VyOS image baked with provisioners/net/vyos-cell.sh"
}

variable "machine_type" {
  type    = string
  default = "e2-small"
}

variable "directory_row_id" {
  type        = string
  description = "The dashboard row id, used to name everything uniquely"
}

variable "onprem_subnets" {
  type        = list(string)
  description = "On-prem CIDRs routed into the tunnel (at least the DC subnets)"
}

variable "cloud_networks" {
  type        = list(string)
  description = "VPC CIDRs that may configure the peer over SSH (the Ansible runner, the Gateway)"
}

variable "wireguard_port" {
  type    = number
  default = 51820
}

variable "wireguard_source_ranges" {
  type        = list(string)
  default     = ["0.0.0.0/0"]
  description = "Where the on-prem router dials from. WireGuard ignores unauthenticated packets, so 0.0.0.0/0 is acceptable; narrow it to the on-prem egress /32 when it is static"
}

locals {
  id     = substr(replace(var.directory_row_id, "-", ""), 0, 20)
  name   = "vyos-link-${local.id}"
  region = join("-", slice(split("-", var.zone), 0, 2))
  tag    = "vyos-link-${local.id}"
}

resource "google_compute_address" "peer" {
  name         = local.name
  region       = local.region
  address_type = "EXTERNAL"
  labels       = { managed-by = "vm-dashboard" }
}

resource "google_compute_instance" "peer" {
  name         = local.name
  zone         = var.zone
  machine_type = var.machine_type
  # A router: it forwards packets whose source and destination are not its own.
  can_ip_forward = true
  tags           = ["vyos-cell", local.tag]
  labels         = { managed-by = "vm-dashboard", purpose = "vyos-site-link" }

  boot_disk {
    initialize_params {
      image = var.image
      size  = 10
    }
  }

  network_interface {
    network    = var.network
    subnetwork = var.subnetwork
    access_config {
      nat_ip = google_compute_address.peer.address
    }
  }
}

resource "google_compute_firewall" "wireguard" {
  name          = "${local.name}-wg"
  network       = var.network
  direction     = "INGRESS"
  source_ranges = var.wireguard_source_ranges
  target_tags   = [local.tag]
  allow {
    protocol = "udp"
    ports    = [tostring(var.wireguard_port)]
  }
}

resource "google_compute_firewall" "configure" {
  name          = "${local.name}-ssh"
  network       = var.network
  direction     = "INGRESS"
  source_ranges = var.cloud_networks
  target_tags   = [local.tag]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Cloud DNS forwards the domain's queries from this range, and the DNS link targets the
# peer's internal address.
resource "google_compute_firewall" "dns" {
  name          = "${local.name}-dns"
  network       = var.network
  direction     = "INGRESS"
  source_ranges = concat(["35.199.192.0/19"], var.cloud_networks)
  target_tags   = [local.tag]
  allow {
    protocol = "udp"
    ports    = ["53"]
  }
  allow {
    protocol = "tcp"
    ports    = ["53"]
  }
}

# The on-prem agent joins servers over WinRM HTTPS, and pings prove the path.
resource "google_compute_firewall" "from_onprem" {
  name          = "${local.name}-onprem"
  network       = var.network
  direction     = "INGRESS"
  source_ranges = var.onprem_subnets
  allow {
    protocol = "tcp"
    ports    = ["5986"]
  }
  allow {
    protocol = "icmp"
  }
}

resource "google_compute_route" "onprem" {
  for_each               = toset(var.onprem_subnets)
  name                   = "${local.name}-${substr(md5(each.value), 0, 8)}"
  network                = var.network
  dest_range             = each.value
  next_hop_instance      = google_compute_instance.peer.self_link
  next_hop_instance_zone = var.zone
  priority               = 900
}

output "public_ip" {
  value = google_compute_address.peer.address
}

output "internal_ip" {
  value = google_compute_instance.peer.network_interface[0].network_ip
}

output "instance_name" {
  value = google_compute_instance.peer.name
}
