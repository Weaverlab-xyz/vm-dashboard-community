# `beyondtrust/sra` provider: `tunnel_type = "k8s"` is blocked by the schema validator

## Summary

The `sra_protocol_tunnel_jump` resource in the
[`beyondtrust/sra`](https://registry.terraform.io/providers/beyondtrust/sra/latest)
Terraform provider **rejects `tunnel_type = "k8s"`** at plan/validate time, even
though k8s protocol tunnels are documented, shown in the provider's own examples,
and validated by the resource's own `applyProtocolTunnelDefaultsAndValidate`. The
`tunnel_type` attribute's `stringvalidator.OneOf(...)` was never updated to include
`"k8s"`.

Confirmed against the **v1.3.0** tag (latest release as of 2026-06).

```
Error: Invalid Attribute Value Match
  with sra_protocol_tunnel_jump.<name>,
  on main.tf line N, in resource "sra_protocol_tunnel_jump" "<name>":
  tunnel_type     = "k8s"
Attribute tunnel_type value must be one of: ["tcp" "mssql"], got: "k8s"
```

## Evidence (v1.3.0 `bt/rs/protocol_tunnel_jump.go`)

The schema validator allows only `tcp`/`mssql`:

```go
"tunnel_type": schema.StringAttribute{
    Optional: true,
    Computed: true,
    Default:  stringdefault.StaticString("tcp"),
    Validators: []validator.String{
        stringvalidator.OneOf([]string{"tcp", "mssql"}...),   // ← "k8s" missing
    },
},
```

…yet the same file enforces k8s-specific requirements, so the value is clearly
expected to be reachable:

```go
if ttype == "k8s" {
    if plan.URL.IsNull() || plan.URL.ValueString() == "" {
        diags.Append(diag.NewErrorDiagnostic("url is required",
            "You must supply a url when TunnelType is \"k8s\"."))
    }
    if plan.CACertificates.IsNull() || plan.CACertificates.ValueString() == "" {
        diags.Append(diag.NewErrorDiagnostic("ca_certificates is required",
            "You must supply ca_certificates when TunnelType is \"k8s\"."))
    }
}
```

The docs + examples also use it:
- `docs/resources/protocol_tunnel_jump.md` → `tunnel_type = "k8s"`
- `examples/resources/sra_protocol_tunnel_jump/resource.tf` → `tunnel_type = "k8s"`
- `docs/data-sources/protocol_tunnel_jump_list.md` → "`ca_certificates` … required when
  `tunnel_type` is `k8s`. _This field only applies to PRA_"

The PRA **backend** accepts `tunnel_type=k8s` (the provider already serializes it —
`api/models.go: TunnelType string json:"tunnel_type"`). The block is purely the
provider's client-side `OneOf`.

## How the dashboard works around it

We create the k8s protocol-tunnel jump over the **PRA Configuration REST API**
instead of Terraform:

- `pra_api_service.create_k8s_tunnel_jump()` → `POST /api/config/v1/jump-item/protocol-tunnel-jump`
  with `tunnel_type: "k8s"`, `url`, `ca_certificates`.
- `pra_api_service.delete_protocol_tunnel_jump()` → `DELETE …/{id}`.

The optional PRA-Vault token account (`sra_vault_token_account`) is *not* affected
by the bug, so it stays in Terraform, associated to the REST-created jump by id
(`terraform_pra_service._generate_k8s_vault_account_hcl`). Once the provider is
fixed we can move the jump back to Terraform.

---

## Upstream issue draft (ready to file at BeyondTrust/terraform-provider-sra)

> **Title:** `sra_protocol_tunnel_jump`: `tunnel_type` validator rejects `"k8s"` despite docs/examples/validation supporting it
>
> **Provider version:** v1.3.0
> **Terraform version:** 1.x
>
> **What happened**
>
> Applying a `sra_protocol_tunnel_jump` with `tunnel_type = "k8s"` fails at plan:
>
> ```
> Attribute tunnel_type value must be one of: ["tcp" "mssql"], got: "k8s"
> ```
>
> **Why it looks like a bug**
>
> In `bt/rs/protocol_tunnel_jump.go`, the `tunnel_type` attribute validator is
> `stringvalidator.OneOf([]string{"tcp", "mssql"}...)`, but the same file's
> `applyProtocolTunnelDefaultsAndValidate` already handles `ttype == "k8s"`
> (requiring `url` and `ca_certificates`). The k8s tunnel type is also in the
> resource docs, the data-source docs, and `examples/resources/sra_protocol_tunnel_jump/resource.tf`.
> The PRA backend accepts `tunnel_type=k8s` (it's serialized in `api/models.go`).
> So the value is supported everywhere except the schema `OneOf`, which blocks it
> client-side.
>
> **Expected**
>
> `OneOf` should include `"k8s"` (and any other backend-supported tunnel types —
> the CHANGELOG mentions PostgreSQL/MySQL/Network tunnel types for the 25.2 API
> that may have the same gap).
>
> **Repro**
>
> ```hcl
> resource "sra_protocol_tunnel_jump" "k8s" {
>   name            = "demo"
>   hostname        = "api.example"
>   jump_group_id   = <id>
>   jumpoint_id     = <id>
>   tunnel_type     = "k8s"
>   url             = "https://api.example:443"
>   ca_certificates = "<PEM>"
> }
> ```
>
> `terraform validate` → the error above.
