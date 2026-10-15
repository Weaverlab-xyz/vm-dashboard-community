output "read_role_definition_id" {
  description = "POV cloud read: assign to the federated app when Workload Credentials serves jobs."
  value       = azurerm_role_definition.read.role_definition_resource_id
}

output "provision_role_definition_id" {
  description = "POV cloud provision: assign to the federated app when it is the only credential, or to the Workload Credentials target app."
  value       = azurerm_role_definition.provision.role_definition_resource_id
}
