terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3.0"
}

# Auth comes from the env the dashboard injects (terraform_provider_env.gcp_env):
# GOOGLE_CREDENTIALS (inline SA JSON) + GOOGLE_PROJECT. The provisioning SA needs
# roles/container.admin so the minted OAuth token (gcp_service.gke_get_token)
# passes GKE's IAM → RBAC for cluster access.
provider "google" {
  project = var.project
  region  = var.region
}

# ── Variables ────────────────────────────────────────────────────────────────

variable "project" {
  type        = string
  description = "GCP project id"
}

variable "region" {
  type        = string
  description = "GCP region (e.g. us-central1)"
}

variable "zone" {
  type        = string
  default     = ""
  description = "Zone for a zonal cluster (cheaper than regional). Empty = '<region>-a'."
}

variable "cluster_name" {
  type        = string
  description = "GKE cluster name (unique per project/location)"
}

variable "k8s_version" {
  type        = string
  default     = ""
  description = "Minimum master version. Empty = GKE default for the channel."
}

variable "machine_type" {
  type        = string
  default     = "e2-standard-2"
  description = "Machine type for the node pool. e2-standard-2 (2 vCPU / 8 GB) fits the 3-replica Entitle agent + Datadog; e2-small (2 GB) cannot."
}

variable "node_count" {
  type        = number
  default     = 3
  description = "Node count (per the cluster's single zone). 3 lets the Entitle agent's 3 anti-affinity replicas each land on a node."
}

variable "disk_size_gb" {
  type        = number
  default     = 50
  description = "Node boot disk size (GB)."
}

variable "disk_type" {
  type        = string
  default     = "pd-standard"
  description = "Node boot disk type. pd-standard draws from a separate quota than SSD (pd-balanced/pd-ssd), so nodes don't consume the project's SSD_TOTAL_GB quota."
}

variable "subnet_cidr" {
  type        = string
  default     = "10.98.0.0/22"
  description = "Primary node subnet CIDR"
}

variable "pods_cidr" {
  type        = string
  default     = "10.100.0.0/16"
  description = "Secondary range for pods (VPC-native)"
}

variable "services_cidr" {
  type        = string
  default     = "10.101.0.0/20"
  description = "Secondary range for services (VPC-native)"
}

variable "master_cidr" {
  type        = string
  default     = "172.16.8.0/28"
  description = "RFC-1918 /28 for the private control-plane endpoint"
}

# Public API endpoint restricted to these CIDRs. Empty = open to all.
variable "authorized_cidrs" {
  type        = list(string)
  default     = []
  description = "CIDRs allowed to reach the public API endpoint (empty = open to all)"
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Resource labels (managed-by, cluster id) — GCP label charset"
}

locals {
  location = var.zone != "" ? var.zone : "${var.region}-a"
}

# ── Networking (self-contained VPC + subnet; egress via Cloud NAT) ────────────

resource "google_compute_network" "vpc" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  name          = "${var.cluster_name}-subnet"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = var.subnet_cidr

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }
}

# Cloud NAT gives the private nodes outbound internet (so the Entitle agent can
# reach its SaaS) without assigning them public IPs.
resource "google_compute_router" "router" {
  name    = "${var.cluster_name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

# Reserve a static egress IP so the cluster's outbound address is stable and
# knowable — the dashboard whitelists it in the Rancher node firewall (a /32) so
# the imported cluster's cattle-cluster-agent can dial out. AUTO_ONLY would hand
# out ephemeral, possibly-multiple IPs that can rotate and silently break the rule.
resource "google_compute_address" "nat" {
  name   = "${var.cluster_name}-nat-ip"
  region = var.region
}

resource "google_compute_router_nat" "nat" {
  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.nat.self_link]
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# ── GKE cluster + node pool ───────────────────────────────────────────────────

resource "google_container_cluster" "this" {
  name     = var.cluster_name
  location = local.location

  # Manage the node pool separately (remove the default one).
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  min_master_version = var.k8s_version != "" ? var.k8s_version : null

  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.subnet.id

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Private nodes (egress via Cloud NAT), public control-plane endpoint.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = var.master_cidr
  }

  # Restrict the public endpoint only when authorized_cidrs is non-empty.
  dynamic "master_authorized_networks_config" {
    for_each = length(var.authorized_cidrs) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.authorized_cidrs
        content {
          cidr_block = cidr_blocks.value
        }
      }
    }
  }

  resource_labels = var.tags
}

resource "google_container_node_pool" "this" {
  name       = "${var.cluster_name}-ng"
  location   = local.location
  cluster    = google_container_cluster.this.name
  node_count = var.node_count

  node_config {
    machine_type = var.machine_type
    disk_size_gb = var.disk_size_gb
    disk_type    = var.disk_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    labels       = var.tags
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────
# k8s_service._assemble_gke_kubeconfig builds a gke-gcloud-auth-plugin exec
# kubeconfig from these; the transient runner swaps the exec for a server-minted
# OAuth token (_runner_kubeconfig → gcp_service.gke_get_token).

output "cluster_name" {
  value       = google_container_cluster.this.name
  description = "GKE cluster name"
}

output "endpoint" {
  value       = "https://${google_container_cluster.this.endpoint}"
  description = "API server URL (kubeconfig server / api_server)"
}

output "ca_certificate" {
  value       = google_container_cluster.this.master_auth[0].cluster_ca_certificate
  description = "Cluster CA, base64 PEM (kubeconfig certificate-authority-data)"
}

output "nat_public_ip" {
  value       = google_compute_address.nat.address
  description = "Reserved Cloud NAT egress IP (added to the Rancher node firewall as a /32)"
}
