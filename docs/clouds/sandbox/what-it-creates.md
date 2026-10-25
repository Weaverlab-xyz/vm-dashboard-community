# What each sandbox script creates

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want to know exactly what the sandbox scripts create in each cloud before you run them.

Part of [Cloud Sandbox Guide](../sandbox.md).

## AWS

```
VPC dashboard-sandbox-vpc (10.99.0.0/16)
  ├─ public subnet  10.99.1.0/24  → IGW → internet      [ECS Gateway task]
  ├─ private subnet 10.99.2.0/24  → local VPC only      [user EC2 instances]
  ├─ db subnet a    10.99.3.0/24  (AZ a) → local only   [managed databases]
  └─ db subnet b    10.99.4.0/24  (AZ b) → local only   [managed databases]

RDS:
  db subnet group dashboard-sandbox-db (spans db subnet a + b — RDS needs >= 2 AZs)
    private Postgres/MySQL/SQL-Server instances deploy here (no public endpoint;
    reached only through the PRA tunnel)

Security groups:
  dashboard-sandbox-jumpoint-sg
    egress: 0.0.0.0/0 (so PRA relay is reachable)
    ingress: none — egress-only; the Gateway dials out, nothing connects in
  dashboard-sandbox-vm-sg
    egress: 10.99.0.0/16 only — no internet
    ingress: tcp/22 and tcp/3389 from dashboard-sandbox-jumpoint-sg  (Shell Jump; Windows RDP jump)
  dashboard-sandbox-fn-vpce-sg
    ingress: tcp/443 from 10.99.0.0/16  (Secrets Manager interface endpoint)

VPC endpoints:
  dashboard-sandbox-secretsmanager-vpce   Interface, private subnet, private DNS
    Lets a network_mode=vpc Cloud Function read its bearer secret at cold start.
    Private DNS is VPC-WIDE, so the Gateway task and ECS runners resolve through
    it too — hence the /16 ingress. ~$7.30/mo; SANDBOX_SKIP_FN_VPCE=1 to skip.
  (the three SSM endpoints are created ON DEMAND by the dashboard, not here)

ECS:
  cluster bt-jumpoint
  IAM role ecsTaskExecutionRole (sandbox-tagged) with the AWS-managed
    AmazonECSTaskExecutionRolePolicy attached

Secrets Manager:
  dashboard/sandbox/ssh-keypair   {public_key, private_key} JSON

IAM (sandbox-tagged, deleted by rollback):
  role ecsTaskExecutionRole                      ECS task pull + logs
  role vmimport                                  vmie.amazonaws.com → S3 + EC2
  role dashboard-sandbox-promote-runner-task     ECS task → S3 PutObject
  user dashboard-sandbox-app                     Dashboard programmatic creds
    managed policy dashboard-app-policy          EC2 / ECS / SM / S3 / Logs / RDS /
                                                 Lambda / EKS / Cost Explorer
    access key cached at ~/.dashboard-sandbox/aws/secret_access_key (0600)
```

**Idempotency**: re-running `setup-aws.sh` looks up resources by tag and
reuses anything already present. The IGW, route tables, subnet associations,
SG rules, IAM policy attachments are all conditional inserts. The dashboard
IAM user is reused on re-runs and a new default version of the managed policy
is created each time, so policy edits in the script land without rotating the
access key. AWS allows at most 2 access keys per user, so the script never
rotates blindly;
if the cached secret is missing but a key still exists in AWS, it warns
with recovery paths rather than minting a third key.

## Azure

```
Resource group dashboard-sandbox-rg
  └─ VNet dashboard-sandbox-vnet (10.99.0.0/16)
       ├─ aci-subnet 10.99.1.0/24 (delegated to Microsoft.ContainerInstance)
       │    → internet egress (default)            [ACI Gateway]
       ├─ vm-subnet  10.99.2.0/24
       │    NSG dashboard-sandbox-vm-nsg:
       │      outbound: allow VirtualNetwork (priority 100)
       │                deny  Internet (priority 200)
       │      inbound:  allow VirtualNetwork
       │    → no internet egress                   [user Azure VMs]
       ├─ k8s-subnet 10.99.3.0/24                   [managed Kubernetes / AKS]
       ├─ db-subnet  10.99.4.0/24                   [managed databases]
       │    (delegated to Microsoft.DBforPostgreSQL/flexibleServers)
       │    → private VNet-integrated Flexible Server
       ├─ jumpoint-subnet 10.99.5.0/24              [tunnel-capable VM Gateway]
       │    → internet egress (no deny NSG; phones home to PRA)
       ├─ functions-subnet 10.99.9.0/24             [vpc-mode Cloud Functions]
       │    (delegated to Microsoft.Web/serverFarms)
       │    service endpoints: Microsoft.Storage, Microsoft.KeyVault
       │    NO NSG, deliberately — the module routes ALL app egress here, so an
       │    Internet deny would also block AzureWebJobsStorage and
       │    WEBSITE_RUN_FROM_PACKAGE and the app would index zero functions
       │    → Azure backbone only; generic egress needs a NAT gateway (~$32/mo)
       └─ desktops-subnet 10.99.6.0/24              [VDI desktop pools]
            NSG dashboard-sandbox-desktops-nsg:
              outbound: allow 443 → Internet  (priority 100)
                        allow VirtualNetwork   (priority 110)
                        deny  Internet         (priority 200)
              inbound:  allow RDP 3389 from VirtualNetwork
            → 443 egress only; for RS jump-client first-boot registration

Private DNS zone dashboard-sandbox.private.postgres.database.azure.com
  linked to the VNet → the Flexible Server's private FQDN resolves inside it.
  (The Azure analog of the AWS private DB subnet group / GCP private-services
  access. The DB is private-only; only the gateway VM reaches it.)

Storage account dashboard-sandbox-…  (Standard_LRS, file share `jpt`)
  for the ACI Gateway /jpt persistence volume

Key Vault dashboard-sandbox-kv-…
  Secret azureVM-ssh-keypair    {public_key, private_key} JSON

Service principal dashboard-sandbox-sp
  Contributor on the resource group
  Storage Blob Data Contributor on the storage account (Terraform state and the
    azure_blob backend are DATA-plane writes; Contributor alone cannot do them)
  User Access Administrator on the resource group (so the AKS module can create
    its own role assignment — Contributor lacks roleAssignments/write)
  Cost Management Reader on the SUBSCRIPTION (Cloud Costs; best-effort, warns
    rather than aborting if you lack Owner/UAA at subscription scope)
  Key Vault access policy: secrets get/list/set/delete (write is needed so
    per-VM admin passwords can be vaulted and removed again on teardown)
  AcrPull on the container registry (so the ACI runners can pull the mirrors)
  Dashboard Image Promoter custom role on the gallery RG (only when
    AZURE_IMAGE_GALLERY_RG is set; override with AZURE_IMAGE_GALLERY_ROLE)
  Credentials cached at ~/.dashboard-sandbox/azure/sp.json (mode 600)

Container registry dashboardsandboxacr…  (Basic SKU)
  Mirrors 4 public images so deploy-time pulls come from ACR, not Docker Hub
  (which rate-limits anonymous pulls):
    beyondtrust/sra-jumpoint:latest          [ACI Shell-Jump Gateway]
    chrweav/ansible-winrm:latest             [ACI config-mgmt runner; has pywinrm]
    chrweav/dashboard-promote-runner:latest  [ACI cross-cloud image promote]
    dtzar/helm-kubectl:latest                [ACI AKS k8s runner]
  One registry serves every region (globally pullable); per-region re-runs reuse it.
  Opt out with SANDBOX_SKIP_ACR=1. The VM-based tunnel Gateway still pulls from
  Docker Hub — its cloud-init docker-runs without a registry login.
```

