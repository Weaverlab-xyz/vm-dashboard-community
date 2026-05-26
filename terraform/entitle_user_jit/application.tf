# The "VM Dashboard" virtual application — Entitle's grouping construct
# that surfaces in the end-user catalog as one entity. Bundling all the
# dashboard-* resources under one application keeps requesters from
# wading through hundreds of unrelated Entitle entries.
#
# `entitle_bundle` is the documented mechanism for "show me one
# catalog entry that grants N underlying resources" — used here as the
# wrapper. The actual approval semantics still come from each
# resource's workflow_id; the bundle just provides the user-facing
# affordance.

resource "entitle_bundle" "vm_dashboard" {
  name        = var.application_name
  description = var.application_description

  # Surface every provisioned dashboard-* resource under this bundle.
  # Operators who later want to restrict the catalog to a subset can
  # narrow the `for` source — but every dashboard-* group should be
  # individually requestable through Entitle even when bundled, so the
  # default is "all".
  resource_ids = [
    for k, _ in var.groups : entitle_resource.dashboard_group[k].id
  ]
}
