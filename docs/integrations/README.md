# Integrations

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are connecting the dashboard to something you already run, and want that system's prerequisites and config keys.

One page per external system. Each covers what the integration does, what it needs from
you before it will work, the config keys it reads, and how it fails.

Two groups worth telling apart. The **BeyondTrust** pages are the four layers the
dashboard puts on top of anything it provisions — a route in, a vaulted credential, a
just-in-time grant, and the endpoint agent. The **platform** pages are places
infrastructure lives, or things that run work on it.

## BeyondTrust

| Page | Read this when |
|---|---|
| [BeyondTrust Integrations](beyondtrust.md) | you are wiring the dashboard into BeyondTrust and want to know which product does which job. |
| [Privileged Remote Access](privileged-remote-access.md) | you want sessions onto provisioned resources brokered and recorded rather than routed. |
| [Password Safe](password-safe.md) | you want credentials vaulted and rotated rather than stored by this dashboard. |
| [Gateways](gateways.md) | a resource sits in a private network and something has to broker a session into it. |
| [Entitle](entitle.md) | you want access to expire on its own instead of being revoked by someone remembering. |
| [Entitle dashboard permissions](entitle-dashboard-permissions.md) | you want dashboard access without standing admins, or you need to tell the two mechanisms apart. |
| [Workload Credentials](workload-credentials.md) | a workload needs a credential minted at run time rather than one stored for it. |
| [EPM for Linux](epml.md) | you are rolling Endpoint Privilege Management for Linux onto provisioned hosts. |

## On-premises hypervisors

| Page | Read this when |
|---|---|
| [Hyper-V](hyperv.md) | your VMs live on Hyper-V and you want them managed from here. |
| [vSphere / ESXi](vsphere.md) | your VMs live on vSphere or a standalone ESXi host. |
| [Proxmox VE](proxmox.md) | your VMs live on Proxmox VE. |
| [Nutanix AHV](nutanix.md) | your VMs live on Nutanix AHV. |
| [XCP-ng / XenServer](xcpng.md) | your VMs live on XCP-ng or XenServer. |
| [VMware Workstation](vmware.md) | you are running VMs on VMware Workstation on this machine. |

Anything the dashboard cannot route to is reached through an agent instead — see
[Remote Agents](../remote-agents.md).

## Runners, platforms and access

| Page | Read this when |
|---|---|
| [Remote Worker](ansible.md) | you are configuring the runner images that execute Config Management, k8s and promote jobs. |
| [Cloud Functions (preview)](cloud-functions.md) | you need a stable HTTPS endpoint external systems can call to act inside your network. |
| [Rancher](rancher.md) | you have more than a couple of clusters and want one place to see them. |
| [Portainer](portainer.md) | you already run Portainer and want the dashboard to drive it. |
| [Entra → Kubernetes federation](entra-k8s-federation.md) | you want people signing in to clusters as themselves rather than sharing a kubeconfig. |
| [Generic OIDC (SSO)](oidc.md) | you want single sign-on for the dashboard instead of local passwords. |
| [MCP server](mcp-server.md) | you want an AI client to drive the dashboard through its own API. |
| [Deploy Docker Compose to the cloud](cloud-compose.md) | you followed an old link — this page has moved. |

The one POV-only integration, Skytap, lives with the rest of the POV material at
[profiles/pov/skytap.md](../profiles/pov/skytap.md): Settings refuses to enable it on a
demo instance, so it is not a choice available here.
