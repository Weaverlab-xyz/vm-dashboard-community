# Keyless GCP for POVs

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you want POVs on GCP without a stored key, with write access granted through Entitle.

Part of [Running POVs on a public cloud](../public-cloud.md).

GCP has three choices: **Service account key**, **SPIFFE identity**, and **SPIFFE +
Entitle**. With either SPIFFE choice no key is stored. The dashboard presents its own
JWT-SVID to Google STS through a workload identity provider, then acts as a service account.
The prerequisites are the same as [Keyless AWS](keyless-aws.md):
the dashboard's SPIRE server, and an issuer Google can reach that equals `SPIRE_JWT_ISSUER`.

Workload Credentials mints no GCP credentials, so on GCP the privileged half comes from
**Entitle**. The service account holds only a read role. Before a build, power change or
teardown writes anything, the dashboard asks Entitle for the provision role on behalf of a
**machine user** — an Entitle user the service account is assigned to on the existing GCP
integration. Entitle binds the role onto that same service account for the length of the
grant.

**SPIFFE identity alone:**

1. **Choose the option.** In the panel, choose **SPIFFE identity** and press **Check the
   issuer is reachable**.
2. **Federate the dashboard.** Apply
   [`terraform/dashboard_federation/gcp`](../../../../terraform/dashboard_federation/gcp/main.tf)
   with `service_account_email`. Its outputs are the panel's **Workload identity provider**
   and **Service account to act as**.
3. **Grant the roles.** Apply [`terraform/pov_cloud_gcp_role`](../../../../terraform/pov_cloud_gcp_role/main.tf)
   with that `service_account_email` and `bind_provision = true`.
4. **Save, then Test connection.**

**SPIFFE + Entitle:**

1. **Federate and grant read only.** Do steps 1–2 above, then apply `pov_cloud_gcp_role`
   with `bind_provision = false`. The service account gets **POV cloud read** only. The
   **POV cloud provision** role exists on the project, unbound.
2. **Create the machine user.** Add a user for the dashboard, for example
   `vm-dashboard-pov@…`, to the IdP group Entitle syncs (an Entra SAML group, say). In
   Entitle, assign that user the service account as its account on the GCP integration.
3. **Make the role requestable.** In Entitle, find **POV cloud provision** on the project
   resource, or put it in a bundle. Give it a workflow that **auto-approves** for the machine
   user's group, with a duration longer than your longest build.
4. **Point the dashboard at it.** In the panel, choose **SPIFFE + Entitle**. Set the
   **Entitle machine user**, the role or bundle id, and the **grant duration**, which must be
   one the workflow allows.
5. **Authenticate to Entitle.** The dashboard does this through the
   [platform identity](../../../beyondtrust/platform-identity.md) (Entitle in the
   registration's Product Scopes), so no Entitle token is stored. The native API token
   still works.
6. **Save, then Test connection.** It reads the service account's permissions and looks up
   the machine user. **It never files a request**, because an auto-approved request is a
   real grant. It reports a standing write role on the service account as a problem: that
   makes every request moot.

How a grant behaves:

- **One grant serves many writes.** It is reused while it has at least a third of its
  duration left. A build is a handful of writes, and a request per write would mean an
  approval record and an IAM wait each.
- **The job waits for IAM.** After a fresh grant, the job polls `testIamPermissions` until
  GCP reports the provision permissions, for up to five minutes. Entitle reports *granted*
  once it has written the binding, but GCP applies it eventually, and a build started early
  fails with a 403 on its first insert.
- **Grants end by duration.** Nothing revokes early: Entitle's revoke endpoint needs a
  person's token, which would be a standing secret.
- **Reads never ask.** The ten-minute reconcile and every page read only, as the service
  account.
- **It fails closed.** If Entitle refuses or never grants, the job fails with Entitle's
  reason, and no write is attempted with less privilege.