**Naming caveat**: Key Vault, storage account, and container registry names
must be globally unique. The script appends a hash of your subscription ID to
keep them collision-safe.

## GCP

```
VPC dashboard-sandbox-vpc (custom mode)
  ├─ dashboard-sandbox-jumpoint-subnet 10.99.1.0/24
  │    → Cloud NAT → internet                     [Gateway COS GCE VM]
  └─ dashboard-sandbox-vm-subnet       10.99.2.0/24
       (no Cloud NAT mapping)
       → no internet egress                       [user GCE instances]

Cloud Router dashboard-sandbox-router
Cloud NAT    dashboard-sandbox-nat
  --nat-custom-subnet-ip-ranges dashboard-sandbox-jumpoint-subnet
  (only the gateway subnet gets NAT — the VM subnet is genuinely cut off)

Firewall rules:
  dashboard-sandbox-allow-internal
    ingress, all protos, source 10.99.0.0/16
  dashboard-sandbox-allow-ssh-from-jumpoint
    ingress tcp/22, source-tag bt-jumpoint, target-tag dashboard-sandbox-vm
  dashboard-sandbox-allow-rdp-from-jumpoint                [Windows RDP jump]
    ingress tcp/3389, source-tag bt-jumpoint, target-tag dashboard-sandbox-vm
  dashboard-sandbox-allow-ssh-from-k8s                   [co-located GKE agent]
    ingress tcp/22, source = k8s node subnet + GKE pod range, target-tag dashboard-sandbox-vm
  dashboard-sandbox-deny-vm-egress
    egress, all protos, target-tag dashboard-sandbox-vm, dest 0.0.0.0/0
  dashboard-sandbox-allow-vm-egress-vpc
    egress, all protos, target-tag dashboard-sandbox-vm, dest 10.99.0.0/16
  dashboard-sandbox-allow-db-from-jumpoint              [managed databases]
    egress tcp/5432, target-tag bt-jumpoint, dest = PSA range
  dashboard-sandbox-allow-db-from-k8s                   [co-located GKE agent]
    egress tcp/5432,3306,1433, target-tag dashboard-sandbox-k8s, dest = PSA range

Private Services Access (managed databases):
  dashboard-sandbox-psa-range  (/20 VPC_PEERING address)
  servicenetworking peering on the VPC → Cloud SQL private IP path.
  The instance is private-IP-only; its IP lands in the peered range
  (outside 10.99.0.0/16), so deny-vm-egress already blocks user VMs —
  only the gateway reaches it (GCP analog of the AWS private DB subnets).

Service account dashboard-sandbox-sa@<project>.iam.gserviceaccount.com
  Roles: 21 project-level bindings — see the `for role in ...` loop in
         scripts/sandbox/Linux/setup-gcp.sh, which carries a why-comment per
         role. (Listing them here just went stale; the script is the source
         of truth.) Cloud Functions adds cloudfunctions.developer,
         secretmanager.admin, cloudbuild.builds.builder and
         artifactregistry.writer, plus the cloudfunctions +
         artifactregistry APIs.
  Key cached at ~/.dashboard-sandbox/gcp/sa-key.json (mode 600)

Secret Manager:
  dashboard-sandbox-ssh-keypair    {public_key, private_key} JSON
```

**Auto-tagging**: the dashboard automatically attaches the `bt-jumpoint`
network tag to its Gateway COS VM and the `dashboard-sandbox-vm` tag (read
from the `gcp_default_network_tag` config key the sandbox script provides)
to every user VM it deploys. The firewall rules' source/target tags match
those automatically — no manual tagging in the deploy form.
