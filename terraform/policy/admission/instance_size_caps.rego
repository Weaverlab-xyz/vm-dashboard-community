# Action-level admission control (pre-deploy gate).
#
# Deny a deploy that requests a blocked instance type / size / class. The blocked
# list is set from Settings (`admission_denied_instance_types`) and injected as
# `input.limits.denied_instance_types`; empty ⇒ inert. `input.request.instance_type`
# is the normalized size the dashboard passes for every path (EC2 instance_type /
# Azure vm_size / GCE machine_type / DB instance_class or sku / node_instance_type).
#
# To cap by size *class* instead of exact match, replace the exact-match check
# below with a prefix/regex rule — policies are versioned in-repo and edited like
# code.
package admission.instance_size_caps

import rego.v1

denied_set := {t | some t in input.limits.denied_instance_types}

deny contains msg if {
	it := input.request.instance_type
	it != ""
	denied_set[it]
	msg := sprintf("instance type %q is blocked by policy", [it])
}
