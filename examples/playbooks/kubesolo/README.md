# KubeSolo + Entitle agent

Single-node Kubernetes on an edge or plant-floor host, and the BeyondTrust Entitle
agent on top of it. Three `hosts: all`, `become: true` plays here, and the agent's two in
[`../ot/`](../ot/README.md), run through Config
Management against an on-prem host reached by a remote agent.

The operator-facing write-up — egress, trust stores, sizing, and the limitation worth
raising before a customer finds it — is [docs/kubernetes/kubesolo.md](../../../docs/kubernetes/kubesolo.md).
This file is the quick reference.

The [OT demo cell](../../../docs/profiles/demo/ot-demo-cell.md) does **not** run
KubeSolo: its cell and broker run k3s, which keeps Docker on the host. These plays are for
the edge and plant-floor hosts where KubeSolo is the right fit.

The Entitle agent plays are shared with the OT broker and live in [`../ot/`](../ot/README.md):
`entitle-agent-install.yml` and `entitle-agent-uninstall.yml` find KubeSolo's kubeconfig
themselves. That README covers the token, the variables and the egress probe.

| File | Purpose |
|---|---|
| `kubesolo-install.yml` | Install KubeSolo, helm and kubectl; optionally trust a corporate root CA first |
| `kubesolo-status.yml` | Read-only — node, pods, footprint, agent release, image pull policy |
| `../ot/entitle-agent-install.yml` | Install the Entitle agent chart with single-node values |
| `../ot/entitle-agent-uninstall.yml` | Remove the release (`confirm: true` required) |
| `kubesolo-uninstall.yml` | Remove KubeSolo and its state (`confirm: true` required) |

## Order

```
kubesolo-install.yml  →  kubesolo-status.yml  →  ../ot/entitle-agent-install.yml  →  kubesolo-status.yml
```

The two status runs bracket the install, and the difference between them is the sizing
answer: KubeSolo idles at about 200 MB, so the agent's own 1Gi request is what actually
sets the floor. Expect **2 vCPU / 4 GB** to be comfortable.

## Variables worth knowing

`kubesolo-install.yml`

| Var | Default | Notes |
|---|---|---|
| `kubesolo_version` | `""` | blank = latest |
| `kubesolo_proxy` | `""` | corporate proxy for the install |
| `kubesolo_path` | `/var/lib/kubesolo` | state dir; the kubeconfig lives under it |
| `node_ca_pem` | `""` | a corporate root CA as PEM **text**, for the NODE's trust store |
| `helm_version` | `v3.16.3` | |
| `kubectl_version` | `""` | blank = whatever dl.k8s.io calls stable |

## Invariants

`tests/test_playbook_kubesolo.py` pins the hand-rolled idempotency (these plays shell
out, so Ansible gives none for free).
