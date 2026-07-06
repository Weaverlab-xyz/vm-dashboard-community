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

# Self-contained network (like AKS/GKE): the module builds its OWN VPC + subnets
# + NAT-instance egress and owns their whole lifecycle — no shared sandbox subnets.
variable "vpc_cidr" {
  type        = string
  default     = "10.97.0.0/16"
  description = "CIDR for the cluster's own VPC. Must NOT overlap the sandbox VPC (10.99.0.0/16); give each concurrent peered cluster a distinct block."
}

variable "nat_instance_type" {
  type        = string
  default     = "t4g.nano"
  description = "Instance type for the NAT instance that gives the private node subnets egress (arm64; ~$3/mo). Cheaper than a managed NAT gateway for a lab."
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
# can reach the API and the existing Phase 2-4 flows work unchanged.
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

# Optional VPC peering back to the sandbox VPC + management-plane reachability.
# Blank sandbox_vpc_id → the cluster is fully isolated (Entitle/PRA still broker
# access, exactly like AKS/GKE). When set, the module peers its VPC to the
# sandbox, adds the return route, and (if the peer SGs are given) opens ingress
# from the cluster security group so an in-cluster agent can reach the VMs/DBs.
variable "sandbox_vpc_id" {
  type        = string
  default     = ""
  description = "Sandbox VPC id to peer with; blank to skip peering."
}

variable "sandbox_vpc_cidr" {
  type        = string
  default     = ""
  description = "Sandbox VPC CIDR (route target on the cluster side of the peering)."
}

variable "sandbox_private_route_table_id" {
  type        = string
  default     = ""
  description = "Sandbox private route table id — gets a return route to this cluster's VPC over the peering."
}

variable "db_security_group_id" {
  type        = string
  default     = ""
  description = "Managed-DB SG id; blank to skip. Opens ingress on db_ports from the cluster SG (requires peering)."
}

variable "vm_security_group_id" {
  type        = string
  default     = ""
  description = "Lab-VM SG id; blank to skip. Opens ingress on vm_ports from the cluster SG (requires peering)."
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

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Resource tags (managed-by, cluster id)"
}

# ── Network — the cluster owns its VPC / subnets / egress ─────────────────────

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = "${var.cluster_name}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.cluster_name}-igw" })
}

# One public subnet (hosts the NAT instance) + two private subnets in distinct
# AZs (EKS control plane + nodes; EKS requires >= 2 AZs).
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, 0)
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = merge(var.tags, { Name = "${var.cluster_name}-public" })
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 1)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags = merge(var.tags, {
    Name                              = "${var.cluster_name}-private-${count.index}"
    "kubernetes.io/role/internal-elb" = "1"
  })
}

# NAT instance in the public subnet: enables ip_forward + MASQUERADE via
# user-data, source/dest check off so it forwards, holds an EIP for a stable
# egress IP (so public_access_cidrs / SaaS allow-lists can pin it).
# Resolve the latest Amazon Linux 2023 arm64 AMI via ec2:DescribeImages (already
# granted for the VM-deploy AMI picker) rather than the SSM public parameter,
# which needs a separate ssm:GetParameter grant the dashboard IAM user lacks.
data "aws_ami" "nat" {
  most_recent = true
  owners      = ["amazon"] # AL2023 is published under the "amazon" owner alias
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-*-arm64"]
  }
}

resource "aws_security_group" "nat" {
  name        = "${var.cluster_name}-nat-sg"
  description = "NAT instance for EKS node egress - ingress from this VPC, egress all"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(var.tags, { Name = "${var.cluster_name}-nat-sg" })
}

resource "aws_instance" "nat" {
  ami                         = data.aws_ami.nat.id
  instance_type               = var.nat_instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.nat.id]
  associate_public_ip_address = true
  source_dest_check           = false

  user_data = <<-NATUD
    #!/bin/bash
    set -euxo pipefail
    # AL2023 is minimal and ships WITHOUT iptables — install it BEFORE any
    # iptables use, or the script dies here (nothing gets masqueraded → the NAT
    # forwards nothing → private nodes have no egress → EKS nodes can't join).
    dnf install -y iptables-services
    sysctl -w net.ipv4.ip_forward=1
    echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-nat.conf
    IFACE="$(ip route | awk '/default/{print $5; exit}')"
    iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE
    iptables -P FORWARD ACCEPT
    service iptables save
    systemctl enable --now iptables
  NATUD

  tags = merge(var.tags, { Name = "${var.cluster_name}-nat" })
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.cluster_name}-nat-eip" })
}

resource "aws_eip_association" "nat" {
  instance_id   = aws_instance.nat.id
  allocation_id = aws_eip.nat.id
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = merge(var.tags, { Name = "${var.cluster_name}-public-rt" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.cluster_name}-private-rt" })
}

# Nodes egress 0.0.0.0/0 through the NAT instance's ENI.
resource "aws_route" "private_nat" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_instance.nat.primary_network_interface_id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ── VPC peering back to the sandbox (optional) ───────────────────────────────
resource "aws_vpc_peering_connection" "sandbox" {
  count       = var.sandbox_vpc_id == "" ? 0 : 1
  vpc_id      = aws_vpc.this.id
  peer_vpc_id = var.sandbox_vpc_id
  auto_accept = true # same account + region
  tags        = merge(var.tags, { Name = "${var.cluster_name}-to-sandbox" })
}

# Cluster side → reach the sandbox VPC over the peering.
resource "aws_route" "to_sandbox" {
  count                     = var.sandbox_vpc_id == "" ? 0 : 1
  route_table_id            = aws_route_table.private.id
  destination_cidr_block    = var.sandbox_vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.sandbox[0].id
}

# Sandbox side (its private RT) → reach this cluster's VPC (return path).
resource "aws_route" "sandbox_return" {
  count                     = (var.sandbox_vpc_id == "" || var.sandbox_private_route_table_id == "") ? 0 : 1
  route_table_id            = var.sandbox_private_route_table_id
  destination_cidr_block    = var.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.sandbox[0].id
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
    subnet_ids              = aws_subnet.private[*].id
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
  subnet_ids      = aws_subnet.private[*].id
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
    aws_route.private_nat,
  ]
}

# ── Management-plane reachability (optional; needs the sandbox peering) ───────
# Open the private VM / DB security groups to the cluster's node instances (they
# carry the EKS-managed cluster security group) so an in-cluster agent can reach
# the resources it manages over the peering. Cross-VPC SG references are valid
# same-region once the VPCs are peered. Rules live in THIS cluster's state →
# torn down with the cluster.
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
  description              = "EKS ${var.cluster_name} nodes → managed DB (over peering)"
  depends_on               = [aws_vpc_peering_connection.sandbox]
}

resource "aws_security_group_rule" "vm_from_cluster" {
  for_each                 = var.vm_security_group_id == "" ? toset([]) : toset([for p in var.vm_ports : tostring(p)])
  type                     = "ingress"
  security_group_id        = var.vm_security_group_id
  source_security_group_id = local.cluster_sg_id
  protocol                 = "tcp"
  from_port                = tonumber(each.value)
  to_port                  = tonumber(each.value)
  description              = "EKS ${var.cluster_name} nodes → lab VM (over peering)"
  depends_on               = [aws_vpc_peering_connection.sandbox]
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

output "vpc_id" {
  value       = aws_vpc.this.id
  description = "The cluster's own VPC id"
}
