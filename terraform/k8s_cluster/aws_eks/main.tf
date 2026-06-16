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
  default     = "1.31"
  description = "Kubernetes minor version for the control plane + node group. Keep on a version in EKS standard support (1.30 drops to extended support in 2026); bump the default as EKS adds versions."
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
