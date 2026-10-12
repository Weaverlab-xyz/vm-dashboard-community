# SPIFFE federation: proving it against real clouds

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are setting up the dashboard to hold no stored cloud secret, and need to prove each cloud accepts its SPIFFE identity, with and without Workload Credentials.

The dashboard can authenticate to AWS, Azure and GCP with short-lived tokens from its own
SPIRE server instead of a stored key. With Workload Credentials, the same identity can be
the everyday credential while jobs take a lease. Feature reference:
[The dashboard's own identity](../oidc/dashboard-identity.md) and
[dynamic credentials](../workload-lab/dynamic-credentials.md). Trust modules:
[`terraform/dashboard_federation`](../../terraform/dashboard_federation/README.md).

Unit tests, `terraform validate` and `packer validate` against the real azure plugin pass.
**No cloud has yet accepted one of these tokens**, so this procedure is the proof. Run it
in sandbox accounts, in order: each section assumes the ones before it passed. Record the
outcome in [§9](#9-record-the-result).

## 0. Prerequisites

- The SPIRE overlay running:
  `docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d`.
- An **issuer reachable from the internet** over HTTPS with a certificate from a **public CA**.
  By default it is the agent gateway's public URL plus `/spiffe`.
- One sandbox account, subscription or project per cloud you will test, and permission to
  create IAM roles, app credentials or workload identity pools in it.
- Note three values from **Settings → Integrations → Remote Agents → Give the dashboard its
  own SPIFFE identity**. Every trust below uses them exactly:
  - the **issuer**, for example `https://agents.example.com/spiffe`, with no trailing slash;
  - the **trust domain**, for example `dashboard.example.com`;
  - the **subject**, which is always `spiffe://<trust domain>/dashboard`.

## 1. The identity itself

1. Tick **Give the dashboard its own SPIFFE identity** and the audiences you will test
   (**AWS**, **Azure**, and the GCP provider name after §7). Save.
2. Within a minute the panel lists one token file per audience, each with *valid until*.
   The same data is at `GET /api/spiffe-identity`.
3. Press **Check the issuer is reachable**.

**Pass:** every row is ✓: *Issuer*, *Discovery document*, *Issuer matches*, *Signing keys*,
*Keys are this dashboard's*.

That check runs from inside the dashboard. Confirm from a machine **outside** your network
too, because a firewall that lets the dashboard out but not the internet in passes the
check and still fails every cloud:

```bash
ISS=https://agents.example.com/spiffe
curl -fsS "$ISS/.well-known/openid-configuration" | jq .issuer
curl -fsS "$ISS/keys" | jq '.keys | length'
```

```powershell
$Iss = 'https://agents.example.com/spiffe'
(Invoke-RestMethod "$Iss/.well-known/openid-configuration").issuer
(Invoke-RestMethod "$Iss/keys").keys.Count
```

**Pass:** the issuer printed is exactly the value from §0, and at least one key is served.

**If it fails:** an untrusted certificate or a private name stops here. AWS and Azure fetch
these documents anonymously and will refuse every token.

## 2. AWS, without Workload Credentials

1. Apply the AWS trust with the dashboard's own policy:

   ```bash
   cd terraform/dashboard_federation/aws
   terraform init
   terraform apply -var issuer_url=$ISS -var trust_domain=dashboard.example.com \
     -var 'policy_arns=["arn:aws:iam::<account>:policy/dashboard-app-policy"]'
   terraform output dashboard_settings
   ```

2. Paste `aws_federation_role_arn` into the SPIFFE panel and keep **AWS** ticked.
3. Clear the stored AWS access key (Settings → AWS). The stored key wins while it exists.

**Pass:**

- The panel's source line reads **AWS uses SPIFFE federation**.
- An AWS page (Inventory, or the AWS tiles) loads.
- CloudTrail shows `AssumeRoleWithWebIdentity` for the role, with role session name
  `vm-dashboard` and the subject from §0 in the web identity token's claims.
- An EC2 deploy job completes. That proves Terraform's provider and its S3 state backend,
  which get the assumed role's session credentials.

**Renewal:** the dashboard re-assumes the role five minutes before the session ends. In a
process that stays up past an hour, CloudTrail shows a second `AssumeRoleWithWebIdentity`,
and pages keep working across it.

**If it fails:**

- `AccessDenied` from STS: the trust policy's `:sub` or `:aud`, or an issuer typo.
- `InvalidIdentityToken`: AWS could not fetch or verify the keys (back to §1).

## 3. AWS, with Workload Credentials as the privileged path

Prerequisite: Workload Credentials is configured for AWS (`dashboard-provision` exists).

1. Re-apply §2's module with `-var everyday_only=true`. The role gains the same deny on
   creating and passing roles as the WLC everyday secret.
2. Set **Settings → Workload Credentials → AWS — everyday credential** to *The dashboard's
   SPIFFE identity*.

**Pass:**

- **Settings → Workload Credentials → Active credential source → Check** shows
  `everyday SPIFFE federation` for AWS, and the `provision` lease as *minted when a job runs*.
- Page traffic is the federated role in CloudTrail (`AssumeRoleWithWebIdentity`).
- An EC2 deploy job mints the provisioning lease. CloudTrail shows its session with the
  session tag `wlc-purpose=provision`.
- The job's Terraform signs through `credential_process`. With `TF_LOG=DEBUG` on the worker
  for one run, its log has `credentials_source=ProcessProvider`.

**Fail closed:** untick **AWS** in the SPIFFE panel. AWS pages must fail with *"AWS's everyday
credential is the dashboard's SPIFFE identity … federation is not usable"*, and **not**
carry on with the lease. Tick it again afterwards.

## 4. Azure, without Workload Credentials

1. Apply the federated credential on the app in `azure_client_id`:

   ```bash
   cd terraform/dashboard_federation/azure
   terraform init
   terraform apply -var issuer_url=$ISS -var trust_domain=dashboard.example.com \
     -var application_object_id=$(az ad app show --id <azure_client_id> --query id -o tsv)
   ```

   ```powershell
   $Obj = az ad app show --id <azure_client_id> --query id -o tsv
   terraform apply -var "issuer_url=$Iss" -var 'trust_domain=dashboard.example.com' `
     -var "application_object_id=$Obj"
   ```

2. Tick **Azure** in the SPIFFE panel and clear the stored Azure client secret.

**Pass:**

- The source line reads **AZURE uses SPIFFE federation**.
- Azure pages load.
- Entra **Sign-in logs → Service principal sign-ins** for the app show the credential type
  as a federated identity credential.
- A VM deploy job completes. Its Terraform runs with `ARM_USE_OIDC`.

**If it fails:** `AADSTS70021` (no matching federated identity record) is an issuer or
subject mismatch; compare with §0 character for character.

## 5. Azure, with Workload Credentials as the privileged path

Prerequisite: Workload Credentials is configured for Azure (`dashboard-azure`).

1. Grant the everyday app, dry run first:

   ```bash
   scripts/wlc/setup-azure-apps.sh --reference-sp <current SP> \
     --everyday-sp <azure_client_id> --dry-run
   ```

   **Pass:** the everyday lines list `NOT granting` for *User Access Administrator*, *Owner*
   and *Role Based Access Control Administrator*, and for any custom role that can write role
   assignments. Then run it without `--dry-run`.
2. Set **Azure — everyday credential** to *The dashboard's SPIFFE identity*.
3. Set **Everyday identity's object id**:
   `az ad sp show --id <azure_client_id> --query id -o tsv`.

**Pass:**

- **Active credential source** shows `everyday SPIFFE federation` for Azure.
- Sign-in logs show page traffic as the federated app and a job's sign-in as the Workload
  Credentials target app with a password credential.
- An **AKS cluster deployed after step 3** is reachable from the Kubernetes page (`/k8s`).
  This proves the everyday identity was granted cluster admin. Clusters built before the
  switch need it added by hand.

**Fail closed:** as in §3, with **Azure** unticked.

## 6. An Azure image build with no secret

Run this on the §4 setup: no Workload Credentials lease for Azure, and no stored secret.

1. Set **Packer image builds: assertion lifetime** above your build's length; 120 minutes
   covers a typical Linux image.
2. Start an Azure Packer build.

**Pass:** the build completes. The sign-in logs show the app signing in with a federated
credential at the start of the build. The template file and the job log contain no JWT.

**The limit, on purpose:** set the lifetime to 15 minutes and run a build that takes longer.
It should fail when the builder signs in again after the assertion has expired. Record how
long it ran; that is the margin to leave above your longest build.

## 7. GCP

1. Apply the pool, provider and impersonation of the service account whose key you are
   retiring:

   ```bash
   cd terraform/dashboard_federation/gcp
   terraform init
   terraform apply -var issuer_url=$ISS -var trust_domain=dashboard.example.com \
     -var project_id=<project> -var service_account_email=<sa>@<project>.iam.gserviceaccount.com
   terraform output dashboard_settings
   ```

2. Paste `dashboard_spiffe_aud_gcp` into the panel's GCP audience and the service account
   into its field, and set `gcp_project_id`.
3. Clear the stored service-account key: `gcp_service_account_json`, and
   `gcp_credentials_json` if set.

**Pass:**

- The source line reads **GCP uses SPIFFE federation**.
- GCP pages load.
- Cloud Audit Logs show `sts.googleapis.com` token exchange and
  `GenerateAccessToken` on the service account.
- A GCE deploy job completes. Terraform reads `gcp-external-account.json` from beside the
  token.

**If it fails:** `invalid_grant` from STS is usually the provider's attribute condition. It
must equal the subject from §0 exactly.

## 8. A key-less install looks configured

With no stored key for any cloud you tested:

- The home page shows the AWS, Azure and GCP tiles.
- On **Images**, an image's promote-to-AWS preflight row *Target credentials configured* passes
  and reads *AWS credentials come from the dashboard's SPIFFE identity* (or *from BeyondTrust
  Workload Credentials* in §3's setup).
- **Health → Workload credentials** shows the provisioning lease as on demand, not failing.

## 9. Record the result

Copy this into the PR or issue that closes out the test, one row per section you ran:

| Cloud | Scenario | Section | Result | Date | Notes |
|---|---|---|---|---|---|
| — | Issuer reachable from outside | §1 | | | |
| AWS | Federation only | §2 | | | |
| AWS | Everyday federation, jobs on WLC | §3 | | | |
| Azure | Federation only | §4 | | | |
| Azure | Everyday federation, jobs on WLC | §5 | | | |
| Azure | Packer build on an assertion | §6 | | | |
| GCP | Federation, SA impersonation | §7 | | | |
| — | Key-less install looks configured | §8 | | | |

Once a cloud has accepted a token, update the *Preview* note at the top of
[The dashboard's own identity](../oidc/dashboard-identity.md) to say which clouds are proven.
