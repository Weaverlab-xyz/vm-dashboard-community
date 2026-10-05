# OT demo: plays the dashboard runs on the plant's DMZ broker

The [OT demo cell](../../../docs/profiles/demo/ot-demo-cell.md) deploys a second machine,
the plant's DMZ broker, running **k3s** with the BeyondTrust Entitle agent and an OpenFaaS
function runtime. These are the plays the cell's deploy queues against it, as ordinary
Config-Management runs. You can also run them by hand.

Each play finds the cluster itself: k3s's `/etc/rancher/k3s/k3s.yaml` first, then
KubeSolo's admin kubeconfig. So the agent plays also work on a standalone KubeSolo edge
host built with [`../kubesolo/`](../kubesolo/README.md).

| File | Purpose |
|---|---|
| `entitle-agent-install.yml` | Install the Entitle agent chart with single-node values |
| `entitle-agent-uninstall.yml` | Remove the release (`confirm: true` required) |
| `openfaas-function-deploy.yml` | Deploy an Entitle REST adapter onto the broker's OpenFaaS runtime |
| `fuxa-admin-rotate.yml` | Rotate the cell HMI's admin password, from the broker |

The filenames are part of the contract: `services/ot_service.py` and
`services/ot_faas_service.py` queue them by name.

## The Entitle agent on the broker

`entitle-agent-install.yml` is what the cell's deploy runs **for you** against the broker. Two things in it exist because of that host, and both help a real
air-gapped site too:

- `entitle_agent_chart` accepts an **absolute path** to a chart archive already on the
  host (the broker bakes one), so the install needs no Helm repo — `--repo` is omitted
  when the chart is a path;
- `entitle_probe_endpoint` / `entitle_probe_ssh_target` run a **pre-flight egress probe
  from a pod** before helm: DNS, 443, 8080, then the target host's 22. The agent is a
  pod, so proving the path from the host would answer a different question — and a
  closed path becomes one legible failure instead of a `CrashLoopBackOff` three layers
  down. Set `entitle_probe: false` to skip it.
- `entitle_probe_only: true` runs **only** that probe and ends the play — no token, no
  chart, no change to the cluster. It is the answer to "can this host reach anything
  else?" on demand, and because it installs nothing it is safe to run against a broker
  whose agent is healthy, and possible on one whose agent never installed at all. The
  OT demo cell's card exposes it as *Probe egress*.

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

`entitle-agent-install.yml`

| Var | Default | Notes |
|---|---|---|
| `entitle_agent_token` | `""` | bind via "Use a secret" |
| `entitle_agent_token_secret` | `""` | or a Password Safe path |
| `entitle_agent_version` | `default` | **pin a real version for OT change control**; left at `default`, a token with `autoUpdate: v1` self-updates the agent |
| `entitle_agent_chart_version` | `""` | blank = latest |
| `entitle_agent_ca_bundle_path` | `""` | a COMBINED bundle on the host for the AGENT's trust store — a different store from the node's own |
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

`tests/test_playbook_ot.py` pins these plays' cluster discovery, and
`tests/test_playbook_kubesolo.py` the hand-rolled idempotency and the three chart values
that are load-bearing findings rather than preferences. If you change
`entitle_agent_replicas`, `datadog.sidecarLogs` or `platform.mode`, those tests and
[docs/kubernetes/kubesolo.md](../../../docs/kubernetes/kubesolo.md) have to move with you.
