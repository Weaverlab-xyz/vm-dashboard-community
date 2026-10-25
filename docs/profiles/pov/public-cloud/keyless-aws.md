# Keyless AWS for POVs

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you want POVs on AWS without a stored access key, through the dashboard's SPIFFE identity.

Part of [Running POVs on a public cloud](../public-cloud.md).

On AWS the panel offers three credentials: **Access key**, **SPIFFE identity**, or **SPIFFE +
Workload Credentials** ([below](#with-workload-credentials-for-jobs)). With either of the
last two, nothing secret is stored. The dashboard presents its own JWT-SVID —
`spiffe://<trust domain>/dashboard`, audience `sts.amazonaws.com` — to AWS STS and assumes a
role with `AssumeRoleWithWebIdentity`. The app and the worker each keep their own token, so
the provision jobs use it too. The mechanism, and the same identity for Azure, GCP and
Workload Credentials, is in [The dashboard's own identity](../../../platform/oidc/dashboard-identity.md).

It needs the dashboard's own SPIRE server ([SPIFFE and SPIRE](../../../platform/spiffe.md)), and it needs
an **issuer AWS can reach from the internet**. With the cloud gateway that is the agent
hostname plus `/spiffe`, e.g. `https://pov-agents.example.com/spiffe`, and it must equal the
SPIRE server's `SPIRE_JWT_ISSUER` exactly.

1. **Publish the issuer.** Run a gateway image built from the current
   `examples/cloud-gateway`, which proxies `/spiffe/*` on the agent hostname. Set
   `SPIRE_JWT_ISSUER` on the SPIRE sidecar to the issuer.
2. **Turn on the identity.** In the panel, choose **SPIFFE identity**. That ticks the
   dashboard's SPIFFE identity and its AWS audience, the same settings as **Remote Agents**.
   Then press **Check the issuer is reachable**. Every check must pass before AWS can
   accept a token.
3. **Create the policies and the role.** In the AWS account, apply
   [`terraform/pov_cloud_aws_policy`](../../../../terraform/pov_cloud_aws_policy/main.tf), then
   [`terraform/dashboard_federation/aws`](../../../../terraform/dashboard_federation/aws/main.tf):

   ```hcl
   # dashboard_federation/aws/terraform.tfvars
   issuer_url   = "https://pov-agents.example.com/spiffe"
   trust_domain = "pov.example.com"
   role_name    = "vm-dashboard-pov"
   policy_arns  = ["<provision_policy_arn from pov_cloud_aws_policy>"]
   ```

   The role trusts exactly `spiffe://<trust domain>/dashboard` with audience
   `sts.amazonaws.com`. Never trust the whole trust domain, because the POV's control node agents
   share it.
4. **Point the panel at the role.** Paste the role ARN into **Role to assume**, save, and
   press **Test connection**. A pass names the assumed role: `Connected to AWS in us-east-2 as
   arn:aws:sts::…:assumed-role/vm-dashboard-pov/vm-dashboard, using the dashboard's SPIFFE
   identity.`

**A stored key always wins.** While an access key is stored, every call uses it and the role
is never assumed. Switching the radio does not delete a key. The panel shows **Remove stored
key** for that, as a separate click, so a key never disappears without you seeing it go.
Remove it once the role works.

`pov_cloud_aws_policy` holds two policies: `pov-cloud-read`, for request-path reads, and
`pov-cloud-provision`, which adds build, power and teardown. Mutating and destructive EC2
actions are conditioned on the `povManagedBy=vm-dashboard` tag, so the role cannot change a
resource a POV did not create. EC2 forces two exceptions: the VPC's main route table, which
exists before it can be tagged, and the AMI and network interface a `RunInstances` names.
If a build fails with `UnauthorizedOperation`, decode the message with
`aws sts decode-authorization-message`. Setting `tag_conditions = false` rules the
conditions out while you diagnose.

## With Workload Credentials for jobs

**SPIFFE + Workload Credentials** splits the credential in two:

- **Everyday calls** use the role above, which needs only `pov-cloud-read`. These are the
  pages, the overview, the environment list and Test connection.
- **Jobs** take a short-lived lease from BeyondTrust Workload Credentials, minted only when a
  job needs one. These are build, control node install, power and teardown. The lease's target role carries
  `pov-cloud-provision`.

So no write-capable AWS credential is live between jobs. Workload Credentials authenticates
the dashboard with the same SPIFFE identity (`wlc.jwt`), so there is no Pathfinder PAT to
store either. Choosing the option sets `wlc_aws_everyday_source = federation`; everything
else about Workload Credentials stays on its own panel.

1. **Register the dashboard in Pathfinder** as a Custom IDP Workload Identity. Use the issuer
   above, a `sub` condition of exactly `spiffe://<trust domain>/dashboard`, and an `aud`
   condition. The steps are in
   [Workload Credentials with the SPIFFE identity](../../../platform/oidc/dashboard-identity.md#beyondtrust-workload-credentials).
2. **Apply the target side** with
   [`terraform/workload_credentials`](../../../../terraform/workload_credentials/README.md):

   ```hcl
   aws_dashboard_policy_arn = "<provision_policy_arn from pov_cloud_aws_policy>"
   create_everyday_secret   = false   # the SPIFFE role is the everyday credential
   ```

3. **Narrow the federated role** by re-applying `dashboard_federation/aws`:

   ```hcl
   policy_arns   = ["<read_policy_arn from pov_cloud_aws_policy>"]
   everyday_only = true   # also denies creating or passing roles
   ```

4. **On the Workload Credentials panel**, set Authentication to *Workload identity* with
   platform *SPIRE*, the Service Name and the token audience. Turn on dynamic credentials for
   AWS with the provisioning secret named (`dashboard-provision`). Then, on this panel, choose
   **SPIFFE + Workload Credentials**, save, and press **Test connection**.

**Test connection proves both halves and mints nothing.** The everyday half assumes the role,
as above. The jobs' half authenticates to Workload Credentials with `GET /session`. That proves
the Pathfinder trust, the site and the Service Name, and it is free. A lease is a **billed**
issuance, so the button never mints one. The first job does, and its outcome shows under
Workload Credentials → Active credential source.

**The ten-minute POV reconcile keeps the everyday credential.** It runs on a timer and only
reads: it lists environments, and for the suspend schedule it *enqueues* a power job, which
then takes its own lease. Running it under the provisioning lease would keep a billed,
write-capable credential alive around the clock. So it is exempt
(`jobs_worker.EVERYDAY_JOB_TYPES`), and every other job still takes the lease.

**Everyday calls fail closed.** If federation is not usable — a stored key is still set, the
role is missing, or the token file is absent — calls outside jobs fail with that reason. They
never fall back to the lease.

**Troubleshooting.**
- **`InvalidIdentityToken`:** AWS could not fetch or validate the issuer's keys. Either the
  issuer is not public, the gateway predates `/spiffe/*`, or the IAM OIDC provider's URL is not
  exactly the issuer.
- **`AccessDenied` on `AssumeRoleWithWebIdentity`:** the role's trust policy does not match the
  token's `sub` or `aud`.
- **"`aws.jwt` does not exist yet":** the token loop has not run since the identity was
  switched on. It runs every minute in both the app and the worker.
