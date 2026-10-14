output "read_policy_arn" {
  description = "Request-path policy. Attach to the federated role when Workload Credentials serves jobs (dashboard_federation/aws policy_arns)."
  value       = aws_iam_policy.read.arn
}

output "provision_policy_arn" {
  description = "Read plus build/power/teardown. Attach to the federated role when it is the only credential, or to the Workload Credentials target role (workload_credentials aws_dashboard_policy_arn)."
  value       = aws_iam_policy.provision.arn
}
