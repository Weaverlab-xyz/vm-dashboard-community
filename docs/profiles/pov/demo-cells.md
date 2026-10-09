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

Both roles need a Linux guest. A role cannot be set on the broker VM, because the agent, the
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

For a cloud POV, promote the image into the POV's cloud and region, then reference it from
the template VM. A **Skytap** POV runs no user-data, so the image has to be baked into a
Skytap template guest. Installing it after boot is not an option.

## What does not carry over, and why

- **The Purdue-zone firewall and the DMZ broker.** A POV's network is a single address
  range today. The story still holds without them: the plant is reachable only through the
  POV's Gateway. Multi-zone POV templates are planned for a later release.
- **The Entitle plant agent and the FUXA function adapter.** These depend on the DMZ broker
  above.
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

On a POV with no such guest these cards show as **out of scope**, the same as a product the
POV does not include. They do not count against the POV's progress.
