# KubeSolo

> **Audience:** operator · **Profile:** `both` · **Read this when:** you need the Entitle agent on a plant-floor or edge host that will not carry a real Kubernetes cluster.

OT customers turn down Kubernetes-based agents for two reasons that have nothing to do
with the agent: the compute a cluster costs, and the burden of maintaining one at a site
with no staff. [KubeSolo](https://kubesolo.io/) removes both — a single-node, etcd-free
distribution whose control plane runs as one process in about 200 MB, and which takes
stock Helm charts unmodified.

That makes it a way to run the **same** BeyondTrust Entitle agent, from the same chart,
on a plant IPC as in the datacenter. This page covers what that install actually looks
like, the two places it is likely to fail, and the one limitation to raise before a
customer finds it.

The plays live in [`examples/playbooks/kubesolo/`](https://github.com/Weaverlab-xyz/vm-dashboard-community/tree/main/examples/playbooks/kubesolo)
and run through [Config Management](config-management.md), against an on-prem host
reached by a [remote agent](remote-agents.md).

## What KubeSolo brings, and what it does not

It bundles CoreDNS, kube-proxy, containerd's default CNI plugins and Rancher's
local-path provisioner, so — unlike a kubeadm cluster and like k3s — **there is no
separate networking step**. Portainer's Cilium walkthrough is an optional upgrade for
network policy, not a prerequisite.

It does **not** ship a kubectl. k3s answers this with `k3s kubectl`; KubeSolo has no
equivalent, and the Config Management runner for an SSH target
(`chrweav/ansible-winrm`) carries neither kubectl nor helm nor `kubernetes.core`. So
`kubesolo-install.yml` installs both clients on the host and every play shells out to
them — the same constraint and the same answer as the
[k3s samples](integrations/ansible/playbooks.md).

State lives under `/var/lib/kubesolo`, with Kine over SQLite standing in for etcd. The
kubeconfig is at `/var/lib/kubesolo/pki/admin/admin.kubeconfig`.

| | |
|---|---|
| Architectures | ARM, ARM64, x86_64, RISC-V 64 |
| Kubernetes line | 1.34 as of KubeSolo v1.1.0 |
| Idle control plane | ~200 MB; 512 MB is the documented floor |
| Practical minimum here | **2 vCPU / 4 GB** — set by the *agent's* 1Gi request, not by KubeSolo |

## Egress: one hostname, two ports, and one of them is not TLS

The Entitle agent token is a base64 JSON blob, and its `routing` field decides the whole
egress profile. Decode yours before quoting anything to a firewall team:

```bash
echo "$TOKEN" | base64 -d | jq 'del(.imageCredentials, .datadogApiKey)'
```

| `routing` | What the host must reach |
|---|---|
| `v1` — current tenants | **`agent.<region>.entitle.io` only**, on 443 and 8080. That host also proxies the container registry, so there is no `ghcr.io` or `gcr.io` egress at all. |
| `v0` — tenants onboarded before 2026-07-07 | The same host, plus direct `ghcr.io` and `gcr.io/datadoghq`. |

Region is `us`, `eu` or `ca`, from the token's `platform` field. Plus DNS.

**Port 8080 is not telemetry and not optional.** It carries `ENTITLE_PROXY_URL`, the
agent's primary channel, and the chart sets it as `http://` — plain HTTP. A security
review will ask about that, so raise it first. Port 443 is the image pull. A
443-only profile is not achievable today.

For a Purdue-model site this places the agent at **L3 / L3.5 with a brokered egress
path**. There is no air-gapped mode; the startup validators fail closed and the pod
enters `CrashLoopBackOff`.

## TLS inspection breaks it in two places, not one

If an inspecting proxy re-signs that connection — Cloudflare Gateway, Zscaler, Netskope
and friends — there are **two independent trust stores** to fix, and skipping the first
produces an error that reads like a blocked port.

| Store | Used by | Fix | Symptom if missed |
|---|---|---|---|
| The node's | containerd, pulling images | `node_ca_pem` in `kubesolo-install.yml` | `ErrImagePull`, `x509: certificate signed by unknown authority` |
| The agent's | the agent's own HTTPS calls | `entitle_agent_ca_bundle_path` in `entitle-agent-install.yml` | validators fail, `Init:CrashLoopBackOff` |

The node's fails first. Fix it first. The agent's bundle must be **combined** — public
CAs *and* the corporate root; the corporate root alone breaks every other call the agent
makes, which is why the play points at the host's own
`/etc/ssl/certs/ca-certificates.crt` after the node store has been updated. It needs
agent image 2.9.10 or newer.

Check for interception before installing anything:

```bash
openssl s_client -connect agent.us.entitle.io:443 -servername agent.us.entitle.io </dev/null 2>/dev/null | openssl x509 -noout -issuer
```

## Why the values file overrides what it does

Read against chart **2.11.0**. The shipped defaults are wrong for a single node in three
ways, and two of them are invisible until you look at the rendered manifest:

- **`agent.replicas` is 3, and 2.11.0 carries no anti-affinity.** All three schedule on
  the one node and reserve 3 CPU / 3Gi of *requests* before doing any work. The play
  sets 1.
- **`datadog.enabled: false` is already the chart default and does not remove Datadog.**
  With `sidecarLogs: true` — also the default — the chart injects a Datadog sidecar
  container into every agent pod, and the Datadog API key rides inside the agent token.
  `datadog.sidecarLogs: false` is the switch that actually drops it. Worth settling
  early: a supported configuration that ships telemetry to a third-party SaaS from
  inside a plant network is something a share of OT prospects will decline on principle.
- **`platform.mode` must be `native`.** The enum is `gcp|aws|azure|native`.

With those three set you get **one pod of two containers** — the `entitle-agent-healthcheck`
init container and `entitle-agent` — and nine objects in total.

**An empty `kubectl logs` is expected, not a fault.** `ENTITLE_LOG_TO_FILE` is gated on
`datadog.enabled`, not on `sidecarLogs`, so with the sidecar off the agent still writes
to a file in an emptyDir that now has no reader:

```bash
kubectl -n entitle exec deploy/entitle-agent -c entitle-agent -- sh -c 'tail -n 200 /var/log/entitle-agent/*'
```

## Where the agent's secrets rest

With `kmsType: kubernetes_secret_manager` — the default — every access token the agent
holds is a Kubernetes Secret, which on KubeSolo means a row in a SQLite file under
`/var/lib/kubesolo`. There is no etcd, and no documented encryption-at-rest
configuration, so **the control is LUKS or a TPM-backed volume on that path**. Decide
that before the agent holds anything real.

If the site runs HashiCorp Vault, `kmsType: hashicorp_vault` with
`externalKmsParams.hashicorp.connectionString` removes the question rather than
mitigating it: the tokens never land on the node.

The chart's RBAC is a namespaced **Role**, nothing cluster-scoped. Within its own
namespace it grants full control of `apps/deployments` — that is the mechanism the agent
self-updates through — read on pods and replicasets, and `'*'` verbs on `secrets` and
`jobs`. Volunteer those two wildcards in a review rather than letting them be found.

## The limitation to raise before a customer finds it

**`imagePullPolicy: Always` is hardcoded in the chart and is not exposed as a values
key.** Every pod restart therefore requires the registry to be reachable — so a node
whose WAN is down cannot restart the agent *even though the image is already in
containerd's store*. It fails as `ErrImagePull`, which reads as a firewall problem.

For an OT site that loses its link for days, this is the finding that decides the
answer. Test it in five minutes rather than discovering it during a power-loss trial:

```bash
sudo iptables -I OUTPUT -d "$(getent hosts agent.us.entitle.io | head -1 | awk '{print $1}')" -j REJECT
kubectl -n entitle rollout restart deploy/entitle-agent
kubectl -n entitle get pods -w
```

The mitigation to reach for is a **site-local registry mirror** via
`agent.image.repository`. The chart supports it explicitly: a non-default repository
turns off the registry-proxy rewrite and passes the pull credentials through unchanged.

Related: pin the agent for change control. Set `entitle_agent_version` to a real version
and leave the image tag alone — the play then renders `HELM_RESTART_POLICY=never`. Left
at `default`, a token carrying `autoUpdate: v1` makes the agent update itself, which no
OT change-control process will accept.

## Running it

Prerequisites, in the order their failures masquerade as each other:

1. **The agent host is granted `agent_ansible` on both sides** — the grant on the Agents
   page, *and* `policy.yaml` carrying `job_types: [agent_ansible]` plus an `ansible:`
   block with `enabled: true` and its own `ansible.targets:` entry covering the guest's
   address on port 22. The top-level `targets:` list is deliberately not consulted.
   `policy.yaml` is written at enrolment, so a change means re-enrolling. See
   [Agent-executed Config Management](remote-agents/config-runs.md).
2. **The guest reports an address** — its hypervisor connection needs
   `sync_guest_details: true`, or the target is listed but disabled with "no address".
3. **The plays are uploaded** to a storage backend. A run resolves an asset by bare
   filename; the repo copy is a sample, not a source.
4. **A credential to reach the guest.** The agent path has no default SSH key — bind a
   Secrets-Management key or a Password Safe managed account, or the run fails
   `Permission denied (publickey)`.

Then:

| Step | Play | Notes |
|---|---|---|
| 1 | `kubesolo-install.yml` | Set `node_ca_pem` if a proxy inspects TLS |
| 2 | `kubesolo-status.yml` | Captures the idle baseline — the first half of the sizing answer |
| 3 | `entitle-agent-install.yml` | Bind the token to `entitle_agent_token` via **Use a secret** |
| 4 | `kubesolo-status.yml` | The delta is the second half |

The token never needs typing. The run form's **Use a secret** panel binds a
Secrets-Management secret to a variable; the dashboard resolves the reference only when
the agent fetches the job bundle, and scrubs the value from job output. Setting
`entitle_agent_token_secret` instead fetches it from Password Safe mid-run. Either way
the play hands it to helm through a 0600 values file rather than `--set`, because
`--set` would put it in argv where `ps` shows it to every local user on the node. See
[Secrets in a run](integrations/ansible/secrets.md).

## See also

- [Config Management](config-management.md) — the run form, targets and runners
- [Remote Agents](remote-agents.md) — reaching an on-prem host at all
- [Entitle](integrations/entitle.md) — the integration this agent serves
- [OT Demo Cell](profiles/demo/ot-demo-cell.md) — the demo cell and protocol simulators this sits beside
- [Kubernetes](kubernetes.md) — the managed-cluster path, where the agent install is a button
