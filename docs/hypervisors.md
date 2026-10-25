# Hypervisors

> **Audience:** operator · **Profile:** `both` · **Read this when:** your VMs run on a hypervisor (VMware, Proxmox, Hyper-V, Nutanix or XCP-ng) and you want to know what the dashboard does with it.

The on-premises virtualisation platforms the dashboard manages, one page each: the same
pages the app's **Hypervisors** menu leads to. Each is off until you enable it in Settings
→ Integrations → Hypervisors.

| Page | Read it when |
|---|---|
| [VMware Workstation](hypervisors/vmware.md) | you are running VMs on VMware Workstation on this machine |
| [VMware vSphere / ESXi](hypervisors/vsphere.md) | your VMs live on vSphere or a standalone ESXi host |
| [Proxmox VE](hypervisors/proxmox.md) | your VMs live on Proxmox VE |
| [Microsoft Hyper-V](hypervisors/hyperv.md) | your VMs live on Hyper-V and you want them managed from here |
| [Nutanix AHV](hypervisors/nutanix.md) | your VMs live on Nutanix AHV |
| [XCP-ng / XenServer](hypervisors/xcpng.md) | your VMs live on XCP-ng or XenServer |

Related:
- [Remote Agents](platform/spiffe/remote-agents.md): when the hypervisor sits in a network the dashboard cannot reach;
- [Inventory](operations/inventory.md): every hypervisor VM in one list, beside the clouds'.
