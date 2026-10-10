# Approver groups, resolved from the names the operator passes to the Entitle directory
# group ids a workflow needs. The data source's search is a substring match, so the
# result is narrowed to the exact name, and the plan stops unless exactly one group has
# it. A near-miss ("Admins" matching "Admins-Readonly") must not become an approver.

locals {
  approver_names = {
    single     = var.single_approver_group
    two_first  = var.two_approver_group
    two_second = var.two_approver_second_group != "" ? var.two_approver_second_group : var.two_approver_group
  }
}

data "entitle_directory_groups" "approver" {
  for_each = local.approver_names

  filter {
    search = each.value
  }

  lifecycle {
    postcondition {
      condition     = length([for g in self.directory_groups : g if g.name == each.value]) == 1
      error_message = "Expected exactly one Entitle directory group named \"${each.value}\"; check the name in the Entitle UI (Directory > Groups)."
    }
  }
}

locals {
  approver_group_id = {
    for k, name in local.approver_names :
    k => one([for g in data.entitle_directory_groups.approver[k].directory_groups : g.id if g.name == name])
  }
}
