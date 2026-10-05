# k3s

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are building an on-prem cluster with the dashboard, or working with one of the k3s clusters it creates for you in the OT demo cell or the Workload Lab.

Part of [Kubernetes](../kubernetes.md).

k3s is the self-managed Kubernetes the dashboard builds wherever there is no cloud
control plane to lean on. It is a single binary with a bundled CNI, and it has two
properties the dashboard depends on:

- **It takes arbitrary API server flags.** `--authentication-config` is a kube-apiserver
  flag that EKS, AKS and GKE do not expose, so only a self-managed cluster can accept Dex
  ID tokens for people, SPIFFE JWT-SVIDs for workloads, or the dashboard's own identity.
- **It runs beside Docker.** k3s keeps its own containerd and CNI and does not need the
  host to be Docker-free, so a host can carry Kubernetes workloads and plain containers at
  once.

The dashboard creates k3s in three places.

| Where | What it is | Built by |
|---|---|---|
| [On-prem clusters](#on-prem-clusters) | A cluster on your own hosts, registered as `cloud=local` | Config Management plays in `examples/playbooks/k3s/` |
| [The OT demo cell](#the-ot-demo-cell-and-its-dmz-broker) | The plant IPC's single-node cluster, and the DMZ broker's | The `ot-sim` image bake, air-gapped |
| [The Workload Lab](#the-workload-labs-spire-lab) | The SPIRE lab's Kubernetes deployment mode, and the k3s node a lab links to | The SPIRE lab's own runs |

## On-prem clusters

The dashboard's Terraform modules are cloud-only and Rancher is import-only, so an on-prem
cluster is built with the `hosts: all`, `become: true` plays in
[`examples/playbooks/k3s/`](../../examples/playbooks/k3s/), run from
[Config Management](../operations/config-management.md) one host at a time:

1. `k3s-open-ports.yml` on every node (6443/tcp, 8472/udp, 10250/tcp, plus etcd for HA).
2. `k3s-server-init.yml` on the first server. It gives you `server_url` and the node token.
3. `k3s-join.yml` on each remaining node, as `agent` (the default) or as another `server`.
4. `k3s-status.yml` on a server, to confirm every node registered.
5. `k3s-kubeconfig.yml` on a server, to collect the admin kubeconfig for registration.
6. `k3s-dex-auth.yml` on each server. **People reach an on-prem cluster only through
   [Dex](../oidc/dex.md#dex-for-on-prem-clusters)**, so this step is required, not optional.

Then register the cluster on the Kubernetes page with that kubeconfig, as `cloud=local`.
From there it is a Config Management target like any other, with one difference: its runs
execute in a sibling container **on the dashboard host**, because an on-prem API address is
reachable from your LAN and not from an in-cloud runner. That host needs a `docker` CLI and
a route to the cluster.

Things to know before relying on it:

- **The registered kubeconfig is standing cluster-admin.** It is stored and used verbatim,
  and k3s's admin kubeconfig is a client certificate you cannot revoke without re-issuing
  the cluster CA. Every run against the cluster authenticates with it. Past a lab, either
  register a kubeconfig built from a scoped ServiceAccount token, or let the cluster trust
  [the dashboard's own identity](../oidc/dashboard-identity.md#on-prem-k3s)
  (`k3s-dashboard-auth.yml`), which replaces the certificate with a thirty-minute token
  minted per call.
- **The install needs egress.** `k3s-server-init.yml` fetches `get.k3s.io`; air-gapped
  on-prem installs are out of scope for these plays (the OT cell's bake, below, is the
  air-gapped path).
- **Rancher** imports a registered cluster fine, but its firewall only auto-allows clusters
  the dashboard provisioned. Add your site's NAT address to `rancher_allowed_source_cidrs`.

The full play reference, including the node token and kubeconfig routed through Password
Safe, is in the [playbooks README](../../examples/playbooks/README.md#kubernetes-k3s-k3s).

## The OT demo cell and its DMZ broker

The [OT demo cell](../profiles/demo/ot-demo-cell.md) is a plant IPC running single-node k3s:
its PLC simulators and the FUXA HMI are Deployments in the `ot-sim` namespace, and its API
on :6443 is one more endpoint PRA can broker, as the `ot-<cell>-k3s` tunnel. Docker stays on
the cell beside it. The cell's DMZ broker runs the same k3s, carrying the Entitle agent and
an OpenFaaS function runtime and nothing else.

Both are baked by `provisioners/ot/ot-sim-debian.sh`, and both boot with no egress: the
bake fetches the pinned k3s binary and its air-gap image bundle, and every workload image
is a tarball k3s imports at start. See [The cell runs on k3s](../profiles/demo/ot-demo-cell.md#the-cell-runs-on-k3s).

## The Workload Lab's SPIRE lab

The [SPIRE lab](../workload-lab/spiffe.md) uses k3s twice:

- **The `k8s` deployment mode** runs k3s on the lab host (`k3s-server-init.yml`) and SPIRE
  on top of it from the hardened Helm charts. See
  [Deployment modes](../workload-lab/spiffe.md#deployment-modes-vm-docker-or-kubernetes).
- **The linked k3s node** is a second VM where a SPIRE-attested workload reaches the
  Kubernetes API with a JWT-SVID instead of a stored token. `k3s-spiffe-auth.yml` makes the
  API server accept those tokens, and it can only do so because k3s takes the
  authentication flag. See
  [Reaching a Kubernetes cluster with a JWT-SVID](../workload-lab/spiffe.md#reaching-a-kubernetes-cluster-with-a-jwt-svid)
  and [Why it is k3s](../workload-lab/spiffe.md#why-it-is-k3s-and-why-that-is-a-real-limit).

`k3s-spiffe-auth.yml`, `k3s-dex-auth.yml` and `k3s-dashboard-auth.yml` share one
`AuthenticationConfiguration`. Each replaces only its own entry, so any of them can be run,
re-run or undone without touching the others.

## See also

- [Managed Kubernetes](k8s.md): the cloud clusters, and the federation k3s does not need
- [KubeSolo](kubesolo.md): single-node Kubernetes for a host that runs nothing but the agent
- [Dex](../oidc/dex.md): the issuer people sign in to an on-prem cluster with
