# Outbound addresses on Azure Container Apps

> **Audience:** operator · **Profile:** `both` · **Read this when:** managed nodes' firewalls need the dashboard's outbound addresses, and you are choosing how to give them.

Part of [Cloud Hosting: Azure Container Apps](../azure-container-apps.md).

A Container Apps environment with no NAT gateway has **no single outbound address**.
It SNATs traffic out of a shared pool of several hundred addresses, and it picks an
address per *destination*. That breaks anything that admits the dashboard by source
address, the managed [Portainer](../../../cloud-services/portainer/managed-server.md#firewall) and
[Rancher](../../../cloud-services/rancher.md) node firewalls above all. The dashboard learns its
address from an echo service, but the echo service and the node see *different*
addresses, so the node drops the bootstrap. The job reads "serving", then
`ConnectTimeout`, then "re-detecting … produced no change".

There are three ways to fix it. Pick one:

| | Cost | Setup | What the node admits |
|---|---|---|---|
| [Let the dashboard read its pool](#option-1-let-the-dashboard-read-its-own-pool) | free | one Reader grant | the environment's published pool (~400 shared Azure addresses) |
| [NAT gateway](#option-2-a-nat-gateway) | ~$37/month + $0.045/GB | three `az` commands | one static address you own |
| [Paste the pool by hand](#option-3-paste-the-pool-by-hand) | free | a copy-paste, repeated when it goes stale | the same pool as option 1, frozen |

The pool in options 1 and 3 is shared Azure address space: other tenants' container
apps in the same region can egress from it too, so they could reach the node's
management ports. The node's own login still stands between them and the node. A NAT
gateway is the only option that admits nothing but the dashboard.

## Option 1: let the dashboard read its own pool

The platform publishes the pool as `properties.outboundIpAddresses` on the Container
App, so the dashboard reads it with the app's own managed identity. Whenever a node
firewall is applied (deploy, **Re-apply**, token mint), the whole pool is admitted on
tcp 9443/8000 (Portainer) or 80/443 (Rancher) and re-read. There's nothing to paste
and nothing to go stale. It needs **one read grant**, and one grant on the resource
group covers both apps:

```bash
for app in dash dash-worker; do
  az containerapp identity assign -n $app -g RG-DASH --system-assigned
  az role assignment create --role Reader \
    --scope "$(az group show -n RG-DASH --query id -o tsv)" \
    --assignee-principal-type ServicePrincipal \
    --assignee-object-id "$(az containerapp show -n $app -g RG-DASH --query identity.principalId -o tsv)"
done
```

**Settings → Containers** (and **→ Kubernetes**) shows *Hosting platform outbound
pool: N addresses* once it works. When it doesn't, the readout shows why. That's nearly
always the missing grant, and the message includes the exact command with the
identity's object id already filled in.

- **Two apps with the same name** visible to the identity, or a user-assigned
  identity: set `DASHBOARD_HOST_RESOURCE_ID` (the app's full resource id) and/or
  `DASHBOARD_HOST_IDENTITY_CLIENT_ID` on the app.
- **A node on AWS** can't hold the pool: a security group allows 60 inbound rules by
  default, and each address × port is one. The pool is left out there, with a warning,
  and only the detected address is admitted. Host the node on Azure or GCP, which hold
  thousands of addresses per rule, or use option 2.

## Option 2: a NAT gateway

A NAT gateway on the environment's subnet gives every outbound connection **one static
address**. The echo detection then sees the same address the node does, so the
dashboard's normal `/32` detection just works, on every cloud including AWS. This
needs a **workload profiles** environment (the default since 2024; a Consumption-only
environment can't take one). Costs are about $32.85/month for the gateway, $3.65/month
for the Standard public IP, and $0.045 per GB processed.

```bash
az network public-ip create -g RG-DASH -n pip-dash-egress --sku Standard --allocation-method Static
az network nat gateway create -g RG-DASH -n nat-dash --public-ip-addresses pip-dash-egress
az network vnet subnet update -g RG-DASH --vnet-name vnet-dash -n snet-aca --nat-gateway nat-dash
```

Then turn option 1 off so the pool stops being admitted, and re-apply the node
firewall (**Settings → Containers → Re-apply**) or redeploy:

```bash
for app in dash dash-worker; do
  az containerapp update -n $app -g RG-DASH --set-env-vars DASHBOARD_EGRESS_POOL=off
done
```

The address is `az network public-ip show -g RG-DASH -n pip-dash-egress --query
ipAddress -o tsv`, which is also the one to give any other allow-list that needs to
admit the dashboard. To remove the gateway, detach it first (`az network vnet subnet
update ... --remove natGateway`): a NAT gateway can't be deleted while a subnet still
references it.

## Option 3: paste the pool by hand

When you can't grant roles (Contributor alone can't write role assignments) and don't
want the cost of a NAT gateway, copy the pool into `portainer_allowed_source_cidrs` (or
`rancher_allowed_source_cidrs`) in Settings, then **Re-apply**:

```bash
az containerapp show -n dash-worker -g RG-DASH --query "join(',', properties.outboundIpAddresses)" -o tsv
```

That copy *can* go stale when Azure grows the pool. The symptom is the same
`ConnectTimeout` coming back, and the fix is to paste it again.
