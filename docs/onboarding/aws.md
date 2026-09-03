# AWS setup

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are giving the dashboard an AWS account to deploy into.

Part of the [Onboarding Guide](../ONBOARDING.md).


The dashboard deploys EC2 instances into **your** AWS account using an IAM
user dedicated to the dashboard.

### 1. Create the IAM user and attach a policy

The dashboard needs a **customer-managed** policy. The AWS-managed policies an
earlier version of this guide recommended do not work — see
[Why not the AWS-managed policies](#why-not-the-aws-managed-policies) below.

**Recommended — lift the canonical policy.** `dashboard-app-policy` in
[`scripts/sandbox/Linux/setup-aws.sh`](../../scripts/sandbox/Linux/setup-aws.sh)
(the `DASHBOARD_POLICY_DOC` heredoc) covers every AWS call the dashboard makes,
across every feature. That script is the source of truth — restating every
statement here would only go stale.

```powershell
aws iam create-user --user-name dashboard-dev

# Copy the DASHBOARD_POLICY_DOC JSON out of setup-aws.sh into
# dashboard-app-policy.json, substituting your account id and your storage
# bucket prefix for the ${...} placeholders, then:
aws iam create-policy `
  --policy-name dashboard-app-policy `
  --policy-document file://dashboard-app-policy.json

aws iam attach-user-policy --user-name dashboard-dev `
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/dashboard-app-policy
```

> **It has to be a managed policy, not an inline one.** Inline user policies are
> capped at 2048 bytes and this document is ~5.2 KB (the managed-policy quota is
> 6144). Managed policies are also versioned, so
> `aws iam create-policy-version --set-as-default` propagates later edits
> without rotating the access key.

**Minimal — core VM deploys only.** If you only want the Cloud VMs page, these
are the permissions that path actually needs:

| Purpose | Actions |
|---------|---------|
| Instance lifecycle | `ec2:Describe*`, `ec2:RunInstances`, `ec2:StartInstances`, `ec2:StopInstances`, `ec2:TerminateInstances`, `ec2:RebootInstances`, `ec2:ModifyInstanceAttribute`, `ec2:CreateTags`, `ec2:DeleteTags`, `ec2:GetPasswordData` |
| Security groups and key pairs | `ec2:CreateSecurityGroup`, `ec2:DeleteSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`/`Egress`, `ec2:RevokeSecurityGroupIngress`/`Egress`, `ec2:CreateKeyPair`, `ec2:DeleteKeyPair`, `ec2:ImportKeyPair` |
| **Terraform remote state** | `s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload`, `s3:ListBucketMultipartUploads`, `s3:ListMultipartUploadParts` — on the bucket **and** on `<bucket>/*` |
| Instance profiles | `iam:PassRole`, scoped to the roles you let the dashboard pass |
| Credential test | `sts:GetCallerIdentity` |

Everything past that is per-feature. Each row below names the `Sid` in
`setup-aws.sh` so you can lift just that statement:

| Feature | Extra permissions | `Sid` in `setup-aws.sh` |
|---------|-------------------|-------------------------|
| Cross-cloud image promote (VHD export / import) | `ec2:ImportImage`, `ec2:ExportImage`, `ec2:DescribeImportImageTasks`, `ec2:DescribeExportImageTasks`, `ec2:CancelImportTask`, `ec2:CancelExportTask` — plus a `vmimport` role with its own trust policy | `DashboardVMImportExport` |
| Gateways and remote runners (ECS) | ECS cluster / task-definition / task actions, `ssm:GetParameter` and `ssm:GetParameters` on the ECS-optimized-AMI parameter path, `iam:CreateServiceLinkedRole` for `ecs.amazonaws.com`, and `iam:PassRole` on `ecsTaskExecutionRole` / `ecsInstanceRole` | `DashboardECS`, `DashboardECSOptimizedAMI`, `DashboardServiceLinkedRole`, `DashboardPassRoles` |
| Kubernetes clusters (EKS) | `eks:*`, role CRUD scoped to `role/k8s-*` and `role/*-role`, `iam:CreateServiceLinkedRole` for the `eks*` service principals, `iam:GetRole` | `DashboardEKS`, `DashboardRoles`, `DashboardEKSServiceLinkedRole`, `DashboardEKSGetRole` |
| Cloud databases (RDS) | DB instance and DB subnet group CRUD, `rds:DescribeDBEngineVersions`, `rds:DescribeOrderableDBInstanceOptions`, tag actions | `DashboardRDS` |
| Cloud Functions (Lambda) | `lambda:*`, plus Secrets Manager on `secret:*-fn-secret-*` | `DashboardLambda`, `DashboardSecretsManager` |
| Cloud Costs | `ce:GetCostAndUsage`, `ce:GetCostForecast`, `ce:GetDimensionValues`, `ce:GetTags` | `DashboardCostExplorer` |
| External secrets backend | Secrets Manager CRUD on `secret:dashboard/*` — see [secrets-management.md](../secrets-management.md#iam-permissions-required-per-backend) | `DashboardSecretsManager` |
| Password Safe VM onboarding | `ssm:SendCommand`, `ssm:GetCommandInvocation`, `ssm:ListCommandInvocations` | `DashboardSSMRunCommand` |
| Job log streaming | `logs:*` | `DashboardLogs` |

#### Why not the AWS-managed policies

Earlier revisions of this guide told you to attach `AmazonEC2FullAccess`,
`AmazonS3ReadOnlyAccess` and `IAMReadOnlyAccess`. That combination is both
broader than necessary and too narrow to work:

- **`AmazonEC2FullAccess`** grants more EC2 than the dashboard uses, and nothing
  at all for ECS, EKS, Lambda, RDS, Secrets Manager or Cost Explorer — every one
  of which this same guide sets up later (Appendix L, Appendix M, and the
  external-vault step in Part D).
- **`AmazonS3ReadOnlyAccess` breaks deploys outright.** Terraform state lives in
  your active storage backend, so an apply must *write* `s3:PutObject` /
  `s3:DeleteObject` for both the `.tfstate` object and its `.tflock` companion
  (the dashboard uses S3-native state locking). Read-only fails every apply and
  destroy. Worse, with no storage backend configured at all, state falls back to
  the container's local disk, where losing that directory **orphans live cloud
  resources** — see [infrastructure-as-code.md](../infrastructure-as-code.md#state-the-thing-that-makes-iac-work).
- **`IAMReadOnlyAccess` cannot `iam:PassRole`**, which is the action attaching an
  instance profile actually requires. The old "looking up instance profiles"
  rationale named the wrong mechanism.

### 2. Create the access key

```powershell
aws iam create-access-key --user-name dashboard-dev
```

Copy the `AccessKeyId` and `SecretAccessKey` from the output — you will
paste them into `.env` in Part D.

### 3. Pick a default region

The dashboard uses `AWS_REGION` as the default for all deploys. Common
picks: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`.

---
