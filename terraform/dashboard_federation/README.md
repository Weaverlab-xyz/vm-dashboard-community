# Dashboard federation: the cloud side of the SPIFFE identity

Three small root modules that make AWS, GCP and Azure trust the dashboard's own SPIFFE
identity, so it needs **no stored cloud secret**, with or without BeyondTrust Workload
Credentials. Apply only the ones you use; each configures only its own provider.

| Module | Creates | Then set in the dashboard |
|---|---|---|
| [`aws/`](aws/) | IAM OIDC provider for the issuer (or reuses yours), a role trusting exactly `spiffe://<trust domain>/dashboard` with audience `sts.amazonaws.com`, its policy attachments | Tick **AWS**, paste `aws_federation_role_arn`, clear the stored access key |
| [`gcp/`](gcp/) | Workload identity pool and OIDC provider with an attribute condition on the exact subject; a `workloadIdentityUser` binding on a service account, or roles granted to the principal directly | Paste the GCP audience and service account, set `gcp_project_id`, clear the stored service-account key |
| [`azure/`](azure/) | A federated identity credential on the app in `azure_client_id` | Tick **Azure**, clear the stored client secret |

Each module's `dashboard_settings` output lists the values. They go under
**Settings → Integrations → Remote Agents → Give the dashboard its own SPIFFE identity**.

## Before you apply

- **The issuer must be public, over HTTPS with a certificate from a public CA.** The clouds
  fetch `<issuer>/.well-known/openid-configuration` and `<issuer>/keys` anonymously when a
  token is presented. Use **Check the issuer is reachable** on the same panel first.
- **Use the issuer and trust domain exactly as the panel shows them.** A trailing slash or
  a different host is a different issuer, and every token is refused.

## With Workload Credentials

The federated identity can be the **everyday** credential while jobs use a Workload
Credentials lease, so nothing that can create or hand out roles is live between jobs:

- **AWS:** apply `aws/` with `everyday_only = true` (it adds the same deny the WLC
  everyday secret carries), then set `wlc_aws_everyday_source = federation`.
- **Azure:** apply `azure/` on the app in `azure_client_id`, grant it with
  `scripts/wlc/setup-azure-apps.sh --everyday-sp`, then set
  `wlc_azure_everyday_source = federation`.
- **GCP** has no Workload Credentials tier; the federated identity is the credential.

See [dynamic credentials](../../docs/preview/workload-lab/dynamic-credentials.md) and
[the dashboard's SPIFFE identity](../../docs/platform/oidc/dashboard-identity.md).

## Without Workload Credentials

The federated identity is the only credential. Give the AWS role the dashboard's full
policy (`everyday_only = false`, the default), the GCP service account or principal the
roles the old key had, and the Azure app the roles its secret had.
