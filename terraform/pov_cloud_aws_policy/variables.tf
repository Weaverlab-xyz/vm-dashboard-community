variable "aws_region" {
  description = "Region for the provider. IAM is global; this only satisfies the provider."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Policy names are <prefix>-read and <prefix>-provision."
  type        = string
  default     = "pov-cloud"
}

variable "tag_conditions" {
  description = "Condition mutating and destructive EC2 actions on the povManagedBy=vm-dashboard tag every POV resource carries. Set false only to rule the conditions out while diagnosing an UnauthorizedOperation (decode it with `aws sts decode-authorization-message`)."
  type        = bool
  default     = true
}

variable "allow_pricing" {
  description = "Allow pricing:GetProducts for /pov/cloud's optional list-price estimate."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags for the two policies."
  type        = map(string)
  default     = {}
}
