# Least-privilege GCP custom roles for a POV instance's GCP cloud provider
# (web_dashboard/services/pov_cloud_gcp.py). The GCP twin of pov_cloud_aws_policy and
# pov_cloud_azure_role:
#
#   povCloudRead       what the request path needs: the overview, the environment list,
#                      Test connection, image lookup and operation polling.
#   povCloudProvision  read, plus what build, power and teardown need: the POV's VPC
#                      network, subnet, firewall rules and VMs.
#
# GCP has no Workload Credentials tier, so the privileged half comes from Entitle:
#
#   SPIFFE identity alone      bind provision to the service account (bind_provision).
#   SPIFFE + Entitle           bind ONLY read. Entitle binds provision onto the same
#                              service account for the length of a grant, on behalf of
#                              the Entitle user the account is assigned to. The role must
#                              exist on the project for Entitle's GCP integration to
#                              offer it.
#
# The provision role's additions must stay equal to PROVISION_PERMISSIONS in
# pov_cloud_gcp.py — the dashboard waits on exactly that set after a grant, and
# tests/test_pov_cloud_gcp_entitle.py pins the two together.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
}

locals {
  read_permissions = [
    "resourcemanager.projects.get",
    "compute.projects.get",
    "compute.regions.get",
    "compute.zones.get",
    "compute.zones.list",
    "compute.networks.get",
    "compute.networks.list",
    "compute.subnetworks.get",
    "compute.subnetworks.list",
    "compute.firewalls.get",
    "compute.firewalls.list",
    "compute.instances.get",
    "compute.instances.list",
    "compute.disks.get",
    "compute.disks.list",
    "compute.images.get",
    "compute.images.getFromFamily",
    "compute.globalOperations.get",
    "compute.regionOperations.get",
    "compute.zoneOperations.get",
  ]

  provision_additions = [
    "compute.networks.create",
    "compute.networks.delete",
    "compute.networks.updatePolicy",
    "compute.subnetworks.create",
    "compute.subnetworks.delete",
    "compute.subnetworks.use",
    "compute.subnetworks.useExternalIp",
    "compute.firewalls.create",
    "compute.firewalls.delete",
    "compute.instances.create",
    "compute.instances.delete",
    "compute.instances.start",
    "compute.instances.stop",
    "compute.instances.setMetadata",
    "compute.instances.setLabels",
    "compute.instances.setTags",
    "compute.disks.create",
    "compute.images.useReadOnly",
  ]
}

resource "google_project_iam_custom_role" "read" {
  project     = var.project_id
  role_id     = "${var.role_id_prefix}Read"
  title       = "${var.title_prefix} read"
  description = "vm-dashboard POV cloud provider: read-only (request path)."
  permissions = local.read_permissions
}

resource "google_project_iam_custom_role" "provision" {
  project     = var.project_id
  role_id     = "${var.role_id_prefix}Provision"
  title       = "${var.title_prefix} provision"
  description = "vm-dashboard POV cloud provider: build, power and tear down POV environments (jobs)."
  permissions = concat(local.read_permissions, local.provision_additions)
}

resource "google_project_iam_member" "read" {
  count   = var.service_account_email == "" ? 0 : 1
  project = var.project_id
  role    = google_project_iam_custom_role.read.id
  member  = "serviceAccount:${var.service_account_email}"
}

# Standing write privilege. Only for the SPIFFE-identity-alone setup; with Entitle,
# leave it false — a standing binding makes every Entitle request moot, and Test
# connection says so.
resource "google_project_iam_member" "provision" {
  count   = var.service_account_email != "" && var.bind_provision ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.provision.id
  member  = "serviceAccount:${var.service_account_email}"
}
