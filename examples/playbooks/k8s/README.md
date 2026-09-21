# Kubernetes samples (`k8s/`)

Six localhost plays (`- hosts: localhost`, `connection: local`) using `kubernetes.core`,
and they split into two groups that differ in **where the credential comes from**.

Feature reference: [docs/workload-lab/kubernetes.md](../../../docs/workload-lab/kubernetes.md).

| File | What it does | How it authenticates |
|---|---|---|
| `list-nodes.yml` | Read-only smoke test — list node names via `k8s_info` | the injected kubeconfig |
| `namespace-ensure.yml` | Create a namespace (`k8s_namespace`) | the injected kubeconfig |
| `deployment-apply.yml` | Apply a sample nginx Deployment + Service | the injected kubeconfig |
| `helm-install.yml` | `helm upgrade --install` a chart | the injected kubeconfig |
| `ci-deploy-with-ps-token.yml` | Deploy as a namespace-scoped Deployer, then assert another namespace is refused | **a ServiceAccount token it fetched from Password Safe** |
| `ci-read-with-ps-token.yml` | Read cluster-wide as a Reader, then assert a Secret is refused | **a ServiceAccount token it fetched from Password Safe** |

All six **always run on the in-cloud runner** (ECS / ACI / Cloud Run) so they reach a
private API server and bypass the corporate TLS-inspecting proxy, on the
`chrweav/ansible-cloud` image (kubernetes.core + the helm CLI) rather than `ansible-winrm`.

## The two groups, and why the split matters

For the first four, pick a registered or provisioned cluster as the Config Management
target (target kind **Kubernetes cluster**). The dashboard token-preps that cluster's
kubeconfig and injects it as `K8S_AUTH_KUBECONFIG` / `KUBECONFIG` — you supply nothing for
the connection, and what you get is cluster-admin.

The two `ci-*-with-ps-token.yml` plays are the **first Kubernetes plays in this repo that
authenticate with something they fetched themselves.** They are the consumer half of the
Workload Lab's Kubernetes tab: a machine *outside* the cluster retrieves a bound
ServiceAccount token from Password Safe and uses only that. The other four supplying
nothing is convenient, and is exactly the property these remove.

## The order to run them in

1. **Onboard a Deployer.** Workload Lab → Kubernetes → *Onboard*, profile `deployer`.
   Nothing has been retrieved yet.
2. **`ci-deploy-with-ps-token.yml`** — a program fetches the token and applies a
   Deployment. Point it at another namespace: **Forbidden**.
3. **Onboard a Reader**, then **`ci-read-with-ps-token.yml`** — cluster-wide reads work,
   and **reading a Secret is Forbidden**.
4. **Rotate, re-run.** The consumer never notices; the audit trail shows the reason.

Steps 2 and 3 carry the refusals, and those are the steps that prove something.

## Two traps these plays encode

**The injected kubeconfig has to be cleared.** When the dashboard runs a play against a
Kubernetes target it injects a cluster-admin kubeconfig as `K8S_AUTH_KUBECONFIG` and
`KUBECONFIG`. Left set, `kubernetes.core` authenticates with *those* instead of the
retrieved token — every task passes, the refusals do not refuse, and the play reports a
successful demonstration of nothing at all. Both plays blank them in an `environment:`
block.

**`failed_when: false`, never `ignore_errors`.** The refusal tasks have to fail so the next
task can judge *why*. `ignore_errors` would also swallow an unreachable API server, and the
assertion would then pass on a connection error rather than on a 403. Each refusal
assertion checks both that the request failed **and** that it failed with a 403.

**The refusals are asserted tasks inside the plays, not runbook steps.** A step in a
runbook gets skipped, and an assertion does not. If the refusals do not refuse, the plays
fail.

## A third consumer, which holds nothing

These two fetch the token with a Password Safe client id and secret handed to the run in
its environment. The [Agent Demo Cell](../../../docs/profiles/demo/agent-demo-cell.md) is
the consumer that holds **nothing**: it reaches Password Safe with a workload identity
brokered by Workload Credentials, so the client pair is never on its host either, and its
episode is approval-gated. See
[docs/workload-lab/consumers.md](../../../docs/workload-lab/consumers.md) for the full
register of what spends each Workload Lab credential.
