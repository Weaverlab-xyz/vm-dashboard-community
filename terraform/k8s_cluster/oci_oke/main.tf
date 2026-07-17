terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3.0"
}

# Credentials arrive as TF_VAR_* from services/terraform_provider_env.oci_env().
provider "oci" {
  tenancy_ocid         = var.tenancy_ocid
  user_ocid            = var.user_ocid
  fingerprint          = var.fingerprint
  private_key          = var.private_key
  private_key_password = var.private_key_passphrase
  region               = var.region
}

# ── Provider credential variables (TF_VAR_*) ─────────────────────────────────
variable "tenancy_ocid" {
  type    = string
  default = ""
}
variable "user_ocid" {
  type    = string
  default = ""
}
variable "fingerprint" {
  type    = string
  default = ""
}
variable "private_key" {
  type      = string
  default   = ""
  sensitive = true
}
variable "private_key_passphrase" {
  type      = string
  default   = ""
  sensitive = true
}
variable "region" {
  type = string
}

# ── Cluster variables ────────────────────────────────────────────────────────
variable "compartment_ocid" {
  type        = string
  description = "Compartment the OKE cluster + node pool land in"
}

variable "cluster_name" {
  type        = string
  description = "OKE cluster name"
}

variable "k8s_version" {
  type        = string
  default     = "v1.31.1"
  description = "OKE Kubernetes version (OKE format, e.g. v1.31.1). Must be a version OKE offers in the region — confirm with `oci ce cluster-options get`."
}

# Self-contained network (like the EKS/AKS/GKE modules): the module builds its
# OWN VCN + subnets + NAT-gateway egress and owns their whole lifecycle.
variable "vcn_cidr" {
  type        = string
  default     = "10.96.0.0/16"
  description = "CIDR for the cluster's own VCN. Must NOT overlap the sandbox VCN (10.98.0.0/16); give each concurrent cluster a distinct block."
}

variable "node_shape" {
  type        = string
  default     = "VM.Standard.A1.Flex"
  description = "Worker node shape. The Always-Free Ampere A1 shape by default."
}

variable "node_ocpus" {
  type        = number
  default     = 2
  description = "OCPUs per node (A1.Flex). Free Ampere budget sustains ~2 OCPU total — 1 node at 2 OCPU stays within it."
}

variable "node_memory_gbs" {
  type        = number
  default     = 12
  description = "Memory (GB) per node (A1.Flex). Free Ampere budget sustains ~12 GB total."
}

variable "node_count" {
  type        = number
  default     = 1
  description = "Worker node count. 1 keeps a single A1 node within the free Ampere allocation (2 OCPU / 12 GB)."
}

variable "node_image_id" {
  type        = string
  default     = ""
  description = "OKE worker node image OCID. Blank → auto-select an Oracle-Linux image matching the k8s version + node shape architecture."
}

variable "ssh_public_key" {
  type        = string
  default     = ""
  description = "Optional SSH public key for worker-node access (debug only; nodes are private)."
}

variable "pods_cidr" {
  type    = string
  default = "10.244.0.0/16"
}

variable "services_cidr" {
  type    = string
  default = "10.96.128.0/20"
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Freeform tags (managed-by, cluster id)"
}

# ── Data sources ─────────────────────────────────────────────────────────────
data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

# Available OKE node images per version/shape. Used to auto-pick a node image
# when node_image_id is blank: prefer an Oracle-Linux image whose name matches
# the node shape's architecture (aarch64 for A1, x86_64 otherwise).
data "oci_containerengine_node_pool_option" "np" {
  node_pool_option_id = "all"
  compartment_id      = var.compartment_ocid
}

locals {
  is_arm    = can(regex("A1", var.node_shape))
  arch_hint = local.is_arm ? "aarch64" : "x86_64"
  # Best-effort: newest Oracle-Linux source matching the arch + k8s minor.
  ver_minor = join(".", slice(split(".", replace(var.k8s_version, "v", "")), 0, 2))
  candidate_images = [
    for s in data.oci_containerengine_node_pool_option.np.sources :
    s.image_id
    if can(regex("Oracle-Linux", s.source_name))
    && can(regex(local.arch_hint, s.source_name))
    && can(regex(local.ver_minor, s.source_name))
  ]
  node_image = var.node_image_id != "" ? var.node_image_id : try(local.candidate_images[0], "")
}

# ── Network ──────────────────────────────────────────────────────────────────
resource "oci_core_vcn" "this" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = [var.vcn_cidr]
  display_name   = "${var.cluster_name}-vcn"
  dns_label      = "oke"
  freeform_tags  = var.tags
}

resource "oci_core_internet_gateway" "this" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.cluster_name}-igw"
  enabled        = true
  freeform_tags  = var.tags
}

resource "oci_core_nat_gateway" "this" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.cluster_name}-nat"
  freeform_tags  = var.tags
}

# Service gateway → all OCI services (OKE nodes reach the control plane + OCIR
# without traversing the internet).
data "oci_core_services" "all" {
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}

resource "oci_core_service_gateway" "this" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.cluster_name}-sgw"
  services {
    service_id = data.oci_core_services.all.services[0]["id"]
  }
  freeform_tags = var.tags
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.cluster_name}-public-rt"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.this.id
  }
  freeform_tags = var.tags
}

resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.cluster_name}-private-rt"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.this.id
  }
  route_rules {
    destination       = data.oci_core_services.all.services[0]["cidr_block"]
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.this.id
  }
  freeform_tags = var.tags
}

# Permissive intra-lab security list (node↔control-plane, intra-VCN, egress all).
resource "oci_core_security_list" "this" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.cluster_name}-sl"
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
  ingress_security_rules {
    source   = var.vcn_cidr
    protocol = "all"
  }
  # Kubernetes API + kubelet from anywhere (public endpoint lab default).
  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "6" # TCP
    tcp_options {
      min = 6443
      max = 6443
    }
  }
  freeform_tags = var.tags
}

resource "oci_core_subnet" "api" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.this.id
  cidr_block                 = cidrsubnet(var.vcn_cidr, 8, 0) # 10.96.0.0/24
  display_name               = "${var.cluster_name}-api"
  dns_label                  = "api"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.this.id]
  prohibit_public_ip_on_vnic = false
  freeform_tags              = var.tags
}

resource "oci_core_subnet" "nodes" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.this.id
  cidr_block                 = cidrsubnet(var.vcn_cidr, 8, 1) # 10.96.1.0/24
  display_name               = "${var.cluster_name}-nodes"
  dns_label                  = "nodes"
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.this.id]
  prohibit_public_ip_on_vnic = true
  freeform_tags              = var.tags
}

resource "oci_core_subnet" "lb" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.this.id
  cidr_block                 = cidrsubnet(var.vcn_cidr, 8, 2) # 10.96.2.0/24
  display_name               = "${var.cluster_name}-lb"
  dns_label                  = "lb"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.this.id]
  prohibit_public_ip_on_vnic = false
  freeform_tags              = var.tags
}

# ── OKE cluster (BASIC = free control plane) + node pool ─────────────────────
resource "oci_containerengine_cluster" "this" {
  compartment_id     = var.compartment_ocid
  name               = var.cluster_name
  vcn_id             = oci_core_vcn.this.id
  kubernetes_version = var.k8s_version
  type               = "BASIC_CLUSTER"

  cluster_pod_network_options {
    cni_type = "FLANNEL_OVERLAY"
  }

  endpoint_config {
    subnet_id            = oci_core_subnet.api.id
    is_public_ip_enabled = true
  }

  options {
    service_lb_subnet_ids = [oci_core_subnet.lb.id]
    add_ons {
      is_kubernetes_dashboard_enabled = false
      is_tiller_enabled               = false
    }
    kubernetes_network_config {
      pods_cidr     = var.pods_cidr
      services_cidr = var.services_cidr
    }
  }

  freeform_tags = var.tags
}

resource "oci_containerengine_node_pool" "this" {
  cluster_id         = oci_containerengine_cluster.this.id
  compartment_id     = var.compartment_ocid
  name               = "${var.cluster_name}-np"
  kubernetes_version = var.k8s_version
  node_shape         = var.node_shape

  node_shape_config {
    ocpus         = var.node_ocpus
    memory_in_gbs = var.node_memory_gbs
  }

  node_config_details {
    size = var.node_count
    placement_configs {
      availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
      subnet_id           = oci_core_subnet.nodes.id
    }
    node_pool_pod_network_option_details {
      cni_type = "FLANNEL_OVERLAY"
    }
  }

  node_source_details {
    source_type = "IMAGE"
    image_id    = local.node_image
  }

  # ssh_public_key is only set when supplied (nodes are private; debug only).
  ssh_public_key = var.ssh_public_key != "" ? var.ssh_public_key : null

  freeform_tags = var.tags
}

# ── Outputs (match the cluster-module contract used by k8s_service) ───────────
# The service assembles an exec kubeconfig (`oci ce cluster generate-token`) from
# cluster_ocid + endpoint + ca_certificate — keeping OCI creds out of TF state.
data "oci_containerengine_cluster_kube_config" "kc" {
  cluster_id = oci_containerengine_cluster.this.id
}

locals {
  # A public OKE cluster exposes its API at endpoints[0].public_endpoint (host:6443).
  public_endpoint = try(oci_containerengine_cluster.this.endpoints[0].public_endpoint, "")
  api_server      = local.public_endpoint != "" ? "https://${local.public_endpoint}" : ""
  # CA is inside the generated kubeconfig content (base64 PEM) — extract it.
  ca_b64 = try(regex("certificate-authority-data:\\s*([A-Za-z0-9+/=]+)", data.oci_containerengine_cluster_kube_config.kc.content)[0], "")
}

output "cluster_name" {
  value       = oci_containerengine_cluster.this.name
  description = "OKE cluster name"
}

output "cluster_ocid" {
  value       = oci_containerengine_cluster.this.id
  description = "OKE cluster OCID (used by `oci ce cluster generate-token`)"
}

output "endpoint" {
  value       = local.api_server
  description = "API server URL (kubeconfig server / api_server)"
}

output "ca_certificate" {
  value       = local.ca_b64
  description = "Cluster CA, base64 PEM (kubeconfig certificate-authority-data)"
}

output "nat_public_ip" {
  value       = oci_core_nat_gateway.this.nat_ip
  description = "Stable NAT egress IP (source address nodes/agents use outbound; added to the Rancher node firewall as a /32)"
}
