terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "aws" {
  region = var.region
}

# ── Variables ────────────────────────────────────────────────────────────────

variable "region" {
  type        = string
  description = "AWS region for the EKS cluster"
}

variable "cluster_name" {
  type        = string
  description = "EKS cluster name (unique per region)"
}

variable "k8s_version" {
  type        = string
  default     = "1.35"
  description = "Kubernetes minor version for the control plane + node group. Track a current EKS standard-support release (mid-2026 EKS supports 1.33–1.36); bump as EKS adds versions and older ones reach end of standard support."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Subnets for the control plane + nodes — the two private k8s subnets in distinct AZs the sandbox emits (k8s_subnet_a_id / k8s_subnet_b_id). EKS requires at least two AZs."
}

variable "node_instance_type" {
  type        = string
  default     = "t3.small"
  description = "EC2 instance type for the managed node group"
}

variable "node_desired" {
  type        = number
  default     = 2
  description = "Desired node count"
}

variable "node_min" {
  type        = number
  default     = 1
  description = "Minimum node count"
}

variable "node_max" {
  type        = number
  default     = 3
  description = "Maximum node count"
}

# Endpoint access. MVP default: public endpoint (optionally CIDR-restricted) +
# private access, so the dashboard host (and its transient kubectl/helm runner)
# can reach the API and the existing Phase 2-4 flows work unchanged. A
# fully-private endpoint reached only through an in-cluster PRA Jumpoint pod is
# the §1.3 follow-on in docs/saas-kubernetes-management-plan.md — deliberately
# out of scope here.
variable "endpoint_public_access" {
  type        = bool
  default     = true
  description = "Expose the public API server endpoint (restrict with public_access_cidrs)"
}

variable "public_access_cidrs" {
  type        = list(string)
  default     = ["0.0.0.0/0"]
  description = "CIDRs allowed to reach the public endpoint (tighten to the operator's egress IP in real use)"
}

variable "endpoint_private_access" {
  type        = bool
  default     = true
  description = "Enable the private (in-VPC) API server endpoint"
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Resource tags (managed-by, cluster id)"
}

# Optional peer security-group reachability — lets the in-cluster workload (e.g.
# the Entitle agent) reach the lab's private VMs / databases it manages. Blank =
# skip. Node instances carry the EKS-managed cluster security group, which is the
# source of the ingress rules added on these SGs below.
variable "db_security_group_id" {
  type        = string
  default     = ""
  description = "SG id of the managed databases; blank to skip. Opens ingress on db_ports from the cluster SG."
}

variable "vm_security_group_id" {
  type        = string
  default     = ""
  description = "SG id of the lab VMs; blank to skip. Opens ingress on vm_ports from the cluster SG."
}

variable "db_ports" {
  type        = list(number)
  default     = [5432, 3306, 1433]
  description = "DB engine ports opened from the cluster SG to db_security_group_id (Postgres / MySQL / SQL Server)."
}

variable "vm_ports" {
  type        = list(number)
  default     = [22]
  description = "Ports opened from the cluster SG to vm_security_group_id (SSH by default)."
}

# ── IAM — cluster + node roles ───────────────────────────────────────────────
# The cluster role lets the EKS control plane manage AWS resources; the node
# role lets nodes join, pull from ECR, and run the VPC CNI. Both must exist with
# their managed policies attached BEFORE the cluster/node group are created
# (depends_on below), or EKS rejects the create.

resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "eks.amazonaws.com" }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster_eks" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role" "node" {
  name = "${var.cluster_name}-node-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "node_ecr" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# ── EKS cluster + managed node group ─────────────────────────────────────────

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.k8s_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_public_access  = var.endpoint_public_access
    endpoint_private_access = var.endpoint_private_access
    # Only meaningful when public access is on; null = leave unset otherwise.
    public_access_cidrs = var.endpoint_public_access ? var.public_access_cidrs : null
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy_attachment.cluster_eks]
}

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.cluster_name}-ng"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids
  instance_types  = [var.node_instance_type]

  scaling_config {
    desired_size = var.node_desired
    min_size     = var.node_min
    max_size     = var.node_max
  }

  tags = var.tags

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]
}

# ── Management-plane reachability (optional) ─────────────────────────────────
# Open the private VM / DB security groups to the cluster's node instances (they
# carry the EKS-managed cluster security group) so an in-cluster agent can reach
# the resources it manages. Rules live in THIS cluster's state (each keyed by its
# own cluster SG as source) → torn down with the cluster; no orphans, and two
# clusters targeting the same DB/VM SG don't collide. Created only when the peer
# SG id is provided.
locals {
  cluster_sg_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

resource "aws_security_group_rule" "db_from_cluster" {
  for_each                 = var.db_security_group_id == "" ? toset([]) : toset([for p in var.db_ports : tostring(p)])
  type                     = "ingress"
  security_group_id        = var.db_security_group_id
  source_security_group_id = local.cluster_sg_id
  protocol                 = "tcp"
  from_port                = tonumber(each.value)
  to_port                  = tonumber(each.value)
  description              = "EKS ${var.cluster_name} nodes → managed DB"
}

resource "aws_security_group_rule" "vm_from_cluster" {
  for_each                 = var.vm_security_group_id == "" ? toset([]) : toset([for p in var.vm_ports : tostring(p)])
  type                     = "ingress"
  security_group_id        = var.vm_security_group_id
  source_security_group_id = local.cluster_sg_id
  protocol                 = "tcp"
  from_port                = tonumber(each.value)
  to_port                  = tonumber(each.value)
  description              = "EKS ${var.cluster_name} nodes → lab VM"
}

# ── Outputs ──────────────────────────────────────────────────────────────────
# The service (k8s_service.run_provision_apply) assembles an exec-based kubeconfig
# from these — keeping AWS creds + region out of Terraform state.

output "cluster_name" {
  value       = aws_eks_cluster.this.name
  description = "EKS cluster name (used by `aws eks get-token`)"
}

output "endpoint" {
  value       = aws_eks_cluster.this.endpoint
  description = "API server URL (kubeconfig server / api_server)"
}

output "ca_certificate" {
  value       = aws_eks_cluster.this.certificate_authority[0].data
  description = "Cluster CA, base64 PEM (kubeconfig certificate-authority-data)"
}

output "cluster_arn" {
  value       = aws_eks_cluster.this.arn
  description = "EKS cluster ARN"
}

output "cluster_security_group_id" {
  value       = local.cluster_sg_id
  description = "EKS-managed cluster security group (carried by node instances; source of the DB/VM ingress rules)"
}
