# BeyondTrust Support ticket — SRA Terraform provider rejects `tunnel_type = "k8s"`

Ready-to-submit support ticket for the `tunnel_type="k8s"` provider bug. GitHub
Issues are disabled on `BeyondTrust/terraform-provider-sra`; per their
`CONTRIBUTING.md`, bugs go through **BeyondTrust Support** (file under
**Privileged Remote Access**). Technical root cause + a ready-to-file PR-style
write-up live in [`sra-provider-k8s-tunnel-bug.md`](sra-provider-k8s-tunnel-bug.md).

Fill in the bracketed value (your PRA version), then paste into a PRA support case.

---

**Product:** Privileged Remote Access
**Component:** Terraform provider (`beyondtrust/sra`)
**Type:** Bug report
**Severity:** Medium — a documented feature (Kubernetes protocol tunnels) is unusable through Terraform; a REST API workaround exists.

**Subject:** SRA Terraform provider rejects `tunnel_type = "k8s"` on `sra_protocol_tunnel_jump` — schema validator omits "k8s"

## Environment
- Provider: `beyondtrust/sra` **v1.3.0** (latest release)
- Terraform: 1.x
- PRA: _[your appliance version / BeyondTrust Cloud tenant]_

## Summary
The SRA Terraform provider cannot create a Kubernetes protocol-tunnel Jump Item.
Any `sra_protocol_tunnel_jump` with `tunnel_type = "k8s"` fails at
`terraform plan`/`validate`, before the request ever reaches the appliance:

```
Error: Invalid Attribute Value Match
  with sra_protocol_tunnel_jump.k8s,
  on main.tf line N, in resource "sra_protocol_tunnel_jump" "k8s":
  tunnel_type     = "k8s"
Attribute tunnel_type value must be one of: ["tcp" "mssql"], got: "k8s"
```

## Steps to reproduce
```hcl
resource "sra_protocol_tunnel_jump" "k8s" {
  name            = "demo"
  hostname        = "api.example"
  jump_group_id   = <id>
  jumpoint_id     = <id>
  tunnel_type     = "k8s"
  url             = "https://api.example:443"
  ca_certificates = "<PEM>"
}
```
Run `terraform validate` → the error above.

## Expected vs. actual
- **Expected:** the k8s tunnel Jump Item is created. The PRA Configuration API accepts `tunnel_type=k8s`.
- **Actual:** rejected client-side by the provider's schema validator.

## Root cause (for the provider team)
- In `bt/rs/protocol_tunnel_jump.go` (v1.3.0) the `tunnel_type` validator is
  `stringvalidator.OneOf([]string{"tcp", "mssql"}...)` — `"k8s"` is missing.
- The **same file** already handles `ttype == "k8s"` in
  `applyProtocolTunnelDefaultsAndValidate` (it requires `url` and `ca_certificates`),
  and `"k8s"` is in the resource docs, the data-source docs, and
  `examples/resources/sra_protocol_tunnel_jump/resource.tf`.
- The Configuration API accepts it:
  `POST /api/config/v1/jump-item/protocol-tunnel-jump` with `tunnel_type:"k8s"`
  succeeds. Only the provider's client-side `OneOf` blocks it.
- This likely also affects other newer tunnel types — the provider CHANGELOG
  references PostgreSQL/MySQL/Network tunnel types for the 25.2 API; please audit
  the same `OneOf`.

## Requested fix
Add `"k8s"` (and any other backend-supported tunnel types) to the `tunnel_type`
`OneOf` validator on `sra_protocol_tunnel_jump`.

## Workaround in place
We currently create the k8s tunnel Jump Item via the Configuration REST API
directly, so no change is needed on our side once the validator is corrected.

## Business impact
Blocks managing Kubernetes PRA tunnels as code via Terraform.
