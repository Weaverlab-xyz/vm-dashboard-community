terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3"
}

provider "google" {
  credentials = var.service_account_json != "" ? var.service_account_json : null
  project     = var.project_id
  region      = var.region
}

# ── Variables ──────────────────────────────────────────────────────────────────

variable "service_account_json" {
  description = "Service account JSON key content. Leave empty to use Application Default Credentials."
  type        = string
  sensitive   = true
  default     = ""
}

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region (derived from zone if not set)"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for the instance"
  type        = string
  default     = "us-central1-a"
}

variable "instance_name" {
  description = "Name of the Compute Engine instance"
  type        = string
}

variable "machine_type" {
  description = "GCE machine type (e.g. e2-medium, n2-standard-2)"
  type        = string
  default     = "e2-medium"
}

variable "image_self_link" {
  description = "Full self_link of the source image (e.g. projects/debian-cloud/global/images/debian-12-bookworm-v20240415)"
  type        = string
}

variable "disk_size_gb" {
  description = "Boot disk size in GB"
  type        = number
  default     = 20
}

variable "disk_type" {
  description = "Boot disk type"
  type        = string
  default     = "pd-balanced"
}

variable "network" {
  description = "VPC network name or self_link"
  type        = string
  default     = "default"
}

variable "subnetwork" {
  description = "Subnetwork self_link. Empty string uses automatic subnetwork selection."
  type        = string
  default     = ""
}

variable "create_external_ip" {
  description = "Attach an ephemeral external IP address"
  type        = bool
  default     = false
}

variable "network_tags" {
  description = "Network tags applied to the instance for firewall rule targeting"
  type        = list(string)
  default     = ["http-server", "https-server"]
}

variable "ssh_username" {
  description = "Linux username for SSH access"
  type        = string
  default     = "gcp-user"
}

variable "ssh_public_key" {
  description = "SSH public key inserted into instance metadata"
  type        = string
  sensitive   = true
  default     = ""
}

variable "service_account_email" {
  description = "Service account email to attach to the instance. Empty = compute default SA."
  type        = string
  default     = ""
}

variable "labels" {
  description = "Labels applied to the instance"
  type        = map(string)
  default     = { managed-by = "vm-dashboard" }
}

# ── Locals ─────────────────────────────────────────────────────────────────────

locals {
  subnetwork         = var.subnetwork != "" ? var.subnetwork : null
  service_acct_email = var.service_account_email != "" ? var.service_account_email : null
  ssh_metadata       = var.ssh_public_key != "" ? { ssh-keys = "${var.ssh_username}:${var.ssh_public_key}" } : {}
}

# ── Compute instance ───────────────────────────────────────────────────────────

resource "google_compute_instance" "vm" {
  name         = var.instance_name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = var.network_tags
  labels       = var.labels

  boot_disk {
    initialize_params {
      image = var.image_self_link
      size  = var.disk_size_gb
      type  = var.disk_type
    }
    auto_delete = true
  }

  network_interface {
    network    = var.network
    subnetwork = local.subnetwork

    dynamic "access_config" {
      for_each = var.create_external_ip ? [1] : []
      content {
        # Ephemeral external IP
      }
    }
  }

  metadata = local.ssh_metadata

  dynamic "service_account" {
    for_each = local.service_acct_email != null ? [local.service_acct_email] : []
    content {
      email  = service_account.value
      scopes = ["cloud-platform"]
    }
  }

  lifecycle {
    # Prevent metadata drift from out-of-band OS Config agent key injection
    ignore_changes = [metadata]
  }
}

# ── Outputs ────────────────────────────────────────────────────────────────────

output "instance_name" {
  description = "Name of the created instance"
  value       = google_compute_instance.vm.name
}

output "zone" {
  description = "Zone the instance was deployed in"
  value       = google_compute_instance.vm.zone
}

output "self_link" {
  description = "Full resource self-link"
  value       = google_compute_instance.vm.self_link
}

output "private_ip" {
  description = "Primary internal IP address"
  value       = google_compute_instance.vm.network_interface[0].network_ip
}

output "public_ip" {
  description = "Ephemeral external IP address (empty if create_external_ip = false)"
  value = (
    var.create_external_ip
    ? try(google_compute_instance.vm.network_interface[0].access_config[0].nat_ip, "")
    : ""
  )
}
