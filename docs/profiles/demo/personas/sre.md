# Platform / SRE

Owns the platform other teams build on: Kubernetes clusters, the container runtime, the
management plane. Their privilege is unusual in shape — broad rather than deep. Cluster-admin
on one cluster is not especially dangerous; cluster-admin on all of them, permanently, is.

Their access problem is **the incident**. During one, an engineer needs real privilege
immediately, and any process that adds minutes will be bypassed. So the privilege gets granted
permanently, "for next time", and next time never audits it.

## Why they care

Kubernetes RBAC is expressive and nobody's is tidy. Bindings accumulate, service accounts
outlive the workloads that needed them, and the cluster has no way to say "this binding was for
an incident in April". Meanwhile the tokens those service accounts hold do not expire either.

The version of this story that lands is not tighter RBAC. It is RBAC that **removes itself**,
so tightening it does not cost anyone an outage.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Clusters and container platforms, and the management plane that reaches them. |
| **PRA** | A tunnel to a private API server, so "no public endpoint" does not mean a bastion nobody patches. |
| **Password Safe** | Owns and rotates the ServiceAccount token — the non-human credential nobody else will touch. |
| **Entitle** | The binding that names the requester and expires with the incident. |

## Use cases

### kubectl that expires

An engineer requests cluster access for the length of an incident and gets an RBAC binding named
after them, which removes itself afterwards. No permanent cluster-admin group.

**Guide:** [Entra → Kubernetes federation](../../../integrations/entra-k8s-federation.md)

### Reach a private cluster API server

Run kubectl against a cluster with no public endpoint, through a brokered tunnel.

**Guide:** [Kubernetes](../../../kubernetes.md)

### Rotate a ServiceAccount token

The long-lived token in a CI system or a sidecar, rotated on a schedule with the consumer
picking up the new value.

**Guide:** [ServiceAccount token rotation](../../../design/k8s-sa-token-rotation.md)

### One group, every cluster

Federate cluster access to the corporate directory, so joining a team grants the right access
everywhere and leaving revokes it — instead of per-cluster identity nobody deprovisions.

**Guide:** [Entra → Kubernetes federation](../../../integrations/entra-k8s-federation.md)

### A container platform with a vaulted admin

Stand up a Portainer or Rancher node and reach its UI by Web Jump with the admin credential
injected — the platform console that usually has a shared login.

**Guide:** [Rancher](../../../integrations/rancher.md) · [Portainer](../../../integrations/portainer.md)

## What to enable

**Kubernetes**, **Entitle** and **Privileged Remote Access**. Add **Password Safe** for the
token-rotation card and **Portainer** for the container-platform card.

Kubernetes is demo-owned, so this focus **needs a demo instance**; on a
[POV instance](../../pov/README.md) its cards report as unavailable by design.

## Talking to this buyer

They will test whether you understand that an expired binding must not interrupt a running
workload — human access and workload identity are different problems, and conflating them is the
fastest way to lose them. Be explicit that the JIT story is about *people*, and that the
service-account story is rotation with a consumer that picks up the new value.
