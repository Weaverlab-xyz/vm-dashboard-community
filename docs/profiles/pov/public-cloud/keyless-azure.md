# Keyless Azure for POVs

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you want POVs on Azure without a stored client secret.

Part of [Running POVs on a public cloud](../public-cloud.md).

Azure has the same three choices as AWS: **Client secret**, **SPIFFE identity**, and
**SPIFFE + Workload Credentials**. With either SPIFFE choice, no client secret is stored.
The dashboard presents its own JWT-SVID — `spiffe://<trust domain>/dashboard`, audience
`api://AzureADTokenExchange` — to Entra ID as a client assertion for the app in **Client
ID**, through a *federated identity credential* on that app. The prerequisites are the same
as [Keyless AWS](keyless-aws.md): the dashboard's SPIRE server,
and an issuer Entra can reach from the internet that equals `SPIRE_JWT_ISSUER`.

**SPIFFE identity alone.** One app, no secret:

1. **Choose the option.** In the panel, choose **SPIFFE identity**. That ticks the
   dashboard's SPIFFE identity and its Azure audience. Press **Check the issuer is
   reachable**.
2. **Trust the dashboard.** Add the federated credential with
   [`terraform/dashboard_federation/azure`](../../../../terraform/dashboard_federation/azure/main.tf).
   Its `application_object_id` is the app's **object** id, not its client id. The credential
   pins exactly `spiffe://<trust domain>/dashboard`.
3. **Grant the role.** Apply
   [`terraform/pov_cloud_azure_role`](../../../../terraform/pov_cloud_azure_role/main.tf) with
   `provision_principal_ids` set to the app's service-principal object id
   (`az ad sp show --id <client id> --query id -o tsv`).
4. **Fill in the panel.** Enter the client id, tenant and subscription, save, and press
   **Test connection**. A pass reads `Connected to Azure subscription … using the dashboard's
   SPIFFE identity as app <client id>.`

**SPIFFE + Workload Credentials.** Two apps:

- **The everyday app** is the one in Client ID. It is federated as above and holds only
  **POV cloud read**. Pages, the overview, the environment list and Test connection run as it.
- **The Workload Credentials target app** holds **POV cloud provision**. A job mints a
  short-lived password onto it, so no write-capable secret exists between jobs.

Workload Credentials authenticates the dashboard with the same SPIFFE identity, so there is
no PAT either. Choosing the option sets `wlc_azure_everyday_source = federation`.

1. **Register the dashboard in Pathfinder.** Register it as a Custom IDP, exactly as for AWS
   ([steps](../../../platform/oidc/dashboard-identity.md#beyondtrust-workload-credentials)).
2. **Create the target app.** Create it, and its Azure integration and dynamic secret, with
   [`terraform/workload_credentials`](../../../../terraform/workload_credentials/README.md), or
   with `scripts/wlc/setup-azure-apps.sh`.
3. **Assign the roles.** Apply `pov_cloud_azure_role` with `read_principal_ids` set to the
   everyday app's service principal and `provision_principal_ids` set to the target app's.
4. **Turn on dynamic Azure credentials.** On the Workload Credentials panel, set
   Authentication to *Workload identity* with platform *SPIRE*. Turn on **dynamic credentials
   for Azure** with the target app's secret named.
5. **Choose the option and test.** On this panel, choose **SPIFFE + Workload Credentials**,
   save, and test. The test assumes nothing and mints nothing on the jobs' side: it
   authenticates to Workload Credentials with the free `GET /session`, as on AWS.

The POV reconcile keeps the everyday credential on Azure too (`jobs_worker.EVERYDAY_JOB_TYPES`).
Everyday calls **fail closed**: with a stored secret still set, or federation otherwise
unusable, they fail with the reason, never falling back to the job password.

**The roles are scoped to the subscription.** IAM can limit writes to resources carrying a
tag. Azure RBAC cannot, and every POV creates its own resource group, so both roles are
defined and assigned at the subscription. **Keep POVs in a subscription of their own** —
that subscription is the blast radius. Neither role can write role assignments, so neither
can widen itself.

**A stored client secret wins.** The panel offers **Remove stored secret**, which clears the
secret only. The client id stays, because it names the app the federated credential is on.

**Troubleshooting.** Test connection names Entra's `AADSTS` code and what fixes it:

| Code | Cause |
|---|---|
| `AADSTS70021` | No federated credential on that app matches the token's issuer, subject and audience. |
| `AADSTS700211` / `700213` | The federated credential names a different issuer / subject. |
| `AADSTS50027` | Entra could not validate the token. `<issuer>/keys` is not reachable — check the gateway serves `/spiffe/*`. |
| `AADSTS700016` | No app with that client id in that tenant. |
