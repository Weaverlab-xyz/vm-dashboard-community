output "read_role" {
  description = "projects/<project>/roles/<prefix>Read — bound to the service account."
  value       = google_project_iam_custom_role.read.id
}

output "provision_role" {
  description = "projects/<project>/roles/<prefix>Provision — the role Entitle grants just in time (or bound standing with bind_provision)."
  value       = google_project_iam_custom_role.provision.id
}
