# Action-level admission control (pre-deploy gate).
#
# Deny a deploy whose target region/location/zone is not in the operator's
# allowed list. The list is set from Settings (Action guardrails →
# `admission_allowed_regions`) and injected as `input.limits.allowed_regions`;
# when it's empty this policy is inert (no restriction). `input.request.region`
# is the normalized target the dashboard passes for every cloud (AWS region /
# Azure location / GCP zone / DB region / cluster region).
#
# The package's last segment (`allowed_regions`) is the rule_id in denial output.
package admission.allowed_regions

import rego.v1

allowed_set := {r | some r in input.limits.allowed_regions}

deny contains msg if {
	count(input.limits.allowed_regions) > 0
	region := input.request.region
	region != ""
	not allowed_set[region]
	msg := sprintf("region %q is not in the allowed list %v", [region, input.limits.allowed_regions])
}
