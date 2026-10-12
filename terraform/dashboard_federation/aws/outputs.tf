output "dashboard_settings" {
  description = "What to set in the dashboard: tick AWS under its SPIFFE identity, paste the role ARN, and clear the stored access key."
  value = {
    aws_federation_role_arn  = aws_iam_role.dashboard.arn
    dashboard_spiffe_aud_aws = true
  }
}

output "subject" {
  value = local.subject
}
