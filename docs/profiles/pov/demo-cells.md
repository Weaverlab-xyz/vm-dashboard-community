# Demo cells in a POV

> **Audience:** operator · **Profile:** `pov` · **Read this when:** a customer wants to see the OT or network-device story on their own POV rather than on your estate instance.

Part of [A POV Instance](README.md). This page covers how the demo profile's cells carry over to a POV, and what changes when they do.

## Why a cell is not copied straight across

On the demo profile, a cell such as the [OT Demo Cell](../demo/ot-demo-cell.md) or the
[Network Demo Cell](../demo/net-demo-cell.md) handles everything itself:

- it deploys its own VMs;
- it wires them into PRA through the install's **global** tenant and the **shared**
  Gateway host;
- it keeps its state on the deploy job.

A POV does none of those things. Its VMs come from a template, its PRA tenant comes from
[the tenant registry](standing-one-up.md#the-tenant-registry), its Gateway and Jump Group
belong to that POV alone, and destroying the POV tears everything down in one ordered pass.

So a cell is not ported into a POV. Each of its parts goes where a POV already keeps that
kind of thing:

| Cell part | Where it lives on a POV |
|---|---|
| The image (the OT simulator or VyOS) | A VM in the POV's template, marked with a **cell role** |
| The extra PRA items (HMI Web Jump, protocol tunnels) | The ordinary [wire-up](wiring.md), which builds them for any guest with a cell role |
| The story | Cards on the POV's **Use cases** tab |

## The cell roles

| Role | What the wire-up adds on top of the normal jump item |
|---|---|
| `ot-sim`, the OT simulator | A **Web Jump** to the FUXA HMI on port 1881. One **PRA protocol tunnel** each for Modbus TCP (502), OPC UA (4840), Siemens S7comm (102) and EtherNet/IP (44818). |
| `vyos`, the network device | No extra items, because the Shell Jump is the demo. The guest is **left out of Entitle**: VyOS rebuilds its user accounts from its own config on every commit, so an account Entitle created over SSH would vanish the first time anyone changed the device. |
| `ot-broker`, the plant's DMZ broker | No extra items. It is the host for [the OT HMI adapter](#just-in-time-hmi-access-through-entitle) and for the POV's Entitle agent. |

Every role needs a Linux guest. A role cannot be set on the broker VM, because the agent, the
Gateway and the Resource Broker all run there.

All of these items go into the POV's own Jump Group, behind the POV's own Gateway. A vendor
scoped to that group can therefore reach the HMI and the PLC, and nothing from any other
POV. The item names follow the POV's managed-system naming,
`<pov>-<vm>-hmi` and `<pov>-<vm>-<protocol>`, so two POVs cloned from the same template
never collide.

## Setting a role

**On a cloud POV**, set **Cell role** on the VM in the cloud template. When the POV is
created, the role is copied onto that VM. It is copied once, and never over a value an
operator has already set.

**On any POV**, including a Skytap POV, which has no template stored in the dashboard, set
it per guest:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"cell_role": "ot-sim"}' https://dashboard.example/api/pov/managed/<env-id>/vms/<vm-id>/cell-role
```

Send `""` to make the guest an ordinary target again. While a guest's role items still
exist in PRA, its role cannot be changed, because teardown asks the role which items to
remove. The items are removed when the POV is destroyed.

Then press **Wire up**. Running it again builds only the items that are missing.

## The images

The guest has to actually be the cell:

- **OT simulator.** Bake the `ot-sim` image as described in
  [provisioners/ot/README.md](../../../provisioners/ot/README.md), using the default k3s
  runtime. The docker runtime serves no S7comm or EtherNet/IP, so those tunnels would point
  at closed ports.
- **VyOS.** Bake `vyos-cell` from a VyOS image you supply, as described in
  [provisioners/net/README.md](../../../provisioners/net/README.md).
- **OT DMZ broker.** Bake the same `ot-sim` provisioner with `OT_ROLE=broker`. That image
  carries k3s, the Entitle agent chart and the OpenFaaS runtime the adapter runs on.

For a cloud POV, promote the image into the POV's cloud and region, then reference it from
the template VM. A **Skytap** POV runs no user-data, so the image has to be baked into a
Skytap template guest. Installing it after boot is not an option.

## Just-in-time HMI access through Entitle

The demo cell's DMZ broker runs an Entitle adapter for the FUXA HMI. An Entitle grant
creates an HMI user that exists only for the grant, and hands the requester the PRA Web
Jump that reaches the HMI. A POV carries this as the **OT HMI access through Entitle** step
on the **Setup** tab, shown only on a POV with an `ot-sim` guest and an Entitle tenant.

Before pressing it you need:

1. A guest with the `ot-broker` role. Name it `entitle` in the template, and the Entitle
   agent step installs onto it without being told.
2. The **Entitle agent** installed on that same guest. The adapter's address resolves only
   inside that guest's k3s, and Entitle reaches it through the agent.
3. **Wire up** run, so the HMI Web Jump exists. The grant hands the requester that Web Jump.
4. The two plays staged in your storage backend under their own names:
   `fuxa-admin-rotate.yml` and `openfaas-function-deploy.yml`, from
   [examples/playbooks/ot/](../../../examples/playbooks/ot/README.md). This is the same
   requirement the demo cell has.

**Deploy adapter** queues two runs on the POV's broker agent, against the `ot-broker`
guest. The first rotates the HMI's admin password off FUXA's seeded default. The second
deploys the adapter, holding that password. It then registers the adapter in the POV's
Entitle tenant as an agent-brokered integration.

It deploys in **dry run**. Each grant reports what it would do without touching the HMI.
When the dry run looks right, press **Go live** on the same step to redeploy with real
grants. Pressing either again is safe: the password and the adapter's credential are
minted once, and the integration is registered once.

Destroying the POV removes the integration from the Entitle tenant before it removes the
agent token, and deletes the adapter's stored credentials. If the Entitle tenant cannot be
reached, the destroy log names the integration to delete by hand.

## Purdue zones

By default a cloud POV is one subnet, and every guest can reach every other guest. Tick
**Purdue zones** on a cloud template to build three subnets instead, with the traffic
between them limited the way the OT demo cell limits it. A guest's zone follows its cell
role; there is no separate zone field to get wrong.

| Zone | Who is in it | Reachable from | Can reach |
|---|---|---|---|
| Enterprise | The POV broker (dashboard agent, PRA Gateway, Resource Broker) and every guest with no OT role | Itself; SSH from the DMZ, for the Entitle agent | Anywhere |
| DMZ | The `ot-broker` guest (Entitle agent, HMI adapter) | Itself; SSH from enterprise, for the broker agent's runs | The POV network, plus HTTPS, port 8080 and DNS outside it |
| Plant | The `ot-sim` guest | Itself; from enterprise on SSH, the HMI and the PLC protocol ports; from the DMZ on SSH and the HMI | The POV network only. **No internet and no public address.** |

The enterprise zone takes the first /24 of the network, the subnet an unzoned POV would
have used; the DMZ and plant take the next two. So a zoned template's network has to be
/22 or larger. The default /16 is.

The rules are built once, in `services/pov_zones.py`, and each cloud expresses them in its
own firewall: security groups on AWS, a network security group per subnet on Azure,
firewall rules on per-zone instance tags on GCP, and a security list per subnet on OCI.
On every cloud the plant's subnet gets no public addresses, and on OCI its route table
has no route out at all.

Zones are fixed when the POV is created. Ticking the box on a template does not rezone a
POV already built from it; destroy and recreate the POV.

## What does not carry over, and why

- **The PRA Vault checkout the demo cell builds for its admin account.** A POV already
  brings Password Safe accounts into PRA through its
  [Password Safe credentials in PRA](wiring.md) step. A second path would create a second
  Vault account for the same credential.
- **The Agent demo cell.** It runs against the SE's own dashboard rather than anything in
  the customer's environment, so it has no POV equivalent.

## The use-case cards

The POV's **Use cases** tab includes three PRA cards that need a guest with a cell role:

- **Operate the plant HMI through a recorded Web Jump** (`ot-sim`).
- **Reach a PLC over Modbus or OPC UA, brokered** (`ot-sim`).
- **Make an emergency firewall change on a network device** (`vyos`).

The **Entitle** group adds **Just-in-time access to the plant HMI**, which needs both an
`ot-sim` and an `ot-broker` guest. Whether its adapter is deployed is the setup step's to
say.

On a POV with no such guest these cards show as **out of scope**, the same as a product the
POV does not include. They do not count against the POV's progress.
