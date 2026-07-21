variable "ami_id" {
  description = "The AMI ID to deploy"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.medium"
}

variable "key_name" {
  description = "Name of the EC2 key pair for SSH access"
  type        = string
}

variable "subnet_id" {
  description = "VPC subnet ID to launch the instance in"
  type        = string
}

variable "security_group_ids" {
  description = "List of security group IDs to attach"
  type        = list(string)
}

variable "instance_name" {
  description = "Name tag for the EC2 instance"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-2"
}

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

resource "aws_iam_role" "ssm_role" {
  name = "vm-cli-ssm-role-${var.instance_name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = {
    "managed-by" = "vm-dashboard"
  }
}

resource "aws_iam_role_policy_attachment" "ssm_policy" {
  role       = aws_iam_role.ssm_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ssm_profile" {
  name = "vm-cli-ssm-profile-${var.instance_name}"
  role = aws_iam_role.ssm_role.name
}

resource "aws_instance" "vm" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  key_name               = var.key_name
  subnet_id              = var.subnet_id
  vpc_security_group_ids = var.security_group_ids
  iam_instance_profile   = aws_iam_instance_profile.ssm_profile.name

  # Own the root disk explicitly so a rollback reliably reclaims it. Some custom
  # Packer AMIs ship delete_on_termination=false on the root device, which
  # otherwise leaves an untagged "available" volume behind after a destroy. The
  # tags make any future orphan traceable back to the dashboard.
  root_block_device {
    delete_on_termination = true
    tags = {
      Name         = "${var.instance_name}-root"
      "managed-by" = "vm-dashboard"
    }
  }

  tags = {
    Name      = var.instance_name
    "managed-by" = "vm-dashboard"
  }
}

output "instance_id" {
  value = aws_instance.vm.id
}

output "public_ip" {
  value = aws_instance.vm.public_ip
}

output "private_ip" {
  value = aws_instance.vm.private_ip
}

output "instance_state" {
  value = aws_instance.vm.instance_state
}
