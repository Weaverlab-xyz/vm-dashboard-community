# POVs on Azure

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are running POVs on Azure and need to know where it differs from AWS.

Part of [Running POVs on a public cloud](../public-cloud.md).

Azure is the second provider, and it maps onto this feature more naturally than AWS
because it has the primitive AWS lacks: **a resource group is an environment.** One per
POV, named `povenv-<name>`, and destroying the POV is a single `begin_delete` on the group
that takes the VMs, their managed disks, NICs, public addresses, the VNet and the NSG with
it, in ARM's own dependency order. The AWS driver unpicks six resource types by hand for
the same result.

Give the service principal **Contributor** on its own subscription, or on a scope where it
can create resource groups — or the narrower custom roles in
[`terraform/pov_cloud_azure_role`](../../../../terraform/pov_cloud_azure_role/main.tf), below.

| | AWS | Azure |
|---|---|---|
| Environment | A tag on every resource | A resource group |
| Teardown | Six resource types, in order | One call |
| Listing | Per region | Subscription-wide, so the orphan sweep is complete |
| Platform login | Windows: EC2's generated Administrator, read with the POV's own key. Linux: none | **Generated per POV** — see below |
| Suspend | `StopInstances` | `begin_deallocate`, never power-off |
| Private address | Persists across stop/start | **Made static**, because it does not |

**Azure POVs have one platform login; AWS Windows guests each have their own.** EC2
generates each Windows instance's Administrator password at first boot, and the dashboard
decrypts it with the POV's own key. Those logins differ per guest, so Password Safe's
functional account on AWS is a local account the wire-up creates instead. See
[AWS Windows guests](../wiring.md#aws-windows-guests-the-dashboard-creates-the-functional-account-on-each-guest).
AWS Linux guests still have none. Azure's case is different: its
`os_profile` requires an admin account at VM creation, for Linux as well as Windows, so a
POV built there has one whether anybody wanted it or not. The dashboard generates it,
stores it encrypted alongside the POV's other secrets, and puts the same account on every
VM in the environment. That is why Azure's `stored_credentials` capability is True and
AWS's is False, and it means the Resource Broker install has the login it needs without
anybody pasting one.

**The two Azure-specific traps this driver exists to avoid**, both of which fail quietly:

- **Power-off is not suspend.** `begin_power_off` leaves a VM "Stopped" and still billing
  for its compute. Only `begin_deallocate` reaches "Stopped (deallocated)". A nightly
  schedule making that mistake would cost the full compute bill while the page reported
  every POV asleep.
- **Private addresses are allocated statically here**, unlike on the other three clouds. A
  deallocated Azure VM with a dynamic private address can return on a different one — and
  by then the wire-up has written the old address into a PRA jump item, a Password Safe
  managed system and an Entitle integration. Every scheduled suspend would silently
  invalidate all three.

The network security group carries **no custom rules**, deliberately: Azure's defaults
already allow the VNet to talk to itself, deny everything inbound from outside it, and
allow outbound. Restating them would be three more things to keep correct and no change in
behaviour.

Template images take either a marketplace URN (`publisher:offer:sku:version`) or the
resource id of a managed or gallery image. Anything else is refused by name — ARM's own
error for a malformed reference names neither the field nor the value.
