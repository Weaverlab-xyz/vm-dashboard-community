# Action-level admission control (pre-action gate) — change-freeze window.
#
# Deny gated actions on frozen weekdays. Unlike the other two policies this one
# deliberately applies to teardowns as well: "no changes on a Sunday" means no
# destroys either, and a freeze that only stopped deploys would be half a freeze. The operator sets the frozen days from Settings
# (`admission_prod_window`, e.g. `sat,sun` or `fri,sat,sun`), injected as
# `input.limits.prod_window`; empty ⇒ inert. The dashboard computes the current
# UTC weekday in Python and passes it as `input.now.weekday` (lowercase `mon`..`sun`),
# so this policy stays free of timezone/date math. Extend to hour ranges via
# `input.now.hour` if you need intra-day windows.
package admission.prod_window

import rego.v1

frozen := {d | some d in input.limits.prod_window}

deny contains msg if {
	count(input.limits.prod_window) > 0
	frozen[input.now.weekday]
	msg := sprintf("%s is frozen on %s (change-freeze window %v)", [input.action, input.now.weekday, input.limits.prod_window])
}
