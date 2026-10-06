# AWS Managed Microsoft AD — a directory for Windows servers to join, driven by
# web_dashboard/services/directory_service.py.
#
# ── The admin password in state ──────────────────────────────────────────────
#
# `password` is required at create and Terraform writes it to state. The service resets
# the directory's `Admin` password through the Directory Service API as soon as this
# apply returns, and stores THAT one in Password Safe / a secret manager, so the value in
# state is dead by the time anyone could read it. `ignore_changes` stops the next plan
# from trying to set it back.
#
# ── Cost and teardown ────────────────────────────────────────────────────────
#
# Two domain controllers bill around the clock (Standard ≈ $150–200/month, Enterprise
# several times that). The service refuses to destroy a directory that dashboard VMs
# are still joined to. A destroy takes 10–20 minutes; a create 20–45.

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

variable "region" {
  type        = string
  description = "AWS region that owns the directory"
}

variable "domain_name" {
  type        = string
  description = "Fully qualified domain name, e.g. corp.example.com"
}

variable "short_name" {
  type        = string
  default     = ""
  description = "NetBIOS name; blank lets AWS derive it from the first label"
}

variable "edition" {
  type        = string
  default     = "Standard"
  description = "Standard or Enterprise"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type        = list(string)
  description = "Exactly two subnets in different Availability Zones"
}

variable "admin_password" {
  type      = string
  sensitive = true
}

variable "directory_row_id" {
  type        = string
  description = "The dashboard's ManagedDirectory id, tagged on the directory"
}

resource "aws_directory_service_directory" "ad" {
  name       = var.domain_name
  short_name = var.short_name != "" ? var.short_name : null
  password   = var.admin_password
  type       = "MicrosoftAD"
  edition    = var.edition

  vpc_settings {
    vpc_id     = var.vpc_id
    subnet_ids = var.subnet_ids
  }

  tags = {
    "managed-by"             = "vm-dashboard"
    "dashboard-directory-id" = var.directory_row_id
  }

  lifecycle {
    ignore_changes = [password]
  }
}

output "directory_id" {
  value = aws_directory_service_directory.ad.id
}

output "dns_ip_addresses" {
  value = tolist(aws_directory_service_directory.ad.dns_ip_addresses)
}

output "security_group_id" {
  value = aws_directory_service_directory.ad.security_group_id
}
