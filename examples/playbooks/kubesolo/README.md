# KubeSolo + Entitle agent

Single-node Kubernetes on an edge or plant-floor host, and the BeyondTrust Entitle
agent on top of it. Five `hosts: all`, `become: true` plays, run through Config
Management against an on-prem host reached by a remote agent.

The operator-facing write-up — egress, trust stores, sizing, and the limitation worth
raising before a customer finds it — is [docs/kubesolo.md](../../../docs/kubesolo.md).
This file is the quick reference.

| File | Purpose |
|---|---|
| `kubesolo-install.yml` | Install KubeSolo, helm and kubectl; optionally trust a corporate root CA first |
| `kubesolo-status.yml` | Read-only — node, pods, footprint, agent release, image pull policy |
| `entitle-agent-install.yml` | Install the Entitle agent chart with single-node values |
| `entitle-agent-uninstall.yml` | Remove the release (`confirm: true` required) |
| `kubesolo-uninstall.yml` | Remove KubeSolo and its state (`confirm: true` required) |

## Order

```
kubesolo-install.yml  →  kubesolo-status.yml  →  entitle-agent-install.yml  →  kubesolo-status.yml
```

The two status runs bracket the install, and the difference between them is the sizing
answer: KubeSolo idles at about 200 MB, so the agent's own 1Gi request is what actually
sets the floor. Expect **2 vCPU / 4 GB** to be comfortable.

## The token

Never type it into Extra Vars. Two supported paths:

- **Run form → "Use a secret"**, mapping a Secrets-Management secret to the variable
  `entitle_agent_token`. The dashboard resolves the reference only when the agent
  fetches the job bundle, and scrubs the value from job output. Needs `secrets:use`.
- **`entitle_agent_token_secret`** set to a Password Safe SECRET path (`folder/title`),
  fetched mid-run by the play itself. Leave it blank and the play behaves as before.

Either way the token reaches helm through a 0600 values file that is deleted in
`always`, never `--set` — `--set` puts it in argv, where `ps` shows it to every local
user on the node.

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

`entitle-agent-install.yml`

| Var | Default | Notes |
|---|---|---|
| `entitle_agent_token` | `""` | bind via "Use a secret" |
| `entitle_agent_token_secret` | `""` | or a Password Safe path |
| `entitle_agent_version` | `default` | **pin a real version for OT change control**; left at `default`, a token with `autoUpdate: v1` self-updates the agent |
| `entitle_agent_chart_version` | `""` | blank = latest |
| `entitle_agent_ca_bundle_path` | `""` | a COMBINED bundle on the host for the AGENT's trust store — a different store from `node_ca_pem` |
| `entitle_agent_replicas` | `1` | the chart defaults to 3, with no anti-affinity |
| `entitle_agent_kms_type` | `kubernetes_secret_manager` | `hashicorp_vault` keeps tokens off the node entirely |

## Two things that look like faults and are not

**An empty `kubectl logs`.** `ENTITLE_LOG_TO_FILE` is gated on `datadog.enabled`, not on
`sidecarLogs`, so with the Datadog sidecar off the agent writes to a file nothing reads:

```
kubectl -n entitle exec deploy/entitle-agent -c entitle-agent -- sh -c 'tail -n 200 /var/log/entitle-agent/*'
```

**`ErrImagePull` after a restart with no network.** `imagePullPolicy: Always` is
hardcoded in the chart, so a cached image does not help. That is the behaviour, not a
misconfiguration — see the docs page for the mitigation.

## Invariants

`tests/test_playbook_kubesolo.py` pins the hand-rolled idempotency (these plays shell
out, so Ansible gives none for free) and the three chart values that are load-bearing
findings rather than preferences. If you change `entitle_agent_replicas`,
`datadog.sidecarLogs` or `platform.mode`, that test and
[docs/kubesolo.md](../../../docs/kubesolo.md) both have to move with you.
